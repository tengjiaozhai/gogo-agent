"""043 候选搜索：经途牛 CLI 只读查询，凭证只用当前用户自己的途牛 API Key。

没有用户 Key、Key 服务不可用或途牛返回失败时，搜索显式失败；不会回退到其他用户、管理员或服务级密钥，
也不会用模拟数据冒充真实候选。
"""

from __future__ import annotations

import asyncio
import os
from typing import Protocol

from gogo_agent.db.session import get_session_factory
from gogo_agent.security.apikey import ApiKeyService
from gogo_agent.security.cipher import SecretCipher, SecretCipherError

from gogo_agent.integrations.skill_runner import SkillError
from gogo_agent.integrations.tuniu import TuniuCliAdapter, to_candidate_set

from .models import CandidateSet, PlanConditions

TUNIU_PROVIDER = "tuniu-cli"


class PlanSearchError(RuntimeError):
    """候选搜索失败；message 是可以展示给用户的固定中文说明，不含密钥或供应商原文。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TuniuKeyProvider(Protocol):
    """按用户取途牛 API Key；只能返回该用户自己的 Key。"""

    def get_api_key(self, user_id: str) -> str:
        """没有该用户的可用 Key 时抛出 PlanSearchError。"""
        ...


class UnconfiguredTuniuKeys:
    """未配置 Key 服务时的默认实现：所有用户都明确失败。"""

    def get_api_key(self, user_id: str) -> str:
        raise PlanSearchError("TUNIU_KEY_SERVICE_UNCONFIGURED", "途牛 API Key 服务未配置，无法搜索真实候选")


class ApiKeyServiceTuniuKeys:
    """把阶段安全模块的用户级 Key 服务接到搜索；只调用 resolve_for_use，不缓存明文。"""

    def __init__(self, service) -> None:
        self._service = service

    def get_api_key(self, user_id: str) -> str:
        try:
            return self._service.resolve_for_use(user_id, TUNIU_PROVIDER).get_secret_value()
        except Exception as exc:  # 只保留错误码，避免把数据库或密文细节带给模型
            code = getattr(exc, "code", type(exc).__name__)
            raise PlanSearchError(
                "TUNIU_KEY_MISSING", f"当前用户未配置可用的途牛 API Key（{code}），无法搜索真实候选",
            ) from None


class TuniuPlanSearch:
    """并发查询去程、返程机票与火车、目的地酒店，并合并为规划候选。"""

    def __init__(self, adapter: TuniuCliAdapter, keys: TuniuKeyProvider) -> None:
        self._adapter = adapter
        self._keys = keys

    async def __call__(self, user_id: str, conditions: PlanConditions) -> CandidateSet:
        api_key = self._keys.get_api_key(user_id)
        origin, destination = conditions.origin, conditions.destination
        try:
            results = await asyncio.gather(
                self._adapter.search_flights(user_id=user_id, api_key=api_key, departure_city=origin,
                                             arrival_city=destination, departure_date=conditions.departure_date),
                self._adapter.search_trains(user_id=user_id, api_key=api_key, departure_city=origin,
                                            arrival_city=destination, departure_date=conditions.departure_date),
                self._adapter.search_flights(user_id=user_id, api_key=api_key, departure_city=destination,
                                             arrival_city=origin, departure_date=conditions.return_date),
                self._adapter.search_trains(user_id=user_id, api_key=api_key, departure_city=destination,
                                            arrival_city=origin, departure_date=conditions.return_date),
                self._adapter.search_hotels(user_id=user_id, api_key=api_key, city=destination,
                                            check_in=conditions.departure_date, check_out=conditions.return_date),
            )
        except SkillError as exc:
            raise PlanSearchError("TUNIU_SEARCH_FAILED", f"途牛搜索失败（{exc.code}），可稍后重试") from None
        return to_candidate_set(list(results))


def build_tuniu_key_provider() -> TuniuKeyProvider:
    """只有配置了加密密钥且数据库可用时才启用用户级 Key；否则所有用户明确失败。"""
    if not os.getenv("GOGO_SECRET_KEYS", "").strip():
        return UnconfiguredTuniuKeys()
    factory = get_session_factory()
    if factory is None:
        return UnconfiguredTuniuKeys()
    try:
        cipher = SecretCipher.from_env()
    except SecretCipherError:
        return UnconfiguredTuniuKeys()
    return ApiKeyServiceTuniuKeys(ApiKeyService(factory, cipher))
