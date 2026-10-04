"""022 离线断点演示：两个请求交错和后台任务的异步上下文边界。"""

import asyncio
import json
import logging
import sys
from contextvars import Context, copy_context

from gogo_agent.intent import (
    ConfidenceLevel, InMemoryWriteLedger, IntentCategory, IntentItem,
    IntentResult, OrderedMasterCoordinator, QueryInput,
)
from gogo_agent.request_context import RequestContext, current_trace_id, trace_scope


logger = logging.getLogger("demo_022")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("  日志 %(message)s trace_id=%(trace_id)s"))
logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


class FakeTool:
    """用显式用户与会话键模拟工具写后读，忽略消息自报的 userId。"""

    def __init__(self):
        self.rows: dict[tuple[str, str], str] = {}
        self.arrived = 0
        self.both_arrived = asyncio.Event()

    async def write_and_read(self, request: RequestContext, untrusted: dict) -> str:
        key = (request.user_id, request.session_id)
        self.rows[key] = request.request_id
        self.arrived += 1
        if self.arrived == 2:
            self.both_arrived.set()
        await self.both_arrived.wait()  # 断点：两个请求已交错，继续观察各自的键。
        await asyncio.sleep(0)
        assert self.rows[key] == request.request_id
        logger.info("假工具读写完成", extra={"trace_id": current_trace_id()})
        return f"可信用户={request.user_id}，会话={request.session_id}，消息声称={untrusted['userId']}"


class FakeChild:
    """023 之前只模拟子 Agent；身份必须由参数传入。"""

    name = "FakeInfoAgent"
    writes = False

    def __init__(self, tool: FakeTool):
        self.tool = tool

    async def run(self, item, question, context: RequestContext, completed):
        assert current_trace_id() == context.trace_id
        return await self.tool.write_and_read(context, json.loads(question.question))


def fixed_intent(message: str) -> IntentResult:
    """用固定意图隔离外部模型，仅观察异步上下文。"""
    return IntentResult(
        intents=[IntentItem(
            intent=IntentCategory.GENERAL_INFO,
            confidence=ConfidenceLevel.HIGH,
            reason="022 固定意图",
            evidence=[message],
        )],
        primary_intent=IntentCategory.GENERAL_INFO,
        multi_intent=False,
        overall_reason="022 异步隔离演示",
    )


async def show_concurrent() -> RequestContext:
    """交错运行两份可信参数，展示日志和假工具只读取本请求。"""
    tool = FakeTool()
    coordinator = OrderedMasterCoordinator({
        IntentCategory.GENERAL_INFO: FakeChild(tool),
    }, InMemoryWriteLedger())

    async def request_turn(user_id: str, session_id: str, forged_user: str) -> RequestContext:
        request = RequestContext(
            user_id=user_id, session_id=session_id, request_id=f"msg-{user_id}",
        )
        message = json.dumps({"userId": forged_user, "question": "查资料"})
        with trace_scope(request):
            logger.info("请求开始", extra={"trace_id": current_trace_id()})
            report = await coordinator.execute(
                fixed_intent(message), QueryInput(question=message), context=request,
            )
            assert report.trace_id == request.trace_id
            assert report.steps[0].trace_id == request.trace_id
            print(f"  {user_id} → {report.steps[0].output}")
        assert current_trace_id() is None
        return request

    print("[两个异步请求交错：可在 FakeTool.write_and_read 的 await 前后打断点]")
    alice, bob = await asyncio.gather(
        request_turn("u001", "s-alice", "u002"),
        request_turn("u002", "s-bob", "u001"),
    )
    assert alice.trace_id != bob.trace_id
    assert set(tool.rows) == {("u001", "s-alice"), ("u002", "s-bob")}
    return alice


async def show_background(request: RequestContext) -> None:
    """观察默认任务复制追踪值，以及用空 Context 创建独立后台任务。"""
    release = asyncio.Event()

    async def job(name: str) -> None:
        await release.wait()  # 断点：父请求退出后再读取子任务上下文。
        values = tuple(copy_context().values())
        assert not any(isinstance(value, RequestContext) for value in values)
        print(f"  {name}：trace_id={current_trace_id()}，隐式 RequestContext 对象=无")

    print("\n[父请求已结束后，后台任务读取各自创建时的上下文]")
    with trace_scope(request):
        inherited = asyncio.create_task(job("默认 create_task"))
        detached = asyncio.create_task(job("context=Context()"), context=Context())
    assert current_trace_id() is None
    release.set()
    await asyncio.gather(inherited, detached)
    print("  生产代码尚无后台任务；后续独立任务应显式选择空上下文与所需参数。")


async def main() -> None:
    alice = await show_concurrent()
    await show_background(alice)
    print("\n本演示使用真实 RequestContext、trace_scope 和顺序协调器；子 Agent、工具、意图结果均为内存替身。")


if __name__ == "__main__":
    asyncio.run(main())
