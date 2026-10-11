"""差旅业务服务的 FastAPI 注入；未迁移数据库时只让业务路由失败。"""

from fastapi import HTTPException
from sqlalchemy import inspect

from gogo_agent.db.session import get_engine, get_session_factory

from .repository import create_business_repository
from .service import TravelBusinessService


_default_service: TravelBusinessService | None = None


def get_travel_service() -> TravelBusinessService:
    """读取同一 SQL 仓储；现有数据库缺表时不影响认证和聊天启动。"""
    global _default_service
    if _default_service is None:
        factory = get_session_factory()
        if factory is not None:
            engine = get_engine()
            required = ("travel_order", "approval_record", "user_profile", "booking_record", "business_action_audit")
            inspector = inspect(engine)
            missing = [name for name in required if not inspector.has_table(name)]
            if missing:
                raise HTTPException(503, "差旅业务表尚未初始化：" + ", ".join(missing))
            order_columns = {item["name"] for item in inspector.get_columns("travel_order")}
            if "active_fingerprint" not in order_columns:
                raise HTTPException(503, "差旅业务表缺少活跃申请唯一键迁移：travel_order.active_fingerprint")
        _default_service = TravelBusinessService(create_business_repository(factory))
    return _default_service
