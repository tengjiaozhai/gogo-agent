"""Chat module providing conversation/message persistence, AgentState storage, and chat endpoints."""

from .models import (
    ChatConversation,
    ChatMessage,
    ConversationView,
    MessageView,
    ChatRequest,
    UpdateTitleRequest,
    FeedbackRequest,
)
from .repository import (
    AgentSessionStoreProtocol,
    ChatHistoryRepositoryProtocol,
    InMemoryAgentSessionStore,
    InMemoryChatHistoryRepository,
    SQLAgentSessionStore,
    SQLChatHistoryRepository,
    create_agent_session_store,
    create_chat_history_repository,
)
from .service import ChatHistoryService
from .executor import ChatAgentExecutor
from .router import router as chat_router

__all__ = [
    "ChatConversation",
    "ChatMessage",
    "ConversationView",
    "MessageView",
    "ChatRequest",
    "UpdateTitleRequest",
    "FeedbackRequest",
    "ChatHistoryRepositoryProtocol",
    "AgentSessionStoreProtocol",
    "InMemoryChatHistoryRepository",
    "SQLChatHistoryRepository",
    "create_chat_history_repository",
    "InMemoryAgentSessionStore",
    "SQLAgentSessionStore",
    "create_agent_session_store",
    "ChatHistoryService",
    "ChatAgentExecutor",
    "chat_router",
]
