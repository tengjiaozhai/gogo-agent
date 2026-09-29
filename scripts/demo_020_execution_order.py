"""020 离线观察新请求、同会话恢复、活跃 Agent 续跑和无效状态回退。"""

import argparse
import asyncio
import json
from contextlib import asynccontextmanager

from agentscope.credential import DeepSeekCredential
from agentscope.message import TextBlock
from agentscope.model import ChatModelBase, ChatResponse

from gogo_agent.chat.executor import ChatAgentExecutor, _FallbackMockModel
from gogo_agent.chat.repository import InMemoryAgentSessionStore, InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.intent import (
    ConfidenceLevel, IntentCategory, IntentItem, IntentPipelineService,
    IntentRecognizer, IntentResult, IntentRuleMatcher, RewriteContextBuilder,
    RewriteResult,
)


QUESTION = "查订单并规划下一程"


class RecordingHistoryRepository(InMemoryChatHistoryRepository):
    """保留真实内存存储，只在三个写入边界记录事件。"""

    def __init__(self, events: list[str]):
        super().__init__()
        self.events = events

    def save_conversation(self, conversation):
        self.events.append("conversation_created")
        return super().save_conversation(conversation)

    def update_title(self, conversation_id, title):
        self.events.append("title_updated")
        return super().update_title(conversation_id, title)

    def save_message(self, message):
        self.events.append("user_saved" if message.role == "user" else "assistant_saved")
        return super().save_message(message)


class RecordingHistoryService(ChatHistoryService):
    """在真实归属校验和历史读取前记录时间点。"""

    def __init__(self, repository, events: list[str]):
        super().__init__(repository=repository)
        self.events = events

    def get_history_messages(self, conversation_id, user_id, *, limit=None):
        self.events.append("history_loaded")
        return super().get_history_messages(conversation_id, user_id, limit=limit)


class RecordingStateStore(InMemoryAgentSessionStore):
    """记录 GoGo AgentState 恢复和保存顺序。"""

    def __init__(self, events: list[str]):
        super().__init__()
        self.events = events

    def load_agent_state(self, session_id, agent_name="GoGo"):
        self.events.append("state_loaded")
        return super().load_agent_state(session_id, agent_name=agent_name)

    def save_agent_state(self, session_id, state, agent_name="GoGo"):
        self.events.append("state_saved")
        return super().save_agent_state(session_id, state, agent_name=agent_name)


class FixedIntentModel(ChatModelBase):
    """只替换 L3 供应商响应；IntentRecognizer 仍执行真实解析。"""

    def __init__(self, events: list[str]):
        super().__init__(
            credential=DeepSeekCredential(api_key="offline-demo"), model="fixed-intent",
            parameters=self.Parameters(), stream=False, max_retries=0,
        )
        self.events = events

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.events.append("l3_model")
        result = IntentResult(
            intents=[
                IntentItem(intent=category, confidence=ConfidenceLevel.HIGH,
                           reason="020 固定双意图", evidence=[QUESTION])
                for category in (IntentCategory.TRAVEL_ORDER_QUERY, IntentCategory.ITINERARY_PLANNING)
            ],
            primary_intent=IntentCategory.ITINERARY_PLANNING,
            multi_intent=True,
            overall_reason="先查询订单，再规划下一程",
        )
        return ChatResponse(content=[TextBlock(text=result.model_dump_json())], is_last=True)


class RecordingRule:
    """调用真实规则，记录 L0/L1 检查。"""

    def __init__(self, events: list[str]):
        self.events = events

    def match(self, query):
        self.events.append("rule_checked")
        return IntentRuleMatcher().match(query)


class FixedRewriter:
    """复合原句不含指代，固定保留原句以观察条件改写分支。"""

    def __init__(self, events: list[str]):
        self.events = events

    async def rewrite(self, context):
        self.events.append("rewrite_called")
        return RewriteResult(
            related=False, rewritten_question=context.query.question,
            reason="020 离线演示保留原句", evidence=[context.query.question],
            missing_context=[],
        )


class OfflineExecutor(ChatAgentExecutor):
    """执行真实 ChatAgentExecutor 路径，只把 GoGo 供应商模型换成本地模型。"""

    def __init__(self, *args, events: list[str], **kwargs):
        super().__init__(*args, **kwargs)
        self.events = events

    def _build_model(self, stream=False):
        return _FallbackMockModel(stream=stream)

    def _build_agent(self, state, prepared, stream=False):
        self.events.append("coordinator_built")
        self.events.append(f"state_context_messages={len(state.context)}")
        agent = super()._build_agent(state, prepared, stream=stream)
        reply = agent.reply

        async def record_reply(*args, **kwargs):
            self.events.append("gogo_replied")
            return await reply(*args, **kwargs)

        agent.reply = record_reply
        return agent


class FakeActiveAgent:
    """027 尚未装配真实活跃 Agent，本演示只注入接口替身。"""

    available_agents = frozenset({"PlanAgent"})

    def __init__(self, events: list[str]):
        self.events = events
        self.active_name = "PlanAgent"

    def get_active_agent(self, request):
        self.events.append("active_lookup")
        return self.active_name

    async def continue_turn(self, request, agent_name, query):
        self.events.append("active_agent_replied")
        return f"{agent_name} 已继续处理：{query.question}"


def show(label: str, events: list[str], history: ChatHistoryService) -> None:
    """打印可逐行对照 020 序列图的真实执行事件和回复元数据。"""
    latest = history.repository.find_messages_by_conversation_id("learning-020")[-1]
    extra = json.loads(latest.extra)
    print(f"\n[{label}]")
    for index, event in enumerate(events, 1):
        print(f"  {index:02d}. {event}")
    print("  回复 Agent：", latest.agent_name)
    print("  请求 ID：", extra.get("request_id") or extra["intent_pipeline"]["request_id"])
    print("  追踪 ID：", extra.get("trace_id") or extra["intent_pipeline"]["trace_id"])


async def run(selected: str) -> None:
    """使用一份内存历史依次展示状态从新请求到续跑的变化。"""
    events: list[str] = []
    history = RecordingHistoryService(RecordingHistoryRepository(events), events)
    states = RecordingStateStore(events)

    @asynccontextmanager
    async def pipeline_factory(history_service):
        yield IntentPipelineService(
            RewriteContextBuilder(history_service), FixedRewriter(events),
            IntentRecognizer(FixedIntentModel(events), rule_matcher=RecordingRule(events)),
        )

    executor = OfflineExecutor(history, states, pipeline_factory=pipeline_factory, events=events)
    active = FakeActiveAgent(events)
    active_executor = OfflineExecutor(
        history, states, pipeline_factory=pipeline_factory,
        active_continuation=active, events=events,
    )

    await executor.execute_turn("learning-020", "demo-user", QUESTION)
    if selected in ("all", "new"):
        show("新请求：保存 → 历史 → 识别 → GoGo → 回复/状态保存", events, history)
    if selected == "new":
        return

    events.clear()
    if selected in ("all", "same"):
        await executor.execute_turn("learning-020", "demo-user", QUESTION)
        show("同会话第二轮：仍识别，但恢复 GoGo AgentState", events, history)
        events.clear()
    if selected == "same":
        return

    await active_executor.execute_turn("learning-020", "demo-user", "继续")
    if selected in ("all", "continue"):
        show("有效活跃状态 + 完整续跑词：直接续跑", events, history)
    if selected == "continue":
        return

    active.active_name = "StaleAgent"
    events.clear()
    await active_executor.execute_turn("learning-020", "demo-user", QUESTION)
    show("无效活跃状态：回完整新请求流水线", events, history)


def main() -> None:
    """按场景运行，适合在 execute_turn、prepare 和 continue_turn 打断点。"""
    parser = argparse.ArgumentParser(description="离线调试 020 的执行顺序与活跃 Agent 续跑")
    parser.add_argument("--case", choices=("all", "new", "same", "continue", "fallback"), default="all")
    args = parser.parse_args()
    print("只用内存仓储与固定模型；真实业务子 Agent 仍待 023/027。")
    asyncio.run(run(args.case))


if __name__ == "__main__":
    main()
