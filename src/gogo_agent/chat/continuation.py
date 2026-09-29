"""020 活跃 Agent 续跑入口的纯选择规则；真实活跃状态装配属于 027。"""

from collections.abc import Collection
from enum import StrEnum
from typing import Protocol

from gogo_agent.intent.models import QueryInput
from gogo_agent.request_context import RequestContext


class TurnEntry(StrEnum):
    """一轮请求从新请求流水线或已验证活跃 Agent 续跑入口开始。"""

    FULL_PIPELINE = "full_pipeline"
    CONTINUE_ACTIVE = "continue_active"


class ActiveAgentContinuation(Protocol):
    """已鉴权会话的活跃 Agent 查询与续跑；实现方仍须校验其工具权限。"""

    @property
    def available_agents(self) -> Collection[str]: ...

    def get_active_agent(self, request: RequestContext) -> str | None: ...

    async def continue_turn(
        self, request: RequestContext, agent_name: str, query: QueryInput,
    ) -> str: ...


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
