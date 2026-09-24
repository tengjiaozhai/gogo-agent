"""对话会话、消息历史与反馈交互的 Pydantic 领域模型与 DTO 模式。"""

from datetime import datetime, timezone
from typing import Any, Optional
from pydantic import BaseModel, Field


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """将 datetime 转换为毫秒级 Unix 时间戳。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class ChatConversation(BaseModel):
    """会话领域模型。"""

    conversation_id: str = Field(..., description="会话唯一标识符")
    user_id: str = Field(..., description="所属用户唯一标识")
    title: str = Field(default="新对话", description="会话标题")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="会话创建时间",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="会话最后更新时间",
    )
    deleted: int = Field(default=0, description="逻辑删除标记 (0: 正常, 1: 已删除)")


class ChatMessage(BaseModel):
    """单条消息领域模型。"""

    message_id: str = Field(..., description="消息唯一标识符")
    conversation_id: str = Field(..., description="所属会话唯一标识符")
    role: str = Field(..., description="消息发送角色: 'user' (用户) / 'agent' (智能体) / 'system' (系统)")
    content: Optional[str] = Field(default=None, description="消息正文内容")
    agent_name: Optional[str] = Field(default=None, description="智能体名称 (如 'GoGo')")
    extra: Optional[str] = Field(default=None, description="扩展元数据 JSON 字符串")
    feedback: Optional[str] = Field(default=None, description="用户反馈: 'LIKE' (赞) / 'DISLIKE' (踩)")
    feedback_at: Optional[datetime] = Field(default=None, description="评价反馈提交时间")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="消息发送时间",
    )
    deleted: int = Field(default=0, description="逻辑删除标记 (0: 正常, 1: 已删除)")


# ===================== HTTP 视图与请求响应 DTO =====================


class ConversationView(BaseModel):
    """会话列表摘要视图对象 (对齐前端契约)。"""

    id: str = Field(..., description="会话唯一 ID (sessionId)")
    title: str = Field(..., description="会话展示标题")
    createdAt: int = Field(..., description="会话创建时间戳 (毫秒级 Unix 时间戳)")
    updatedAt: int = Field(..., description="会话最近活跃时间戳 (毫秒级 Unix 时间戳)")


class MessageView(BaseModel):
    """消息列表项视图对象 (对齐前端契约)。"""

    id: str = Field(..., description="消息唯一 ID (messageId)")
    role: str = Field(..., description="发送角色: 'user' (用户) 或 'agent' (智能体)")
    content: Optional[str] = Field(default=None, description="消息文本内容")
    agentName: Optional[str] = Field(default=None, description="智能体标识名称")
    timestamp: int = Field(..., description="发送时间戳 (毫秒级 Unix 时间戳)")
    extra: Optional[dict[str, Any]] = Field(default=None, description="扩展元数据字典 (如思考过程、工具调用)")
    feedback: Optional[str] = Field(default=None, description="用户反馈评价: 'LIKE' (赞) / 'DISLIKE' (踩) / null")
    feedbackAt: Optional[int] = Field(default=None, description="评价时间戳 (毫秒级 Unix 时间戳)")


class ChatRequest(BaseModel):
    """发送对话消息请求体。"""

    message: str = Field(default="", description="用户向智能体发送的输入文本内容")


class UpdateTitleRequest(BaseModel):
    """更新会话标题请求体。"""

    title: str = Field(..., description="新的会话标题名称")


class FeedbackRequest(BaseModel):
    """提交消息点赞/点踩反馈请求体。"""

    feedback: Optional[str] = Field(
        default=None,
        description="反馈类型: 'LIKE' (点赞) / 'DISLIKE' (点踩) / 'CLEAR' 或 null (取消评价)",
    )


class ChatJSONResponse(BaseModel):
    """非流式 (JSON) 对话响应体。"""

    sessionId: str = Field(..., description="所属会话 ID")
    messageId: str = Field(..., description="本次助手回复的消息 ID")
    content: str = Field(..., description="助手完整回复正文文本")


class ActionResponse(BaseModel):
    """通用操作结果响应体。"""

    success: bool = Field(default=True, description="操作执行是否成功")
