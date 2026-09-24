"""Chat history service coordinating L1 business conversation and message lifecycles."""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException, status

from .models import (
    ChatConversation,
    ChatMessage,
    ConversationView,
    MessageView,
    to_epoch_ms,
)
from .repository import ChatHistoryRepositoryProtocol, create_chat_history_repository


class ChatHistoryService:
    """Service managing conversations, messages, ownership validation, and titles."""

    DEFAULT_TITLE = "新对话"
    TITLE_MAX_LENGTH = 24

    def __init__(self, repository: Optional[ChatHistoryRepositoryProtocol] = None):
        self._repo = repository or create_chat_history_repository()

    @property
    def repository(self) -> ChatHistoryRepositoryProtocol:
        return self._repo

    def list_conversations(self, user_id: str) -> list[ConversationView]:
        """Query all non-deleted conversations for a user, sorted by updated_at desc."""
        convs = self._repo.find_conversations_by_user_id(user_id)
        views: list[ConversationView] = []
        for c in convs:
            views.append(
                ConversationView(
                    id=c.conversation_id,
                    title=c.title,
                    createdAt=to_epoch_ms(c.created_at) or 0,
                    updatedAt=to_epoch_ms(c.updated_at) or 0,
                )
            )
        return views

    def list_messages(self, conversation_id: str, user_id: str) -> list[MessageView]:
        """Query all non-deleted messages for a conversation, verifying user ownership."""
        conv = self._repo.find_conversation_by_id(conversation_id)
        if not conv:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="会话不存在",
            )
        if conv.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问该会话",
            )

        messages = self._repo.find_messages_by_conversation_id(conversation_id)
        views: list[MessageView] = []
        for m in messages:
            extra_dict: Optional[dict[str, Any]] = None
            if m.extra:
                try:
                    extra_dict = json.loads(m.extra)
                except Exception:
                    extra_dict = None

            views.append(
                MessageView(
                    id=m.message_id,
                    role=m.role,
                    content=m.content,
                    agentName=m.agent_name,
                    timestamp=to_epoch_ms(m.created_at) or 0,
                    extra=extra_dict,
                    feedback=m.feedback,
                    feedbackAt=to_epoch_ms(m.feedback_at),
                )
            )
        return views

    def save_user_message(self, conversation_id: str, user_id: str, content: str) -> str:
        """Save a user message.

        Lazy creates the conversation if not yet existing.
        If existing, strictly verifies user ownership to prevent cross-user session tampering.
        Auto-generates title from first non-empty user message if title is default.
        """
        conv = self._repo.find_conversation_by_id(conversation_id)
        now = datetime.now(timezone.utc)
        if conv is None:
            conv = ChatConversation(
                conversation_id=conversation_id,
                user_id=user_id,
                title=self.DEFAULT_TITLE,
                created_at=now,
                updated_at=now,
            )
            self._repo.save_conversation(conv)
        elif conv.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问该会话",
            )

        trimmed = (content or "").strip()
        if conv.title == self.DEFAULT_TITLE and trimmed:
            new_title = trimmed[: self.TITLE_MAX_LENGTH]
            self._repo.update_title(conversation_id, new_title)

        message_id = self._generate_message_id()
        msg = ChatMessage(
            message_id=message_id,
            conversation_id=conversation_id,
            role="user",
            content=content,
            created_at=now,
        )
        self._repo.save_message(msg)
        return message_id

    def save_assistant_message(
        self,
        conversation_id: str,
        user_id: str,
        content: str,
        agent_name: str = "GoGo",
        extra: Optional[dict[str, Any]] = None,
    ) -> str:
        """Save an AI assistant response message."""
        conv = self._repo.find_conversation_by_id(conversation_id)
        if not conv or conv.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问该会话",
            )

        message_id = self._generate_message_id()
        extra_str = json.dumps(extra, ensure_ascii=False) if extra else None
        now = datetime.now(timezone.utc)
        msg = ChatMessage(
            message_id=message_id,
            conversation_id=conversation_id,
            role="agent",
            content=content,
            agent_name=agent_name,
            extra=extra_str,
            created_at=now,
        )
        self._repo.save_message(msg)
        return message_id

    def save_system_message(self, conversation_id: str, user_id: str, content: str) -> str:
        """Save a system notification message (e.g. error or interrupt)."""
        conv = self._repo.find_conversation_by_id(conversation_id)
        if not conv or conv.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问该会话",
            )

        message_id = self._generate_message_id()
        now = datetime.now(timezone.utc)
        msg = ChatMessage(
            message_id=message_id,
            conversation_id=conversation_id,
            role="system",
            content=content,
            created_at=now,
        )
        self._repo.save_message(msg)
        return message_id

    def update_title(self, conversation_id: str, user_id: str, title: str) -> None:
        """Manually update the title of a conversation."""
        conv = self._repo.find_conversation_by_id(conversation_id)
        if not conv:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="会话不存在",
            )
        if conv.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问该会话",
            )
        clean_title = (title or "").strip() or self.DEFAULT_TITLE
        self._repo.update_title(conversation_id, clean_title)

    def delete_conversation(self, conversation_id: str, user_id: str) -> None:
        """Soft delete a conversation and its messages."""
        conv = self._repo.find_conversation_by_id(conversation_id)
        if not conv:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="会话不存在",
            )
        if conv.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问该会话",
            )
        self._repo.delete_messages_by_conversation_id(conversation_id)
        self._repo.delete_conversation(conversation_id)

    def update_feedback(
        self,
        session_id: str,
        message_id: str,
        user_id: str,
        feedback: Optional[str],
    ) -> None:
        """Update user feedback (LIKE / DISLIKE / None) for a message."""
        msg = self._repo.find_message_by_id(message_id)
        if not msg or msg.conversation_id != session_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="消息不存在",
            )
        conv = self._repo.find_conversation_by_id(session_id)
        if not conv or conv.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问该会话",
            )

        normalized = self._normalize_feedback(feedback)
        feedback_at = datetime.now(timezone.utc) if normalized else None
        self._repo.update_feedback(message_id, normalized, feedback_at)

    def _normalize_feedback(self, feedback: Optional[str]) -> Optional[str]:
        if feedback is None:
            return None
        trimmed = feedback.strip()
        if not trimmed or trimmed.upper() in ("CLEAR", "NULL", "NONE"):
            return None
        upper = trimmed.upper()
        if upper not in ("LIKE", "DISLIKE"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="无效的反馈类型，仅支持 LIKE 或 DISLIKE",
            )
        return upper

    def _generate_message_id(self) -> str:
        return f"msg_{uuid.uuid4().hex}"
