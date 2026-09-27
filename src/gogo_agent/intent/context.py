"""从有归属校验的业务历史构造改写上下文。"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from fastapi import HTTPException

if TYPE_CHECKING:
    from gogo_agent.chat.service import ChatHistoryService

from .models import HistoryMessage, QueryInput, RewriteContext


class RewriteContextBuilder:
    """读取最近十条历史并限制其字符量，当前问题须已由聊天服务保存。"""

    RECENT_MESSAGE_LIMIT = 10

    def __init__(self, history_service: ChatHistoryService, *, max_history_chars: int = 8000):
        if max_history_chars < 1:
            raise ValueError("历史字符预算必须大于零")
        self._history_service = history_service
        self._max_history_chars = max_history_chars

    def build_rewrite_context(
        self,
        session_id: str,
        user_id: str,
        *,
        current_message_id: str | None = None,
        reference_date: date | None = None,
    ) -> RewriteContext:
        """鉴权后提取末尾当前消息，保留此前历史；可用消息 ID 防止接错本轮问题。"""
        messages = self._history_service.get_history_messages(
            session_id, user_id, limit=self.RECENT_MESSAGE_LIMIT + 1
        )
        if not messages:
            raise HTTPException(409, "请先保存本轮用户消息，再构造改写上下文")
        current = messages[-1]
        if current_message_id is not None:
            # 同时刻记录按 ID 稳定排序，但随机 ID 不代表写入先后；显式 ID 标识本轮问题。
            current = next((message for message in messages if message.message_id == current_message_id), None)
            if current is None or current.created_at != messages[-1].created_at:
                raise HTTPException(409, "当前消息不在最新时间窗口，不能使用此历史快照")
        if current.role != "user":
            raise HTTPException(409, "请先保存本轮用户消息，再构造改写上下文")
        query = QueryInput(question=current.content)

        history: list[HistoryMessage] = []
        remaining = self._max_history_chars
        truncated = False
        for message in reversed([message for message in messages if message.message_id != current.message_id]):
            content = (message.content or "").strip()
            if not content:
                continue
            if remaining == 0:
                truncated = True
                break
            clipped = len(content) > remaining
            history.append(HistoryMessage(
                role=message.role,
                content=content[-remaining:] if clipped else content,
                truncated=clipped,
            ))
            remaining -= len(history[-1].content)
            truncated = truncated or clipped
        history.reverse()
        return RewriteContext(
            query=query,
            history=history,
            reference_date=reference_date or datetime.now(ZoneInfo("Asia/Shanghai")).date(),
            history_truncated=truncated,
        )
