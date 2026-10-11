"""SQLAlchemy ORM models mirroring the MySQL/MariaDB database schema."""

from datetime import datetime
from decimal import Decimal
from typing import Optional
from sqlalchemy import BigInteger, DateTime, Index, Integer, Numeric, String, Text, UniqueConstraint
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


class TravelOrderModel(Base):
    """差旅申请单；状态与当前审批关联由业务事务维护。"""

    __tablename__ = "travel_order"

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="差旅单唯一标识")
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="申请人的认证用户标识")
    destination: Mapped[str] = mapped_column(String(256), nullable=False, comment="出差目的地城市")
    departure_city: Mapped[str] = mapped_column(String(128), nullable=False, comment="出发城市")
    departure_date: Mapped[str] = mapped_column(String(32), nullable=False, comment="出发日期，YYYY-MM-DD")
    return_date: Mapped[str] = mapped_column(String(32), nullable=False, comment="返回日期，YYYY-MM-DD")
    purpose: Mapped[str] = mapped_column(String(512), nullable=False, comment="出差事由")
    status: Mapped[str] = mapped_column(String(32), nullable=False, comment="差旅单状态：DRAFT/SUBMITTED/APPROVED/REJECTED/COMPLETED/CANCELLED")
    approval_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="当前关联的审批流程实例标识")
    active_fingerprint: Mapped[Optional[str]] = mapped_column(
        String(64), unique=True, nullable=True, comment="当前有效申请的用户与规范化字段指纹；撤销或拒绝后清空",
    )
    plan_html_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True, comment="规划 HTML 对象存储 key，尚未生成时为空")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="差旅单创建 UTC 时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="差旅单最近修改 UTC 时间")

    __table_args__ = (Index("idx_travel_user_status", "user_id", "status"),)


class ApprovalRecordModel(Base):
    """每次差旅审批的不可替代历史记录；同一差旅单可有多条。"""

    __tablename__ = "approval_record"

    process_instance_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="审批流程实例唯一标识")
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="审批申请人的认证用户标识")
    title: Mapped[str] = mapped_column(String(256), nullable=False, comment="审批单展示标题")
    status: Mapped[str] = mapped_column(String(32), nullable=False, comment="审批状态：PENDING/APPROVED/REJECTED/CANCELLED")
    approval_form: Mapped[str] = mapped_column(Text, nullable=False, comment="提交时差旅单字段的 JSON 快照")
    remark: Mapped[Optional[str]] = mapped_column(String(512), nullable=True, comment="管理员审批备注")
    submit_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="审批提交 UTC 时间")
    update_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="审批最近更新 UTC 时间")
    order_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="关联的差旅单标识")

    __table_args__ = (Index("idx_approval_order_submit", "order_id", "submit_time"),)


class UserProfileModel(Base):
    """认证用户拥有的联系人与常驻地档案。"""

    __tablename__ = "user_profile"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="档案所有者的认证用户标识")
    base_city: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, comment="用户常驻城市")
    level: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, comment="用户职级，如 P5/P6/P7/P8")
    name_pinyin: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, comment="用于酒店预订的姓名拼音")
    email: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, comment="用于酒店预订的邮箱")
    chinese_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="用于机票预订的中文姓名")
    id_type: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="证件类型编号 0–9；0 为身份证、1 为护照，其余按供应商约定")
    id_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="旅客证件号")
    phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, comment="联系人手机号")
    gender: Mapped[Optional[str]] = mapped_column(String(4), nullable=True, comment="性别，M 或 F")


class BookingRecordModel(Base):
    """内部预订记录；不以本地状态代替供应商订单结果。"""

    __tablename__ = "booking_record"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True,
        comment="内部数据库行标识，对应 Java BIGINT，SQLite 测试用 INTEGER 自增",
    )
    booking_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, comment="系统生成的预订业务标识")
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="预订所属的认证用户标识")
    conversation_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="关联聊天会话标识")
    travel_order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True, comment="关联差旅单标识")
    biz_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="业务类型：FLIGHT/HOTEL/TRAIN")
    platform: Mapped[str] = mapped_column(String(32), nullable=False, comment="供应商平台名称")
    external_order_no: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, comment="供应商订单标识")
    status: Mapped[str] = mapped_column(String(32), nullable=False, comment="本地预订状态，如 CREATED/CONFIRMED/CANCELLED")
    external_status: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="供应商原始状态")
    payment_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, comment="支付状态：UNPAID/PAID/REFUNDED")
    title: Mapped[Optional[str]] = mapped_column(String(256), nullable=True, comment="预订展示标题")
    total_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, comment="订单总金额，元")
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY", comment="金额币种")
    contact_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="供应商联系人姓名")
    contact_phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, comment="供应商联系人手机号")
    start_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="服务开始 UTC 时间")
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="服务结束 UTC 时间")
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="供应商原始明细 JSON")
    remark: Mapped[Optional[str]] = mapped_column(String(512), nullable=True, comment="预订备注或取消原因")
    booked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="供应商下单 UTC 时间")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="内部记录创建 UTC 时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="内部记录最近更新 UTC 时间")
    deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="软删除标志，0 为有效，1 为已删除")

    __table_args__ = (
        Index("idx_booking_user_biz", "user_id", "biz_type"),
        Index("idx_booking_status", "status"),
        UniqueConstraint("platform", "biz_type", "external_order_no", name="uk_platform_order"),
    )


class BusinessActionAuditModel(Base):
    """用户确认后的业务写入审计与幂等结果。"""

    __tablename__ = "business_action_audit"

    action_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="本次写入动作的唯一审计标识")
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="发起写入的认证用户标识")
    action: Mapped[str] = mapped_column(String(32), nullable=False, comment="写入动作类型，如 submit/modify/cancel")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, comment="调用方提供的请求幂等键")
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="规范化请求体的 SHA-256 摘要")
    result_json: Mapped[str] = mapped_column(Text, nullable=False, comment="成功写入结果的 JSON 快照")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="动作完成 UTC 时间")

    __table_args__ = (UniqueConstraint("user_id", "action", "idempotency_key", name="uk_business_action_request"),)
