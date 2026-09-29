"""016 供 Master 委派的顺序执行与可复现的假子 Agent 验收边界。"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from gogo_agent.request_context import RequestContext

from .models import IntentCategory, IntentItem, IntentResult, QueryInput


class IntentExecutionStep(BaseModel):
    """一个按识别顺序执行的子任务及其来源、结果和失败状态。"""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(..., ge=0, description="事项在有序意图列表中的零基位置")
    trace_id: str = Field(..., min_length=1, description="本轮服务端生成的追踪标识，与子 Agent 调用关联")
    intent: IntentCategory = Field(..., description="当前事项的意图类别")
    agent_name: str = Field(..., min_length=1, description="实际负责或预计负责该事项的子 Agent 名称")
    status: Literal["completed", "failed", "skipped"] = Field(..., description="事项已完成、失败或因前项失败跳过")
    output: str | None = Field(..., description="子 Agent 已完成事项的实际文字结果，未完成时为空")
    error_type: str | None = Field(..., description="失败的异常类型或未实现原因，不回显业务内容与凭证")
    reused: bool = Field(default=False, description="写事项是否复用了相同请求键的已有结果")


class IntentExecutionReport(BaseModel):
    """一次多意图执行的有序聚合，不把部分成功表述为全部完成。"""

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(..., min_length=1, description="整轮请求的服务端追踪标识")
    status: Literal["completed", "partial", "failed"] = Field(..., description="整轮全部完成、部分完成或全部失败")
    steps: list[IntentExecutionStep] = Field(..., min_length=1, description="按原有 intents 顺序排列的每项执行结果")
    summary: str = Field(..., min_length=1, description="含明确失败范围的聚合结果文字")


class ChildIntentAgent(Protocol):
    """由 Master 委派的一个已装配子 Agent；当前仅用于假 Agent 验收。"""

    name: str
    writes: bool

    async def run(
        self,
        item: IntentItem,
        question: QueryInput,
        context: RequestContext,
        completed: tuple[IntentExecutionStep, ...],
    ) -> str: ...


class InMemoryWriteLedger:
    """教学用同进程写调用去重表，真实业务仍需工具层持久化幂等。"""

    def __init__(self) -> None:
        self._locks_guard = asyncio.Lock()
        self._locks: dict[tuple[str, str, str, int, IntentCategory], asyncio.Lock] = {}
        self._steps: dict[tuple[str, str, str, int, IntentCategory], IntentExecutionStep] = {}

    async def run_once(
        self,
        key: tuple[str, str, str, int, IntentCategory],
        operation: Callable[[], Awaitable[IntentExecutionStep]],
        pending: IntentExecutionStep,
    ) -> IntentExecutionStep:
        """先记未知状态再执行；取消或失败后也不盲目重复写调用。"""
        async with self._locks_guard:
            lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._steps:
                return self._steps[key].model_copy(update={
                    "reused": True,
                    "trace_id": pending.trace_id,
                })
            self._steps[key] = pending
            step = await operation()
            self._steps[key] = step
            return step


class OrderedMasterCoordinator:
    """按 L3 给出的 intents 顺序委派，失败即停并保留已完成结果。"""

    def __init__(
        self,
        children: Mapping[IntentCategory, ChildIntentAgent],
        write_ledger: InMemoryWriteLedger,
    ) -> None:
        self._children = children
        self._write_ledger = write_ledger

    async def execute(
        self,
        result: IntentResult,
        question: QueryInput,
        *,
        context: RequestContext,
    ) -> IntentExecutionReport:
        """用服务端可信标识去重写调用，并把后续依赖失败标成跳过。"""
        if not isinstance(context, RequestContext):
            raise TypeError("顺序执行必须接收服务端 RequestContext")
        steps: list[IntentExecutionStep] = []
        for index, item in enumerate(result.intents):
            child = self._children.get(item.intent)
            if child is None:
                steps.append(IntentExecutionStep(
                    index=index, trace_id=context.trace_id,
                    intent=item.intent, agent_name="未装配",
                    status="failed", output=None, error_type="AgentUnavailable",
                ))
                break

            async def invoke() -> IntentExecutionStep:
                try:
                    output = await child.run(item, question, context, tuple(steps))
                    if not isinstance(output, str) or not output.strip():
                        raise ValueError("子 Agent 未提供可确认的完成结果")
                except Exception as exc:
                    return IntentExecutionStep(
                        index=index, trace_id=context.trace_id,
                        intent=item.intent, agent_name=child.name,
                        status="failed", output=None, error_type=type(exc).__name__,
                    )
                return IntentExecutionStep(
                    index=index, trace_id=context.trace_id,
                    intent=item.intent, agent_name=child.name,
                    status="completed", output=output, error_type=None,
                )

            if child.writes:
                key = (context.user_id, context.session_id, context.request_id, index, item.intent)
                pending = IntentExecutionStep(
                    index=index, trace_id=context.trace_id,
                    intent=item.intent, agent_name=child.name,
                    status="failed", output=None, error_type="OutcomeUnknown",
                )
                step = await self._write_ledger.run_once(key, invoke, pending)
            else:
                step = await invoke()
            steps.append(step)
            if step.status != "completed":
                break

        for index in range(len(steps), len(result.intents)):
            item = result.intents[index]
            child = self._children.get(item.intent)
            steps.append(IntentExecutionStep(
                index=index, trace_id=context.trace_id,
                intent=item.intent,
                agent_name=child.name if child else "未装配",
                status="skipped", output=None, error_type="PreviousStepFailed",
            ))

        completed = [step for step in steps if step.status == "completed"]
        status: Literal["completed", "partial", "failed"] = (
            "completed" if len(completed) == len(steps) else "partial" if completed else "failed"
        )
        lines = [f"{step.index + 1}. {step.agent_name}：{step.output}" for step in completed]
        if status != "completed":
            first_unfinished = next(step for step in steps if step.status != "completed")
            lines.append(f"第 {first_unfinished.index + 1} 项未完成；后续事项未执行。")
        return IntentExecutionReport(
            trace_id=context.trace_id,
            status=status, steps=steps, summary="\n".join(lines),
        )
