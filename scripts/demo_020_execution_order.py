"""020：像调试 Java 聊天链路一样，离线走通一轮 Python Agent 请求。

逐语句断点表、四个场景的完整事件解释和练习答案见
docs/契约样例/020-执行顺序与续跑边界.md 的「Java 工程师逐步调试」一节。

先从文件底部的 main() 看起，再进 run()，最后才看上面的替身类：
main() 解析 --case；run() 手动组装仓储、流水线和执行器；execute_turn()
根据活跃 Agent 状态选择「直接续跑」或「准备意图 → GoGo 回复」。

Java 基线工程中的相近入口（路径相对于 Java 工程根目录）：
- src/main/java/com/gogo/travel/controller/ChatController.java 的 chat()：保存消息并判断续跑。
- src/main/java/com/gogo/travel/agent/service/ChatAgentExecutor.java 的 executeAgent()：
  根据传入的 Agent 选择完整流水线或继续调用业务 Agent。
- src/main/java/com/gogo/travel/agent/service/AgentPipelineService.java 的
  executeFullPipeline()：先按原句快筛，未命中再改写和识别。
本脚本直接调用 Python ChatAgentExecutor.execute_turn()，不经过 HTTP 鉴权；
Python IntentPipelineService.prepare() 只准备意图结果，后续 GoGo 回复由执行器负责。
Java 的 Mono 在订阅时运行；这里 asyncio.run() 启动事件循环，await 等待当前
异步步骤完成后再往下走。两者都处理异步调用，但调度和返回值语义并不相同。

从仓库根目录运行（不需要外部模型密钥或数据库）：
    PYTHONPATH=src .venv/bin/python scripts/demo_020_execution_order.py --case new
    PYTHONPATH=src .venv/bin/python scripts/demo_020_execution_order.py --case same
    PYTHONPATH=src .venv/bin/python scripts/demo_020_execution_order.py --case continue
    PYTHONPATH=src .venv/bin/python scripts/demo_020_execution_order.py --case fallback
    PYTHONPATH=src .venv/bin/python scripts/demo_020_execution_order.py --case all
same、continue、fallback 会先在同一个内存会话中执行必要的前置轮次，
只打印所选场景的事件；all 按顺序打印四种场景。
关键输出：new 的 state_context_messages=0；same 恢复后为 2；continue 有
active_agent_replied 而没有 rule_checked；fallback 则重新出现 rule_checked。
all 的后续场景复用前面几轮留下的内存状态，上下文消息数会随之增加。

按下面的顺序打断点，并单步观察（函数名比固定行号更不容易因编辑而失效）：
1. run() 创建 history 和 states：它们像 Java 测试中手动装配的 Repository/Store；
   看 executor._history_service is active_executor._history_service 和两个执行器的
   _session_store 是否同一对象。
2. ChatAgentExecutor.execute_turn()：在 _save_user_turn() 返回后看
   request.session_id、request.request_id；在 _active_agent_for_turn() 返回后看
   active_name，确认先保存用户消息再选入口。
3. run() 里的 pipeline_factory()：只在完整流水线分支进入；看三个依赖如何装入
   IntentPipelineService，以及 yield 后谁接到 pipeline。它类似 Java 注入一个工厂，
   async with 类似资源作用域；这里 yield 后没有关闭动作，生产工厂才负责关闭客户端。
4. IntentPipelineService.prepare()：看 context.history、fast.result、rewrite；
   返回 _prepare_turn() 后看 prepared.branch。new 会出现
   rule_checked → rewrite_called → rule_checked → l3_model。
5. FakeActiveAgent.continue_turn() 或 show()：continue 直接回复，跳过第 3、4 步；
   same 会恢复已有 GoGo AgentState，fallback 则重新进入第 3、4 步。

动手自检：先预测 --case continue 和 --case fallback 哪个会出现 history_loaded、
rule_checked、state_saved，再分别运行核对；解释 events.clear() 为什么只清掉观察日志，
不会清掉内存中的会话历史和 AgentState。

真实边界：执行器、意图流水线、历史构造和规则匹配走项目代码；历史和状态用内存存储，
改写与 L3 模型返回固定结果，GoGo 供应商模型与活跃 PlanAgent 使用演示替身。
因此这里能验证调用顺序和分支，不能验证 HTTP 鉴权、真实模型判断或业务 Agent 效果。
events 像 Java 测试里的 List<String> 调用轨迹；有些事件在 super() 写入前就记录，
所以单条事件只是「进入调用边界」，要结合后续状态和完整运行结果判断写入。
"""

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

    def _build_agent(self, state, prepared, request, tool_calls, stream=False):
        self.events.append("coordinator_built")
        self.events.append(f"state_context_messages={len(state.context)}")
        agent = super()._build_agent(state, prepared, request, tool_calls, stream=stream)
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
    """使用一份内存历史依次展示状态变化。

    带着问题单步执行：新请求、同会话第二轮、有效续跑、失效活跃 Agent
    各自会执行或跳过哪些步骤？助手消息和 AgentState 何时保存？
    """
    events: list[str] = []
    history = RecordingHistoryService(RecordingHistoryRepository(events), events)
    states = RecordingStateStore(events)

    # Java 视角：这是注入 Executor 的工厂，不是此处立刻执行的 Service 方法。
    # Executor 在 _prepare_turn() 中进入 async with 时，才运行下面的函数体。
    @asynccontextmanager
    async def pipeline_factory(history_service):
        # 与 Java 构造器注入相似：显式组装三个依赖，方便分别查看它们的值。
        context_builder = RewriteContextBuilder(history_service)
        rewriter = FixedRewriter(events)
        recognizer = IntentRecognizer(
            FixedIntentModel(events), rule_matcher=RecordingRule(events),
        )
        pipeline = IntentPipelineService(context_builder, rewriter, recognizer)
        # yield 把对象交给 async with ... as pipeline；识别发生在随后的 prepare()。
        yield pipeline

    executor = OfflineExecutor(history, states, pipeline_factory=pipeline_factory, events=events)
    active = FakeActiveAgent(events)
    active_executor = OfflineExecutor(
        history, states, pipeline_factory=pipeline_factory,
        active_continuation=active, events=events,
    )

    # 每个场景都先走这一轮。same/continue/fallback 调试时首次命中 factory
    # 属于准备会话；只有目标轮次的输出才会由 show() 打印。
    if selected in ("same", "continue", "fallback"):
        print("准备轮次：先跑一轮普通请求建立内存会话；此轮断点会命中，事件不展示。")
    await executor.execute_turn("learning-020", "demo-user", QUESTION)
    if selected in ("all", "new"):
        show("新请求：保存 → 历史 → 识别 → GoGo → 回复/状态保存", events, history)
    if selected == "new":
        return

    # 只清空本轮观察日志；history 与 states 仍保存第一轮，供下一轮恢复。
    events.clear()
    if selected in ("all", "same"):
        await executor.execute_turn("learning-020", "demo-user", QUESTION)
        show("同会话第二轮：仍识别，但恢复 GoGo AgentState", events, history)
        events.clear()
    if selected == "same":
        return

    # fallback 还会先走一次有效续跑；它写入业务历史，不更新 GoGo AgentState。
    if selected == "fallback":
        print("准备轮次：先让 PlanAgent 有效续跑一次；此轮断点会命中，事件不展示。")
    await active_executor.execute_turn("learning-020", "demo-user", "继续")
    if selected in ("all", "continue"):
        show("有效活跃状态 + 完整续跑词：直接续跑", events, history)
    if selected == "continue":
        return

    # 模拟旧路由状态：该名称不在 available_agents 中，应回到完整流水线。
    active.active_name = "StaleAgent"
    events.clear()
    await active_executor.execute_turn("learning-020", "demo-user", QUESTION)
    show("无效活跃状态：回完整新请求流水线", events, history)


def main() -> None:
    """按场景运行，适合在 execute_turn、prepare 和 continue_turn 打断点。"""
    parser = argparse.ArgumentParser(description="离线调试 020 的执行顺序与活跃 Agent 续跑")
    parser.add_argument("--case", choices=("all", "new", "same", "continue", "fallback"), default="all")
    args = parser.parse_args()
    print("只用内存仓储与固定模型；此脚本演示 020 顺序，023 只读信息子 Agent 与 027 活跃 Agent 续跑另行验收。")
    asyncio.run(run(args.case))


if __name__ == "__main__":
    main()
