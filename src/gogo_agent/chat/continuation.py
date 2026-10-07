"""027 已认证会话的活跃 InfoAgent 记录与精确续聊入口。"""

import asyncio
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Awaitable, Callable, Protocol

from agentscope.agent import Agent
from agentscope.event import ReplyFinishedReason
from agentscope.message import UserMsg
from agentscope.state import AgentState
from pydantic import ValidationError

from gogo_agent.intent.models import QueryInput
from gogo_agent.request_context import RequestContext

from .repository import ActiveAgentRecord, AgentSessionStoreProtocol


class TurnEntry(StrEnum):
    """一轮请求从新请求流水线或已验证活跃 Agent 续跑入口开始。"""

    FULL_PIPELINE = "full_pipeline"
    CONTINUE_ACTIVE = "continue_active"


@dataclass(frozen=True)
class ContinuationReply:
    """子 Agent 本轮可见文本及尚未提交的 AgentState。"""

    text: str
    state: AgentState | None = None


class ActiveAgentContinuation(Protocol):
    """已鉴权会话的活跃 Agent 查询与续跑；实现方仍须校验其工具权限。"""

    @property
    def available_agents(self) -> Collection[str]: ...

    def get_active_agent(self, request: RequestContext) -> str | None: ...

    async def continue_turn(
        self, request: RequestContext, agent_name: str, query: QueryInput,
    ) -> ContinuationReply: ...

    def record_completed_agent(self, request: RequestContext, agent_name: str) -> None: ...

    def clear_active(self, request: RequestContext) -> None: ...


# 与 Java ContinuationSignals 的明确续跑词对齐；匹配完整消息，不猜测自由文本。
_CONTINUATION_SIGNALS = frozenset({
    "确定", "确认", "提交", "继续", "是的", "好的", "对", "好",
    "修改", "补充", "不对", "取消", "重新", "再",
    "ok", "yes", "confirm", "continue", "修改一下", "补充一下", "重新来", "再来",
})


def choose_turn_entry(
    active_agent_name: str | None,
    message: str,
    available_agents: Collection[str],
) -> TurnEntry:
    """仅对存在且已注册的活跃 Agent 接受明确续跑信号；无效记录回完整入口。"""
    if (
        active_agent_name
        and active_agent_name in available_agents
        and message.lower() in _CONTINUATION_SIGNALS
    ):
        return TurnEntry.CONTINUE_ACTIVE
    return TurnEntry.FULL_PIPELINE


class InfoAgentContinuation:
    """只对已有 InfoAgent 状态和未过期的同用户会话直接续聊。"""

    available_agents = frozenset({"InfoAgent"})

    def __init__(
        self,
        session_store: AgentSessionStoreProtocol,
        build_agent: Callable[[RequestContext, AgentState], Agent],
        close_agent: Callable[[Agent], Awaitable[None]],
        state_session_id: Callable[[str, str], str],
        timeout_seconds: Callable[[], float],
        ttl: timedelta = timedelta(minutes=30),
    ) -> None:
        self._session_store = session_store
        self._build_agent = build_agent
        self._close_agent = close_agent
        self._state_session_id = state_session_id
        self._timeout_seconds = timeout_seconds
        self._ttl = ttl

    def _key(self, request: RequestContext) -> str:
        return self._state_session_id(request.session_id, request.user_id)

    def get_active_agent(self, request: RequestContext) -> str | None:
        key = self._key(request)
        try:
            record = self._session_store.load_active_agent(key)
        except (ValidationError, ValueError, TypeError):
            # 持久化内容损坏时安全降级到完整流水线，不把旧路由当作权限依据。
            try:
                self._session_store.delete_active_agent(key)
            except Exception:
                pass
            return None
        if record is None:
            return None
        if (
            record.agent_name not in self.available_agents
            or record.expires_at <= datetime.now(timezone.utc)
            or self._session_store.load_agent_state(key, agent_name=record.agent_name) is None
        ):
            self._session_store.delete_active_agent(key)
            return None
        return record.agent_name

    def record_completed_agent(self, request: RequestContext, agent_name: str) -> None:
        if agent_name not in self.available_agents:
            raise ValueError("不能保存未注册的活跃 Agent")
        key = self._key(request)
        if self._session_store.load_agent_state(key, agent_name=agent_name) is None:
            raise ValueError("活跃 Agent 缺少可恢复状态")
        self._session_store.save_active_agent(key, ActiveAgentRecord(
            agent_name=agent_name,
            expires_at=datetime.now(timezone.utc) + self._ttl,
        ))

    def clear_active(self, request: RequestContext) -> None:
        self._session_store.delete_active_agent(self._key(request))

    async def continue_turn(
        self, request: RequestContext, agent_name: str, query: QueryInput,
    ) -> ContinuationReply:
        if agent_name not in self.available_agents:
            raise ValueError("不能续跑未注册的 Agent")
        key = self._key(request)
        state = self._session_store.load_agent_state(key, agent_name=agent_name)
        if state is None:
            raise ValueError("活跃 Agent 状态已丢失")
        agent = self._build_agent(request, state)
        try:
            async with asyncio.timeout(self._timeout_seconds()) as deadline:
                reply = await agent.reply(UserMsg(name="user", content=query.question))
            if deadline.expired():
                raise TimeoutError("InfoAgent 续跑超时")
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError
            if reply.finished_reason != ReplyFinishedReason.COMPLETED:
                raise RuntimeError(f"InfoAgent finished with {reply.finished_reason}")
            answer = reply.get_text_content() or ""
            if not answer.strip():
                raise ValueError("信息子 Agent 没有返回可用回答")
            return ContinuationReply(answer, agent.state)
        finally:
            await self._close_agent(agent)
