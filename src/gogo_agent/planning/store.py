"""060 计划记录的持久化：按用户隔离、乐观版本控制，跨用户访问一律视为不存在。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, create_engine, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from gogo_agent.db.base import Base

from .models import PlanView


class ItineraryPlanModel(Base):
    """行程规划记录表 (itinerary_plan)，每行保存一份计划的完整状态快照。"""

    __tablename__ = "itinerary_plan"

    plan_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="计划稳定标识")
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="计划所属的认证用户标识")
    session_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="创建计划的聊天会话标识，由服务端从可信请求上下文注入；独立脚本或旧数据为空",
    )
    order_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="关联的本人差旅单标识，只保存引用；未绑定审批单时为空",
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="计划版本，用于乐观并发控制")
    state_json: Mapped[str] = mapped_column(Text, nullable=False, comment="计划完整状态的 JSON 快照，含条件、进度、候选、方案与审核")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="创建 UTC 时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="最近更新 UTC 时间")

    __table_args__ = (
        # 按会话列计划是高频读路径；等值过滤走普通列，不再对 state_json 做 JSON 路径运算（无法走索引）。
        Index("idx_plan_user_session_updated", "user_id", "session_id", "updated_at"),
        # 用户只记得差旅单号；按单号反查计划必须能走索引，不能对 state_json 做 JSON 路径运算。
        Index("idx_plan_user_order", "user_id", "order_id", "updated_at"),
    )


class PlanVersionConflict(Exception):
    """保存时版本已被其他写入推进，调用方必须重新读取后再决定。"""

    code = "PLAN_VERSION_CONFLICT"


def _to_view(row: ItineraryPlanModel) -> PlanView:
    """快照优先，列作为历史行的补充来源。

    列有值而快照缺失或不同时以列为准（回填过的历史行）；列为空时保留快照原值，
    否则未回填的老行会被读成空，进而丢掉会话与审批归属。
    """
    plan = PlanView.model_validate_json(row.state_json)
    patch: dict[str, str] = {}
    if row.session_id is not None and plan.session_id != row.session_id:
        patch["session_id"] = row.session_id
    if row.order_id is not None and plan.order_id != row.order_id:
        patch["order_id"] = row.order_id
    return plan.model_copy(update=patch) if patch else plan


class PlanRepository:
    """只通过 plan_id 与 user_id 共同定位计划，避免跨用户读写。"""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def create(self, user_id: str, plan: PlanView) -> None:
        """新建计划；plan_id 冲突由数据库主键拒绝。"""
        now = plan.updated_at
        with self.session_factory() as session, session.begin():
            session.add(ItineraryPlanModel(
                plan_id=plan.plan_id, user_id=user_id, session_id=plan.session_id, order_id=plan.order_id,
                version=plan.version,
                state_json=plan.model_dump_json(), created_at=plan.created_at, updated_at=now,
            ))

    def get(self, user_id: str, plan_id: str) -> PlanView | None:
        """读取本人计划；他人或不存在的计划都返回 None。会话列对历史行是权威来源。"""
        with self.session_factory() as session:
            row = session.scalar(select(ItineraryPlanModel).where(
                ItineraryPlanModel.plan_id == plan_id, ItineraryPlanModel.user_id == user_id,
            ))
            return _to_view(row) if row is not None else None

    def list_for_session(self, user_id: str, session_id: str, limit: int = 20) -> list[PlanView]:
        """先按本人和聊天会话的普通列过滤，再取最近计划；不再解析 state_json。"""
        with self.session_factory() as session:
            rows = session.scalars(select(ItineraryPlanModel).where(
                ItineraryPlanModel.user_id == user_id,
                ItineraryPlanModel.session_id == session_id,
            ).order_by(ItineraryPlanModel.updated_at.desc()).limit(limit))
            return [_to_view(row) for row in rows]

    def list_for_order(self, user_id: str, order_id: str, limit: int = 20) -> list[PlanView]:
        """按本人差旅单号反查计划；用户通常只记得单号，不记得计划标识。"""
        with self.session_factory() as session:
            rows = session.scalars(select(ItineraryPlanModel).where(
                ItineraryPlanModel.user_id == user_id,
                ItineraryPlanModel.order_id == order_id,
            ).order_by(ItineraryPlanModel.updated_at.desc()).limit(limit))
            return [_to_view(row) for row in rows]

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
            # 旧行的会话/审批归属只在列上；快照里可能没有该字段，不能用空值把它覆盖回去。
            if plan.session_id is not None:
                row.session_id = plan.session_id
            if plan.order_id is not None:
                row.order_id = plan.order_id
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
