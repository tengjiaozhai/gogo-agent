"""Repository layer for chat history (L1) and AgentScope Agent state (L2)."""

from datetime import datetime, timezone
from typing import Optional, Protocol
from agentscope.state import AgentState

from .models import ChatConversation, ChatMessage


class ChatHistoryRepositoryProtocol(Protocol):
    """Protocol defining L1 human-readable conversation and message persistence."""

    def find_conversations_by_user_id(self, user_id: str) -> list[ChatConversation]: ...
    def find_conversation_by_id(self, conversation_id: str) -> Optional[ChatConversation]: ...
    def save_conversation(self, conversation: ChatConversation) -> None: ...
    def update_title(self, conversation_id: str, title: str) -> bool: ...
    def delete_conversation(self, conversation_id: str) -> bool: ...
    def delete_messages_by_conversation_id(self, conversation_id: str) -> int: ...
    def find_messages_by_conversation_id(self, conversation_id: str) -> list[ChatMessage]: ...
    def find_message_by_id(self, message_id: str) -> Optional[ChatMessage]: ...
    def save_message(self, message: ChatMessage) -> None: ...
    def update_feedback(
        self,
        message_id: str,
        feedback: Optional[str],
        feedback_at: Optional[datetime],
    ) -> bool: ...


class AgentSessionStoreProtocol(Protocol):
    """Protocol defining L2 AgentScope internal runtime state persistence."""

    def load_agent_state(self, session_id: str, agent_name: str = "GoGo") -> Optional[AgentState]: ...
    def save_agent_state(self, session_id: str, state: AgentState, agent_name: str = "GoGo") -> None: ...


# ===================== In-Memory Implementations =====================


class InMemoryChatHistoryRepository:
    """In-memory implementation of ChatHistoryRepository for hermetic unit testing."""

    def __init__(self):
        self._conversations: dict[str, ChatConversation] = {}
        self._messages: dict[str, ChatMessage] = {}

    def find_conversations_by_user_id(self, user_id: str) -> list[ChatConversation]:
        convs = [
            c for c in self._conversations.values()
            if c.user_id == user_id and c.deleted == 0
        ]
        convs.sort(key=lambda x: x.updated_at, reverse=True)
        return convs

    def find_conversation_by_id(self, conversation_id: str) -> Optional[ChatConversation]:
        c = self._conversations.get(conversation_id)
        if c and c.deleted == 0:
            return c
        return None

    def save_conversation(self, conversation: ChatConversation) -> None:
        self._conversations[conversation.conversation_id] = conversation

    def update_title(self, conversation_id: str, title: str) -> bool:
        c = self.find_conversation_by_id(conversation_id)
        if not c:
            return False
        c.title = title
        c.updated_at = datetime.now(timezone.utc)
        return True

    def delete_conversation(self, conversation_id: str) -> bool:
        c = self._conversations.get(conversation_id)
        if not c or c.deleted != 0:
            return False
        c.deleted = 1
        c.updated_at = datetime.now(timezone.utc)
        return True

    def delete_messages_by_conversation_id(self, conversation_id: str) -> int:
        count = 0
        for m in self._messages.values():
            if m.conversation_id == conversation_id and m.deleted == 0:
                m.deleted = 1
                count += 1
        return count

    def find_messages_by_conversation_id(self, conversation_id: str) -> list[ChatMessage]:
        msgs = [
            m for m in self._messages.values()
            if m.conversation_id == conversation_id and m.deleted == 0
        ]
        msgs.sort(key=lambda x: x.created_at)
        return msgs

    def find_message_by_id(self, message_id: str) -> Optional[ChatMessage]:
        m = self._messages.get(message_id)
        if m and m.deleted == 0:
            return m
        return None

    def save_message(self, message: ChatMessage) -> None:
        self._messages[message.message_id] = message
        # Touch conversation updated_at
        conv = self._conversations.get(message.conversation_id)
        if conv and conv.deleted == 0:
            conv.updated_at = datetime.now(timezone.utc)

    def update_feedback(
        self,
        message_id: str,
        feedback: Optional[str],
        feedback_at: Optional[datetime],
    ) -> bool:
        m = self.find_message_by_id(message_id)
        if not m:
            return False
        m.feedback = feedback
        m.feedback_at = feedback_at
        return True


class InMemoryAgentSessionStore:
    """In-memory store for AgentScope 2.x AgentState (L2 memory)."""

    def __init__(self):
        self._states: dict[str, str] = {}

    def _make_key(self, session_id: str, agent_name: str) -> str:
        return f"{session_id}:{agent_name}"

    def load_agent_state(self, session_id: str, agent_name: str = "GoGo") -> Optional[AgentState]:
        key = self._make_key(session_id, agent_name)
        data = self._states.get(key)
        if not data:
            return None
        return AgentState.model_validate_json(data)

    def save_agent_state(self, session_id: str, state: AgentState, agent_name: str = "GoGo") -> None:
        key = self._make_key(session_id, agent_name)
        self._states[key] = state.model_dump_json()


# ===================== SQL Implementations (SQLAlchemy / MariaDB) =====================


class SQLChatHistoryRepository:
    """Production SQL implementation using SQLAlchemy for L1 conversation and message history."""

    def __init__(self, session_factory=None):
        from gogo_agent.db.session import get_session_factory
        self._session_factory = session_factory or get_session_factory()

    def _to_conversation_domain(self, row) -> Optional[ChatConversation]:
        if row is None:
            return None
        return ChatConversation(
            conversation_id=row.conversation_id,
            user_id=row.user_id,
            title=row.title,
            created_at=row.created_at,
            updated_at=row.updated_at,
            deleted=row.deleted,
        )

    def _to_message_domain(self, row) -> Optional[ChatMessage]:
        if row is None:
            return None
        return ChatMessage(
            message_id=row.message_id,
            conversation_id=row.conversation_id,
            role=row.role,
            content=row.content,
            agent_name=row.agent_name,
            extra=getattr(row, "extra", None),
            feedback=getattr(row, "feedback", None),
            feedback_at=getattr(row, "feedback_at", None),
            created_at=row.created_at,
            deleted=row.deleted,
        )

    def find_conversations_by_user_id(self, user_id: str) -> list[ChatConversation]:
        if not self._session_factory:
            return []
        from sqlalchemy import desc, select
        from gogo_agent.db.models import ChatConversationModel

        with self._session_factory() as session:
            stmt = (
                select(ChatConversationModel)
                .where(
                    ChatConversationModel.user_id == user_id,
                    ChatConversationModel.deleted == 0,
                )
                .order_by(desc(ChatConversationModel.updated_at))
            )
            rows = session.scalars(stmt).all()
            return [self._to_conversation_domain(r) for r in rows if r is not None]

    def find_conversation_by_id(self, conversation_id: str) -> Optional[ChatConversation]:
        if not self._session_factory:
            return None
        from sqlalchemy import select
        from gogo_agent.db.models import ChatConversationModel

        with self._session_factory() as session:
            stmt = select(ChatConversationModel).where(
                ChatConversationModel.conversation_id == conversation_id,
                ChatConversationModel.deleted == 0,
            )
            row = session.scalar(stmt)
            return self._to_conversation_domain(row)

    def save_conversation(self, conversation: ChatConversation) -> None:
        if not self._session_factory:
            return
        from sqlalchemy import select
        from gogo_agent.db.models import ChatConversationModel

        with self._session_factory() as session:
            stmt = select(ChatConversationModel).where(
                ChatConversationModel.conversation_id == conversation.conversation_id
            )
            row = session.scalar(stmt)
            now = datetime.now(timezone.utc)
            if row:
                row.title = conversation.title
                row.updated_at = now
                row.deleted = conversation.deleted
            else:
                row = ChatConversationModel(
                    conversation_id=conversation.conversation_id,
                    user_id=conversation.user_id,
                    title=conversation.title,
                    created_at=conversation.created_at or now,
                    updated_at=conversation.updated_at or now,
                    deleted=conversation.deleted,
                )
                session.add(row)
            session.commit()

    def update_title(self, conversation_id: str, title: str) -> bool:
        if not self._session_factory:
            return False
        from sqlalchemy import update
        from gogo_agent.db.models import ChatConversationModel

        with self._session_factory() as session:
            stmt = (
                update(ChatConversationModel)
                .where(
                    ChatConversationModel.conversation_id == conversation_id,
                    ChatConversationModel.deleted == 0,
                )
                .values(title=title, updated_at=datetime.now(timezone.utc))
            )
            res = session.execute(stmt)
            session.commit()
            return res.rowcount > 0

    def delete_conversation(self, conversation_id: str) -> bool:
        if not self._session_factory:
            return False
        from sqlalchemy import update
        from gogo_agent.db.models import ChatConversationModel

        with self._session_factory() as session:
            stmt = (
                update(ChatConversationModel)
                .where(
                    ChatConversationModel.conversation_id == conversation_id,
                    ChatConversationModel.deleted == 0,
                )
                .values(deleted=1, updated_at=datetime.now(timezone.utc))
            )
            res = session.execute(stmt)
            session.commit()
            return res.rowcount > 0

    def delete_messages_by_conversation_id(self, conversation_id: str) -> int:
        if not self._session_factory:
            return 0
        from sqlalchemy import update
        from gogo_agent.db.models import ChatMessageModel

        with self._session_factory() as session:
            stmt = (
                update(ChatMessageModel)
                .where(
                    ChatMessageModel.conversation_id == conversation_id,
                    ChatMessageModel.deleted == 0,
                )
                .values(deleted=1)
            )
            res = session.execute(stmt)
            session.commit()
            return res.rowcount

    def find_messages_by_conversation_id(self, conversation_id: str) -> list[ChatMessage]:
        if not self._session_factory:
            return []
        from sqlalchemy import asc, select
        from gogo_agent.db.models import ChatMessageModel

        with self._session_factory() as session:
            stmt = (
                select(ChatMessageModel)
                .where(
                    ChatMessageModel.conversation_id == conversation_id,
                    ChatMessageModel.deleted == 0,
                )
                .order_by(asc(ChatMessageModel.created_at))
            )
            rows = session.scalars(stmt).all()
            return [self._to_message_domain(r) for r in rows if r is not None]

    def find_message_by_id(self, message_id: str) -> Optional[ChatMessage]:
        if not self._session_factory:
            return None
        from sqlalchemy import select
        from gogo_agent.db.models import ChatMessageModel

        with self._session_factory() as session:
            stmt = select(ChatMessageModel).where(
                ChatMessageModel.message_id == message_id,
                ChatMessageModel.deleted == 0,
            )
            row = session.scalar(stmt)
            return self._to_message_domain(row)

    def save_message(self, message: ChatMessage) -> None:
        if not self._session_factory:
            return
        from sqlalchemy import select, update
        from gogo_agent.db.models import ChatConversationModel, ChatMessageModel

        with self._session_factory() as session:
            stmt = select(ChatMessageModel).where(
                ChatMessageModel.message_id == message.message_id
            )
            row = session.scalar(stmt)
            now = datetime.now(timezone.utc)
            if row:
                row.content = message.content
                row.role = message.role
                row.agent_name = message.agent_name
                row.extra = message.extra
                row.feedback = message.feedback
                row.feedback_at = message.feedback_at
                row.deleted = message.deleted
            else:
                row = ChatMessageModel(
                    message_id=message.message_id,
                    conversation_id=message.conversation_id,
                    role=message.role,
                    content=message.content,
                    agent_name=message.agent_name,
                    extra=message.extra,
                    feedback=message.feedback,
                    feedback_at=message.feedback_at,
                    created_at=message.created_at or now,
                    deleted=message.deleted,
                )
                session.add(row)

            # 更新对应会话的 updated_at
            up_stmt = (
                update(ChatConversationModel)
                .where(ChatConversationModel.conversation_id == message.conversation_id)
                .values(updated_at=now)
            )
            session.execute(up_stmt)
            session.commit()

    def update_feedback(
        self,
        message_id: str,
        feedback: Optional[str],
        feedback_at: Optional[datetime],
    ) -> bool:
        if not self._session_factory:
            return False
        from sqlalchemy import update
        from gogo_agent.db.models import ChatMessageModel

        with self._session_factory() as session:
            stmt = (
                update(ChatMessageModel)
                .where(
                    ChatMessageModel.message_id == message_id,
                    ChatMessageModel.deleted == 0,
                )
                .values(feedback=feedback, feedback_at=feedback_at)
            )
            res = session.execute(stmt)
            session.commit()
            return res.rowcount > 0


class SQLAgentSessionStore:
    """Production SQL implementation for L2 AgentState in agentscope_session table."""

    def __init__(self, session_factory=None):
        from gogo_agent.db.session import get_session_factory
        self._session_factory = session_factory or get_session_factory()

    def _make_key(self, session_id: str, agent_name: str) -> str:
        return f"{session_id}:{agent_name}"

    def load_agent_state(self, session_id: str, agent_name: str = "GoGo") -> Optional[AgentState]:
        if not self._session_factory:
            return None
        from sqlalchemy import select
        from gogo_agent.db.models import AgentScopeSessionModel

        key = self._make_key(session_id, agent_name)
        with self._session_factory() as session:
            stmt = select(AgentScopeSessionModel).where(
                AgentScopeSessionModel.session_id == key,
                AgentScopeSessionModel.state_key == "agent_state",
                AgentScopeSessionModel.item_index == 0,
            )
            row = session.scalar(stmt)
            if not row or not row.state_data:
                return None
            return AgentState.model_validate_json(row.state_data)

    def save_agent_state(self, session_id: str, state: AgentState, agent_name: str = "GoGo") -> None:
        if not self._session_factory:
            return
        from sqlalchemy import select
        from gogo_agent.db.models import AgentScopeSessionModel

        key = self._make_key(session_id, agent_name)
        json_data = state.model_dump_json()
        with self._session_factory() as session:
            stmt = select(AgentScopeSessionModel).where(
                AgentScopeSessionModel.session_id == key,
                AgentScopeSessionModel.state_key == "agent_state",
                AgentScopeSessionModel.item_index == 0,
            )
            row = session.scalar(stmt)
            now = datetime.now(timezone.utc)
            if row:
                row.state_data = json_data
                row.updated_at = now
            else:
                row = AgentScopeSessionModel(
                    session_id=key,
                    state_key="agent_state",
                    item_index=0,
                    state_data=json_data,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            session.commit()


# ===================== Factory Functions =====================


def create_chat_history_repository() -> ChatHistoryRepositoryProtocol:
    """Factory creating SQL repository when DB configured, fallback to in-memory."""
    from gogo_agent.db.session import get_session_factory
    if get_session_factory() is not None:
        return SQLChatHistoryRepository()
    return InMemoryChatHistoryRepository()


def create_agent_session_store() -> AgentSessionStoreProtocol:
    """Factory creating SQL AgentState store when DB configured, fallback to in-memory."""
    from gogo_agent.db.session import get_session_factory
    if get_session_factory() is not None:
        return SQLAgentSessionStore()
    return InMemoryAgentSessionStore()
