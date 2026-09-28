"""016–017 真实网关与 Qdrant 的分层意图流水线断点演示。"""

import argparse
import asyncio
from datetime import date

from gogo_agent.chat.repository import InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.intent import (
    InMemoryWriteLedger,
    IntentCategory,
    OrderedMasterCoordinator,
)
from gogo_agent.intent.runtime import open_intent_pipeline


REFERENCE_DATE = date(2026, 9, 28)
CASES = {
    "l0": "帮我查一下差旅政策，并且报销这张发票",
    "l1": "帮我查机票",
    "l2": "我交上去那个流程现在走到哪一步了",
    "l3": "查订单并规划下一程",
    "rewrite": "那改成三天呢？",
}


class DemoChild:
    """只用于演示顺序与去重的假子 Agent，不连接真实差旅或订单工具。"""

    def __init__(self, name: str, *, writes: bool):
        self.name = name
        self.writes = writes
        self.calls = 0

    async def run(self, item, question, completed):
        self.calls += 1
        prior = f"，承接 {completed[-1].agent_name}" if completed else ""
        return f"仅模拟处理 {item.intent.value}{prior}"


async def show_case(case: str, history: ChatHistoryService, pipeline) -> None:
    """每个案例独立会话，方便在 prepare 和 matcher 上打断点。"""
    session_id = f"learning-016017-{case}"
    user_id = "demo-user"
    if case == "rewrite":
        history.save_user_message(
            session_id, user_id,
            "请规划2026年10月8日从北京去杭州的两天行程，包括交通和酒店。",
        )
        history.save_assistant_message(
            session_id, user_id,
            "教学用上一轮摘要：从北京去杭州，2026年10月8日出发，两天行程；尚未办理申请或预订。",
            agent_name="TeachingContext",
        )

    question = CASES[case]
    message_id = history.save_user_message(session_id, user_id, question)
    prepared = await pipeline.prepare(
        session_id, user_id, current_message_id=message_id,
        reference_date=REFERENCE_DATE,
    )
    print(f"\n[{case.upper()}] 原问题：{question}")
    print("  分支：", prepared.branch)
    print("  原句快筛：", [layer.value for layer in prepared.fast_decision.attempted_layers])
    print("  快筛原因：", prepared.fast_decision.reason)
    print("  快筛候选：", [
        (candidate.intent.value, None if candidate.score is None else round(candidate.score, 4))
        for candidate in prepared.fast_decision.candidates
    ])
    if prepared.rewrite is not None:
        print("  改写结果：", prepared.rewrite.rewritten_question)
    if prepared.decision is None:
        print("  需要补充：", prepared.rewrite.missing_context)
        return
    print("  最终命中层：", prepared.decision.hit_layer.value)
    print("  完整识别尝试：", [layer.value for layer in prepared.decision.attempted_layers])
    print("  有序意图：", [item.intent.value for item in prepared.decision.result.intents])
    print("  多意图：", prepared.decision.result.multi_intent)

    if case == "l3" and prepared.decision.result.multi_intent:
        manage = DemoChild("FakeManageAgent", writes=False)
        plan = DemoChild("FakePlanAgent", writes=True)
        master = OrderedMasterCoordinator({
            IntentCategory.TRAVEL_ORDER_QUERY: manage,
            IntentCategory.ITINERARY_PLANNING: plan,
        }, InMemoryWriteLedger())
        request = dict(user_id=user_id, session_id=session_id, request_id=message_id)
        result = prepared.decision.result
        effective = prepared.effective_question
        first = await master.execute(result, effective, **request)
        repeated = await master.execute(result, effective, **request)
        print("  假 Master 首次执行：", first.status, first.summary.replace("\n", "；"))
        print("  同请求再次执行：", repeated.status, "规划写调用次数=", plan.calls)


async def run(cases: list[str]) -> int:
    """只调用真实模型与意图 Qdrant；业务执行均为内存假 Agent。"""
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    print("固定参考日期：2026-09-28 / Asia/Shanghai")
    print("L0/L1/L2/L3 来自真实规则、Qdrant 与模型；业务子 Agent 仅作内存模拟。")
    try:
        async with open_intent_pipeline(history) as pipeline:
            for case in cases:
                await show_case(case, history, pipeline)
    except Exception as exc:
        code = getattr(exc, "status_code", None)
        print(f"演示失败：{type(exc).__name__}" + (f"，HTTP {code}" if code else ""))
        return 1
    return 0


def main() -> None:
    """可只跑一个分支以设置断点，默认依次展示四条路径。"""
    parser = argparse.ArgumentParser(description="调试 016–017 四层意图识别和假 Master 顺序执行")
    parser.add_argument(
        "--case", choices=("all", *CASES), default="all",
        help="只运行一个案例；默认 all 依次运行 L0、L1、L2、L3、改写",
    )
    args = parser.parse_args()
    cases = list(CASES) if args.case == "all" else [args.case]
    raise SystemExit(asyncio.run(run(cases)))


if __name__ == "__main__":
    main()
