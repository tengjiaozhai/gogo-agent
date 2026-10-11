"""阶段 D 单一 SQLAlchemy 仓储；业务工具不能直接组装 SQL。"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
from typing import Optional

from sqlalchemy import Engine, create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gogo_agent.db.base import Base
from gogo_agent.db.models import (
    ApprovalRecordModel, BookingRecordModel, BusinessActionAuditModel,
    TravelOrderModel, UserProfileModel,
)
from gogo_agent.db.session import get_session_factory


class BusinessRepository:
    """用调用方控制的事务读写差旅、审批、资料和预订记录。"""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """同一次业务写入和审计共用一个提交或回滚边界。"""
        with self.session_factory() as session:
            with session.begin():
                yield session

    @staticmethod
    def profile(session: Session, user_id: str) -> Optional[UserProfileModel]:
        return session.get(UserProfileModel, user_id)

    @staticmethod
    def order(session: Session, user_id: str, order_id: str) -> Optional[TravelOrderModel]:
        return session.scalar(select(TravelOrderModel).where(
            TravelOrderModel.order_id == order_id, TravelOrderModel.user_id == user_id,
        ))

    @staticmethod
    def order_for_update(session: Session, user_id: str, order_id: str) -> Optional[TravelOrderModel]:
        """修改/取消时锁定订单行；MySQL 上与管理员路径统一先锁订单。"""
        return session.scalar(select(TravelOrderModel).where(
            TravelOrderModel.order_id == order_id, TravelOrderModel.user_id == user_id,
        ).with_for_update().execution_options(populate_existing=True))

    @staticmethod
    def order_any_user(session: Session, order_id: str) -> Optional[TravelOrderModel]:
        return session.get(TravelOrderModel, order_id)

    @staticmethod
    def order_any_user_for_update(session: Session, order_id: str) -> Optional[TravelOrderModel]:
        """管理员审批前按相同顺序锁定订单，避免与本人修改/取消交错。"""
        return session.scalar(select(TravelOrderModel).where(
            TravelOrderModel.order_id == order_id,
        ).with_for_update().execution_options(populate_existing=True))

    @staticmethod
    def orders(session: Session, user_id: str, status: str | None = None) -> list[TravelOrderModel]:
        statement = select(TravelOrderModel).where(TravelOrderModel.user_id == user_id)
        if status is not None:
            statement = statement.where(TravelOrderModel.status == status)
        return list(session.scalars(statement.order_by(TravelOrderModel.created_at.desc())))

    @staticmethod
    def matching_active_order(session: Session, user_id: str, fields: dict[str, str]) -> Optional[TravelOrderModel]:
        return session.scalar(select(TravelOrderModel).where(
            TravelOrderModel.user_id == user_id,
            TravelOrderModel.destination == fields["destination"],
            TravelOrderModel.departure_city == fields["departure_city"],
            TravelOrderModel.departure_date == fields["departure_date"],
            TravelOrderModel.return_date == fields["return_date"],
            TravelOrderModel.purpose == fields["purpose"],
            TravelOrderModel.status.in_(("DRAFT", "SUBMITTED", "APPROVED")),
        ))

    @staticmethod
    def approval(session: Session, user_id: str, process_instance_id: str) -> Optional[ApprovalRecordModel]:
        return session.scalar(select(ApprovalRecordModel).where(
            ApprovalRecordModel.user_id == user_id,
            ApprovalRecordModel.process_instance_id == process_instance_id,
        ))

    @staticmethod
    def approval_for_update(session: Session, user_id: str, process_instance_id: str) -> Optional[ApprovalRecordModel]:
        """在已锁订单后锁当前审批记录。"""
        return session.scalar(select(ApprovalRecordModel).where(
            ApprovalRecordModel.user_id == user_id,
            ApprovalRecordModel.process_instance_id == process_instance_id,
        ).with_for_update().execution_options(populate_existing=True))

    @staticmethod
    def approval_any_user(session: Session, process_instance_id: str) -> Optional[ApprovalRecordModel]:
        return session.get(ApprovalRecordModel, process_instance_id)

    @staticmethod
    def approvals_for_order(session: Session, user_id: str, order_id: str) -> list[ApprovalRecordModel]:
        return list(session.scalars(select(ApprovalRecordModel).where(
            ApprovalRecordModel.user_id == user_id, ApprovalRecordModel.order_id == order_id,
        ).order_by(ApprovalRecordModel.submit_time.desc())))

    @staticmethod
    def approvals(session: Session, status: str | None = None) -> list[ApprovalRecordModel]:
        statement = select(ApprovalRecordModel)
        if status is not None:
            statement = statement.where(ApprovalRecordModel.status == status)
        return list(session.scalars(statement.order_by(ApprovalRecordModel.submit_time.desc())))

    @staticmethod
    def decide_if_pending(session: Session, process_instance_id: str, status: str, remark: str | None) -> bool:
        result = session.execute(update(ApprovalRecordModel).where(
            ApprovalRecordModel.process_instance_id == process_instance_id,
            ApprovalRecordModel.status == "PENDING",
        ).values(status=status, remark=remark))
        return result.rowcount == 1

    @staticmethod
    def booking(session: Session, user_id: str, booking_id: str) -> Optional[BookingRecordModel]:
        return session.scalar(select(BookingRecordModel).where(
            BookingRecordModel.user_id == user_id,
            BookingRecordModel.booking_id == booking_id,
            BookingRecordModel.deleted == 0,
        ))

    @staticmethod
    def bookings(
        session: Session, user_id: str, *, status: str | None = None,
        biz_type: str | None = None, order_id: str | None = None,
    ) -> list[BookingRecordModel]:
        statement = select(BookingRecordModel).where(
            BookingRecordModel.user_id == user_id, BookingRecordModel.deleted == 0,
        )
        if status is not None:
            statement = statement.where(BookingRecordModel.status == status)
        if biz_type is not None:
            statement = statement.where(BookingRecordModel.biz_type == biz_type)
        if order_id is not None:
            statement = statement.where(BookingRecordModel.travel_order_id == order_id)
        return list(session.scalars(statement.order_by(BookingRecordModel.created_at.desc())))

    @staticmethod
    def audit(session: Session, user_id: str, action: str, idempotency_key: str) -> Optional[BusinessActionAuditModel]:
        return session.scalar(select(BusinessActionAuditModel).where(
            BusinessActionAuditModel.user_id == user_id,
            BusinessActionAuditModel.action == action,
            BusinessActionAuditModel.idempotency_key == idempotency_key,
        ))


_memory_engine: Engine | None = None
_memory_factory: sessionmaker[Session] | None = None


def create_business_repository(session_factory: sessionmaker[Session] | None = None) -> BusinessRepository:
    """配置数据库时沿用项目 SQL 会话；本地无库时使用隔离内存 SQLite。"""
    global _memory_engine, _memory_factory
    factory = session_factory or get_session_factory()
    if factory is None:
        if _memory_factory is None:
            _memory_engine = create_engine(
                "sqlite+pysqlite:///:memory:",
                connect_args={"check_same_thread": False}, poolclass=StaticPool,
            )
            Base.metadata.create_all(_memory_engine, tables=[
                TravelOrderModel.__table__, ApprovalRecordModel.__table__,
                UserProfileModel.__table__, BookingRecordModel.__table__,
                BusinessActionAuditModel.__table__,
            ])
            _memory_factory = sessionmaker(bind=_memory_engine, expire_on_commit=False)
        factory = _memory_factory
    return BusinessRepository(factory)
