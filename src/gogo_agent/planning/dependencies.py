"""043 规划服务的进程内注入。

计划记录优先持久化到 `GOGO_DATABASE_URL` 指向的关系库（表 `itinerary_plan`），
因此进程重启或多 worker 之间都能读回同一个计划；未配置数据库时回落到进程内存
SQLite，仅供练习与测试。设置环境变量 `GOGO_PLAN_STORE=memory` 可强制使用内存实现，
自动化测试用它避免写入真实库。
"""

from __future__ import annotations

import logging
import os

from gogo_agent.db.base import Base
from gogo_agent.db.session import get_engine, get_session_factory

from .service import PlanService
from .store import ItineraryPlanModel, PlanRepository, create_plan_repository

logger = logging.getLogger(__name__)

MEMORY_STORE_ENV = "GOGO_PLAN_STORE"
_service: PlanService | None = None


def _build_repository() -> PlanRepository:
    """有可用数据库时用持久化仓储，否则回落内存；建表幂等，不依赖单独的迁移脚本。"""
    if os.getenv(MEMORY_STORE_ENV, "").strip().lower() == "memory":
        return create_plan_repository()
    factory = get_session_factory()
    engine = get_engine()
    if factory is None or engine is None:
        return create_plan_repository()
    try:
        Base.metadata.create_all(bind=engine, tables=[ItineraryPlanModel.__table__], checkfirst=True)
    except Exception:
        # 建表失败（如账号无 DDL 权限）时显式降级并记录，避免请求路径直接 500。
        logger.warning("itinerary_plan 建表失败，规划记录退回内存存储", exc_info=True)
        return create_plan_repository()
    return PlanRepository(factory)


def get_plan_service() -> PlanService:
    """返回进程内唯一的规划服务；首次调用时创建。"""
    global _service
    if _service is None:
        _service = PlanService(_build_repository())
    return _service


def reset_plan_service(service: PlanService | None = None) -> None:
    """测试和脚本专用：替换或清空进程内服务。"""
    global _service
    _service = service
