"""测试全局隔离：默认禁止连接 `.env` 中的真实数据库与外部服务。"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_plan_store() -> None:
    """规划记录默认走内存实现，避免测试往真实库写计划。

    需要验证持久化路径的测试自己注入 session_factory，不受此开关影响。
    """
    os.environ.setdefault("GOGO_PLAN_STORE", "memory")


@pytest.fixture(autouse=True, scope="session")
def _isolate_preferences():
    """长期偏好默认写 Redis；测试统一使用进程内后端，避免读写真实用户偏好。真实 Redis 用例自行注入。"""
    from gogo_agent.context.preferences import get_preference_store
    previous = os.environ.get("GOGO_PREFERENCE_STORE")
    os.environ["GOGO_PREFERENCE_STORE"] = "memory"
    get_preference_store.cache_clear()
    yield
    get_preference_store.cache_clear()
    if previous is None:
        os.environ.pop("GOGO_PREFERENCE_STORE", None)
    else:
        os.environ["GOGO_PREFERENCE_STORE"] = previous


@pytest.fixture(autouse=True, scope="session")
def _isolate_reme():
    """长期记忆默认关闭；避免单测里的自动记忆后台任务连接真实 ReMe。真实用例自行传入 base_url。"""
    previous = os.environ.get("GOGO_REME_BASE_URL")
    os.environ["GOGO_REME_BASE_URL"] = ""
    yield
    if previous is None:
        os.environ.pop("GOGO_REME_BASE_URL", None)
    else:
        os.environ["GOGO_REME_BASE_URL"] = previous
