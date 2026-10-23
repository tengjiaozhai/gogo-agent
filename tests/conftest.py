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
