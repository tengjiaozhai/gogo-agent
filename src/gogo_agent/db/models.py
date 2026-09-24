"""SQLAlchemy ORM models mirroring the MySQL/MariaDB database schema."""

from datetime import datetime
from typing import Optional
from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class UserAccountModel(Base):
    """用户登录账号表 (user_account)。"""

    __tablename__ = "user_account"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password: Mapped[str] = mapped_column(String(128), nullable=False)
    real_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="USER", nullable=False)
    created_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ChatConversationModel(Base):
    """会话表 (chat_conversation)。"""

    __tablename__ = "chat_conversation"

    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(256), default="新对话", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    deleted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        Index("idx_user_updated", "user_id", "updated_at"),
    )


class ChatMessageModel(Base):
    """对话消息记录表 (chat_message)。"""

    __tablename__ = "chat_message"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    agent_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    extra: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    feedback: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    feedback_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    deleted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        Index("idx_conv_created", "conversation_id", "created_at"),
    )


class AgentScopeSessionModel(Base):
    """AgentScope 内部运行时状态表 (agentscope_session)。

    用于跨请求/重启持久化 AgentState（模型推理上下文、中间思考与状态）。
    与业务给人看的 chat_message (L1) 彻底解耦，形成 L2 记忆持久化。
    """

    __tablename__ = "agentscope_session"

    session_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    state_key: Mapped[str] = mapped_column(String(255), primary_key=True, default="agent_state")
    item_index: Mapped[int] = mapped_column(Integer, primary_key=True, default=0)
    state_data: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

