"""085：显式用户偏好，权威存储为 Redis，不让模型抽取或授权写入。

存储形状：每位用户一个 Hash，键为 `gogo:pref:{user_id}`，字段为偏好分类，值为 PreferenceView 的 JSON。
写入是单条 HSET（同一分类只保留最新值），撤回是单条 HDEL（物理删除，不留旧版本）。

取舍：Redis 不可用时写入与读取显式失败（HTTP 503）；模型输入侧只在本轮跳过偏好并记录告警，不中断对话。
测试与离线演示使用 `GOGO_PREFERENCE_STORE=memory`，走同一套校验逻辑的进程内后端。
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import json
import logging
import os
from typing import Any, Literal, cast

import redis
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis.exceptions import RedisError

from gogo_agent.auth.dependencies import get_current_user
from gogo_agent.auth.models import UserAccount
from gogo_agent.context.preference_options import PREFERENCE_OPTIONS, SPEC_BY_KEY

logger = logging.getLogger("uvicorn.error")
KEY_PREFIX = "gogo:pref:"
PreferenceKey = Literal[
    "flight_cabin", "flight_airline", "flight_seat", "flight_time", "flight_direct",
    "hotel_star", "hotel_brand", "hotel_room", "hotel_floor", "hotel_location", "hotel_facilities",
    "train_seat", "train_time", "train_position",
    "transport", "reimburse_priority", "schedule_priority", "meal",
]


class PreferenceWrite(BaseModel):
    """认证用户显式保存或更新一条长期偏好的请求。"""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    value: str = Field(..., min_length=1, max_length=500, description="用户明确要求记住的偏好；不构成政策或执行授权")
    source_message: str = Field(..., min_length=1, max_length=1000, description="本次显式设置的来源原文，可用于追溯与撤回")


class PreferenceView(PreferenceWrite):
    """当前仍然生效的一条本人长期偏好。"""
    key: PreferenceKey = Field(..., description="偏好项标识，与 Java 选项表一致；每项只保留最新有效值")
    updated_at: datetime = Field(..., description="最近显式更新的 UTC 时间")


class PreferenceStoreError(RuntimeError):
    """偏好存储不可用或记录无法解析。消息只含类别信息，不含主机、密码或偏好原文。"""


def validate_choice(key: str, value: str) -> str:
    """按选项表校验取值：单选必须是选项之一；多选必须是 JSON 数组且元素均为选项。返回规范化后的存储值。"""
    spec = SPEC_BY_KEY[key]
    options = spec["options"]
    if spec["type"] == "single":
        if value not in options:
            raise ValueError(f"{key} 的取值必须是选项之一")
        return value
    try:
        chosen: Any = json.loads(value)
    except ValueError:
        raise ValueError(f"{key} 是多选项，取值必须是 JSON 数组") from None
    if not isinstance(chosen, list) or not chosen or not all(isinstance(item, str) for item in chosen):
        raise ValueError(f"{key} 是多选项，取值必须是非空字符串数组")
    unknown = [item for item in chosen if item not in options]
    if unknown:
        raise ValueError(f"{key} 含有未知选项")
    if len(set(chosen)) != len(chosen):
        raise ValueError(f"{key} 含有重复选项")
    return json.dumps(chosen, ensure_ascii=False)


def _checked_user(user_id: str) -> str:
    if not user_id or not user_id.strip():
        raise ValueError("偏好操作必须提供用户标识")
    return user_id


class PreferenceStore:
    """唯一偏好入口：校验与序列化只在这里实现，底层读写由子类提供。"""

    def list(self, user_id: str) -> list[PreferenceView]:
        raw = self._read_all(_checked_user(user_id))
        try:
            views = [PreferenceView.model_validate_json(payload) for payload in raw.values()]
        except ValidationError:
            raise PreferenceStoreError("偏好记录格式无效，已拒绝读取") from None
        return sorted(views, key=lambda item: item.key)

    def put(self, user_id: str, key: str, request: PreferenceWrite) -> PreferenceView:
        # 同一契约同时约束 HTTP 和脚本调用；未知项与不在选项表中的取值不得入库。
        if key not in SPEC_BY_KEY:
            raise ValueError(f"未知的偏好项：{key}")
        normalized = validate_choice(key, request.value)
        request = request.model_copy(update={"value": normalized})
        view = PreferenceView.model_validate(dict(key=key, **request.model_dump(), updated_at=datetime.now(timezone.utc)))
        self._write(_checked_user(user_id), view.key, view.model_dump_json())
        return view

    def delete(self, user_id: str, key: str) -> None:
        self._remove(_checked_user(user_id), key)

    def _read_all(self, user_id: str) -> dict[str, str]:
        raise NotImplementedError

    def _write(self, user_id: str, key: str, payload: str) -> None:
        raise NotImplementedError

    def _remove(self, user_id: str, key: str) -> None:
        raise NotImplementedError


class RedisPreferenceStore(PreferenceStore):
    """Redis Hash 后端：HGETALL 读取，HSET 覆盖同类，HDEL 物理撤回。"""

    def __init__(self, client: redis.Redis):
        self._client = client

    @staticmethod
    def _hash_key(user_id: str) -> str:
        return KEY_PREFIX + user_id

    def _call(self, command, *args) -> object:
        try:
            return command(*args)
        except RedisError as exc:
            raise PreferenceStoreError(f"偏好存储不可用（{type(exc).__name__}）") from None

    def _read_all(self, user_id: str) -> dict[str, str]:
        # decode_responses=True 时 HGETALL 返回 str；类型桩仍声明为 bytes，这里显式转换以满足类型检查。
        raw = self._call(self._client.hgetall, self._hash_key(user_id))
        return {str(field): str(value) for field, value in cast(dict, raw).items()}

    def _write(self, user_id: str, key: str, payload: str) -> None:
        self._call(self._client.hset, self._hash_key(user_id), key, payload)

    def _remove(self, user_id: str, key: str) -> None:
        self._call(self._client.hdel, self._hash_key(user_id), key)


class InMemoryPreferenceStore(PreferenceStore):
    """进程内后端，仅用于测试与离线演示。传入同一个 dict 可模拟多个实例共享后端。"""

    def __init__(self, data: dict[str, dict[str, str]] | None = None):
        self._data: dict[str, dict[str, str]] = {} if data is None else data

    def _read_all(self, user_id: str) -> dict[str, str]:
        return dict(self._data.get(user_id, {}))

    def _write(self, user_id: str, key: str, payload: str) -> None:
        self._data.setdefault(user_id, {})[key] = payload

    def _remove(self, user_id: str, key: str) -> None:
        self._data.get(user_id, {}).pop(key, None)


def _redis_client_from_env() -> redis.Redis:
    host = os.getenv("GOGO_REDIS_HOST", "121.43.231.155").strip()
    if not host:
        raise PreferenceStoreError("未配置 GOGO_REDIS_HOST，无法保存长期偏好")
    return redis.Redis(
        host=host,
        port=int(os.getenv("GOGO_REDIS_PORT", "6379").strip()),
        db=int(os.getenv("GOGO_REDIS_DB", "0").strip()),
        password=os.getenv("GOGO_REDIS_PASSWORD", "").strip() or None,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


@lru_cache(maxsize=1)
def get_preference_store() -> PreferenceStore:
    """默认使用 GOGO_REDIS_* 指向的 Redis；GOGO_PREFERENCE_STORE=memory 时使用进程内后端。"""
    mode = os.getenv("GOGO_PREFERENCE_STORE", "").strip().lower()
    if mode == "memory":
        return InMemoryPreferenceStore()
    if mode not in ("", "redis"):
        raise PreferenceStoreError("GOGO_PREFERENCE_STORE 只接受 redis 或 memory")
    return RedisPreferenceStore(_redis_client_from_env())


def preferences_for_model(store: PreferenceStore, user_id: str) -> list[dict]:
    """模型输入使用的有效偏好副本；存储不可用时本轮不注入偏好，并记录脱敏告警。"""
    try:
        return [{"key": item.key, "value": item.value, "source_message": item.source_message}
                for item in store.list(user_id)]
    except PreferenceStoreError as exc:
        logger.warning("preferences skipped for this turn: %s", exc)
        return []


router = APIRouter(prefix="/api/preferences", tags=["preferences"])


def _unavailable(exc: PreferenceStoreError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.get("/options", summary="返回差旅偏好选项表（与 Java 基线文案一致）")
def list_preference_options(user: UserAccount = Depends(get_current_user)):
    return PREFERENCE_OPTIONS


@router.get("", response_model=list[PreferenceView], summary="读取本人的有效长期偏好")
def list_preferences(user: UserAccount = Depends(get_current_user), store: PreferenceStore = Depends(get_preference_store)):
    try:
        return store.list(user.user_id)
    except PreferenceStoreError as exc:
        raise _unavailable(exc) from None


@router.put("/{key}", response_model=PreferenceView, summary="本人显式保存或替换一项长期偏好")
def put_preference(key: PreferenceKey, request: PreferenceWrite,
                   user: UserAccount = Depends(get_current_user), store: PreferenceStore = Depends(get_preference_store)):
    try:
        return store.put(user.user_id, key, request)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    except PreferenceStoreError as exc:
        raise _unavailable(exc) from None


@router.delete("/{key}", status_code=204, summary="本人撤回一项长期偏好")
def delete_preference(key: PreferenceKey,
                      user: UserAccount = Depends(get_current_user), store: PreferenceStore = Depends(get_preference_store)) -> None:
    try:
        store.delete(user.user_id, key)
    except PreferenceStoreError as exc:
        raise _unavailable(exc) from None
