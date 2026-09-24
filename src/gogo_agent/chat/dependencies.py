"""FastAPI dependencies for chat history service, session store, and executor."""

from typing import Optional
from fastapi import Depends

from .executor import ChatAgentExecutor
from .repository import (
    AgentSessionStoreProtocol,
    ChatHistoryRepositoryProtocol,
    create_agent_session_store,
    create_chat_history_repository,
)
from .service import ChatHistoryService

_default_chat_repo: Optional[ChatHistoryRepositoryProtocol] = None
_default_session_store: Optional[AgentSessionStoreProtocol] = None
_default_history_service: Optional[ChatHistoryService] = None
_default_executor: Optional[ChatAgentExecutor] = None


def get_chat_history_repository() -> ChatHistoryRepositoryProtocol:
    global _default_chat_repo
    if _default_chat_repo is None:
        _default_chat_repo = create_chat_history_repository()
    return _default_chat_repo


def get_agent_session_store() -> AgentSessionStoreProtocol:
    global _default_session_store
    if _default_session_store is None:
        _default_session_store = create_agent_session_store()
    return _default_session_store


def get_chat_history_service(
    repo: ChatHistoryRepositoryProtocol = Depends(get_chat_history_repository),
) -> ChatHistoryService:
    global _default_history_service
    if _default_history_service is None or _default_history_service.repository is not repo:
        _default_history_service = ChatHistoryService(repository=repo)
    return _default_history_service


def get_chat_executor(
    history_service: ChatHistoryService = Depends(get_chat_history_service),
    session_store: AgentSessionStoreProtocol = Depends(get_agent_session_store),
) -> ChatAgentExecutor:
    global _default_executor
    if (
        _default_executor is None
        or _default_executor.history_service is not history_service
        or _default_executor.session_store is not session_store
    ):
        _default_executor = ChatAgentExecutor(
            chat_history_service=history_service,
            agent_session_store=session_store,
        )
    return _default_executor
