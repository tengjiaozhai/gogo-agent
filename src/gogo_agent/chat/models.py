"""Pydantic domain and DTO models for chat conversations, messages, and feedback."""

from datetime import datetime, timezone
from typing import Any, Optional
from pydantic import BaseModel, Field


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to millisecond Unix epoch timestamp."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class ChatConversation(BaseModel):
    """Domain model for a conversation session."""

    conversation_id: str
    user_id: str
    title: str = "新对话"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    deleted: int = 0


class ChatMessage(BaseModel):
    """Domain model for a single message in a conversation."""

    message_id: str
    conversation_id: str
    role: str  # "user" | "agent" | "system"
    content: Optional[str] = None
    agent_name: Optional[str] = None
    extra: Optional[str] = None  # JSON string
    feedback: Optional[str] = None  # "LIKE" | "DISLIKE" | None
    feedback_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    deleted: int = 0


# ===================== HTTP DTOs =====================


class ConversationView(BaseModel):
    """Frontend-compatible conversation view DTO."""

    id: str
    title: str
    createdAt: int
    updatedAt: int


class MessageView(BaseModel):
    """Frontend-compatible message item view DTO."""

    id: str
    role: str
    content: Optional[str] = None
    agentName: Optional[str] = None
    timestamp: int
    extra: Optional[dict[str, Any]] = None
    feedback: Optional[str] = None
    feedbackAt: Optional[int] = None


class ChatRequest(BaseModel):
    """Chat invocation request payload."""

    message: str = ""


class UpdateTitleRequest(BaseModel):
    """Title update request payload."""

    title: str


class FeedbackRequest(BaseModel):
    """Message feedback request payload."""

    feedback: Optional[str] = None


class ChatJSONResponse(BaseModel):
    """JSON response for chat invocation when non-streaming is requested."""

    sessionId: str
    messageId: str
    content: str


class ActionResponse(BaseModel):
    """Generic action response (e.g. updated=true, deleted=true)."""

    success: bool = True
