"""FastAPI router for chat conversation, message history, feedback, and execution endpoints."""

from typing import Optional
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from gogo_agent.auth.dependencies import get_current_user
from gogo_agent.auth.models import UserAccount

from .dependencies import get_chat_executor, get_chat_history_service
from .executor import ChatAgentExecutor
from .models import (
    ChatJSONResponse,
    ChatRequest,
    ConversationView,
    FeedbackRequest,
    MessageView,
    UpdateTitleRequest,
)
from .service import ChatHistoryService

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.get(
    "/conversations",
    response_model=list[ConversationView],
    summary="获取当前登录用户的所有历史会话",
)
async def list_conversations(
    current_user: UserAccount = Depends(get_current_user),
    history_service: ChatHistoryService = Depends(get_chat_history_service),
) -> list[ConversationView]:
    """查询当前登录用户的所有历史会话，按最后更新时间倒序排序。"""
    return history_service.list_conversations(current_user.user_id)


@router.get(
    "/{sessionId}/messages",
    response_model=list[MessageView],
    summary="获取指定会话的历史消息",
)
async def list_messages(
    sessionId: str,
    current_user: UserAccount = Depends(get_current_user),
    history_service: ChatHistoryService = Depends(get_chat_history_service),
) -> list[MessageView]:
    """查询指定会话的消息历史。严格校验会话所有权，防越权读取。"""
    return history_service.list_messages(sessionId, current_user.user_id)


@router.post(
    "/{sessionId}",
    summary="向指定会话发送消息并触发智能体回复",
)
async def send_chat_message(
    sessionId: str,
    req: ChatRequest,
    request: Request,
    current_user: UserAccount = Depends(get_current_user),
    executor: ChatAgentExecutor = Depends(get_chat_executor),
):
    """核心对话接口。

    持久化用户消息 (L1)，恢复或创建 AgentState 记忆 (L2)，驱动 AgentScope Agent 推理，
    并持久化助手回复。默认以 text/event-stream (SSE) 输出以适配前端；若客户端指定
    Accept: application/json 则以结构化 JSON 返回。
    """
    accept_header = request.headers.get("accept", "")
    if "application/json" in accept_header and "text/event-stream" not in accept_header:
        reply_text, msg_id = await executor.execute_turn(
            session_id=sessionId,
            user_id=current_user.user_id,
            message=req.message,
        )
        return ChatJSONResponse(sessionId=sessionId, messageId=msg_id, content=reply_text)

    return StreamingResponse(
        executor.stream_turn_sse(
            session_id=sessionId,
            user_id=current_user.user_id,
            message=req.message,
        ),
        media_type="text/event-stream",
    )


@router.put(
    "/{sessionId}/title",
    summary="更新会话标题",
)
async def update_title(
    sessionId: str,
    req: UpdateTitleRequest,
    current_user: UserAccount = Depends(get_current_user),
    history_service: ChatHistoryService = Depends(get_chat_history_service),
) -> dict[str, bool]:
    """更新指定会话标题。严格校验会话所有权。"""
    history_service.update_title(sessionId, current_user.user_id, req.title)
    return {"updated": True}


@router.delete(
    "/{sessionId}",
    summary="逻辑删除指定会话及其消息",
)
async def delete_conversation(
    sessionId: str,
    current_user: UserAccount = Depends(get_current_user),
    history_service: ChatHistoryService = Depends(get_chat_history_service),
) -> dict[str, bool]:
    """删除指定会话。采用逻辑删除，确保数据永不物理抹除。"""
    history_service.delete_conversation(sessionId, current_user.user_id)
    return {"deleted": True}


@router.put(
    "/{sessionId}/messages/{messageId}/feedback",
    summary="提交/更新对某条 AI 回复的用户反馈",
)
async def update_feedback(
    sessionId: str,
    messageId: str,
    req: Optional[FeedbackRequest] = None,
    current_user: UserAccount = Depends(get_current_user),
    history_service: ChatHistoryService = Depends(get_chat_history_service),
) -> dict[str, bool]:
    """点赞 / 点踩 / 取消反馈。校验消息与会话归属。"""
    fb = req.feedback if req else None
    history_service.update_feedback(sessionId, messageId, current_user.user_id, fb)
    return {"updated": True}
