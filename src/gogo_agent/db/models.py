"""SQLAlchemy ORM models mirroring the MySQL/MariaDB database schema."""

from datetime import datetime
from typing import Optional
from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class UserAccountModel(Base):
    """用户登录账号表 (user_account)。"""

    __tablename__ = "user_account"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键自增 ID")
    user_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True, comment="用户全局唯一标识符")
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True, comment="登录账号名称")
    password: Mapped[str] = mapped_column(String(128), nullable=False, comment="密码哈希值或兼容的旧明文")
    real_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="用户真实姓名")
    role: Mapped[str] = mapped_column(String(16), default="USER", nullable=False, comment="角色权限: USER / ADMIN")
    created_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, comment="账号创建时间")


class ChatConversationModel(Base):
    """会话表 (chat_conversation)。"""

    __tablename__ = "chat_conversation"

    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="会话唯一标识符")
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False, comment="所属用户唯一标识符")
    title: Mapped[str] = mapped_column(String(256), default="新对话", nullable=False, comment="会话展示标题")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, comment="会话创建时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False, comment="会话最近更新时间")
    deleted: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="逻辑删除标记 (0: 正常, 1: 已删除)")

    __table_args__ = (
        Index("idx_user_updated", "user_id", "updated_at"),
    )


class ChatMessageModel(Base):
    """对话消息记录表 (chat_message)。"""

    __tablename__ = "chat_message"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="消息唯一标识符")
    conversation_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False, comment="所属会话唯一标识符")
    role: Mapped[str] = mapped_column(String(32), nullable=False, comment="发送角色: user / agent / system")
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="消息正文内容")
    agent_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, comment="智能体名称 (如 GoGo)")
    extra: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="扩展元数据 JSON 文本")
    feedback: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="用户反馈评价: LIKE / DISLIKE")
    feedback_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="评价反馈提交时间")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, comment="消息发送时间")
    deleted: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="逻辑删除标记 (0: 正常, 1: 已删除)")

    __table_args__ = (
        Index("idx_conv_created", "conversation_id", "created_at"),
    )


class AgentScopeSessionModel(Base):
    """AgentScope 内部运行时状态表 (agentscope_session)。

    用于跨请求/重启持久化 AgentState（模型推理上下文、中间思考与状态）。
    与业务给人看的 chat_message (L1) 彻底解耦，形成 L2 记忆持久化。
    """

    __tablename__ = "agentscope_session"

    session_id: Mapped[str] = mapped_column(String(255), primary_key=True, comment="会话唯一标识符")
    state_key: Mapped[str] = mapped_column(String(255), primary_key=True, default="agent_state", comment="状态键名 (如 agent_state)")
    item_index: Mapped[int] = mapped_column(Integer, primary_key=True, default=0, comment="多分块状态索引项")
    state_data: Mapped[str] = mapped_column(Text, nullable=False, comment="AgentState 序列化 JSON 文本")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, comment="状态创建时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False, comment="状态最近更新时间")

