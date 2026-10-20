"""060 计划记录的持久化：按用户隔离、乐观版本控制，跨用户访问一律视为不存在。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from gogo_agent.db.base import Base

from .models import PlanView


class ItineraryPlanModel(Base):
    """行程规划记录表 (itinerary_plan)，每行保存一份计划的完整状态快照。"""

    __tablename__ = "itinerary_plan"

    plan_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="计划稳定标识")
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="计划所属的认证用户标识")
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="计划版本，用于乐观并发控制")
    state_json: Mapped[str] = mapped_column(Text, nullable=False, comment="计划完整状态的 JSON 快照，含条件、进度、候选、方案与审核")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="创建 UTC 时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="最近更新 UTC 时间")


class PlanVersionConflict(Exception):
    """保存时版本已被其他写入推进，调用方必须重新读取后再决定。"""

    code = "PLAN_VERSION_CONFLICT"


class PlanRepository:
    """只通过 plan_id 与 user_id 共同定位计划，避免跨用户读写。"""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def create(self, user_id: str, plan: PlanView) -> None:
        """新建计划；plan_id 冲突由数据库主键拒绝。"""
        now = plan.updated_at
        with self.session_factory() as session, session.begin():
            session.add(ItineraryPlanModel(
                plan_id=plan.plan_id, user_id=user_id, version=plan.version,
                state_json=plan.model_dump_json(), created_at=plan.created_at, updated_at=now,
            ))

    def get(self, user_id: str, plan_id: str) -> PlanView | None:
        """读取本人计划；他人或不存在的计划都返回 None。"""
        with self.session_factory() as session:
            row = session.scalar(select(ItineraryPlanModel).where(
                ItineraryPlanModel.plan_id == plan_id, ItineraryPlanModel.user_id == user_id,
            ))
            return PlanView.model_validate_json(row.state_json) if row is not None else None

    def save(self, user_id: str, plan: PlanView, expected_version: int) -> None:
        """仅当数据库中的版本仍等于 expected_version 时写入新版本。"""
        with self.session_factory() as session, session.begin():
            row = session.scalar(select(ItineraryPlanModel).where(
                ItineraryPlanModel.plan_id == plan.plan_id, ItineraryPlanModel.user_id == user_id,
            ))
            if row is None:
                raise KeyError(plan.plan_id)
            if row.version != expected_version:
                raise PlanVersionConflict(f"计划版本已变化：期望 {expected_version}，实际 {row.version}")
            row.version = plan.version
            row.state_json = plan.model_dump_json()
            row.updated_at = plan.updated_at


def create_plan_repository(session_factory: sessionmaker[Session] | None = None) -> PlanRepository:
    """未指定会话工厂时使用隔离的内存 SQLite，供练习与测试；不自动连接生产库。"""
    if session_factory is None:
        engine = create_engine(
            "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(engine, tables=[ItineraryPlanModel.__table__])
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return PlanRepository(session_factory)
