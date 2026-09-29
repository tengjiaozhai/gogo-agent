"""021 离线演示：伪造 userId 不覆盖认证身份，多个子步骤共用 trace_id。"""

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi.testclient import TestClient

from gogo_agent.api import app
from gogo_agent.auth.dependencies import get_auth_service
from gogo_agent.auth.repository import InMemoryUserAccountRepository
from gogo_agent.auth.security import InMemoryTokenStore, TokenManager
from gogo_agent.auth.service import AuthService
from gogo_agent.chat.dependencies import get_chat_executor, get_chat_history_service
from gogo_agent.chat.executor import ChatAgentExecutor, _FallbackMockModel
from gogo_agent.chat.repository import InMemoryAgentSessionStore, InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.intent import (
    ConfidenceLevel, FastMatch, InMemoryWriteLedger, IntentCandidate,
    IntentCategory, IntentItem, IntentPipelineService, IntentRecognizer,
    IntentResult, MatchStatus, OrderedMasterCoordinator, QueryInput,
    RecognitionLayer, RewriteContextBuilder,
)
from gogo_agent.request_context import RequestContext


class FixedRule:
    """让 HTTP 教学请求离线命中 L1，保留实际流水线的历史归属校验。"""

    def match(self, query: QueryInput) -> FastMatch:
        result = IntentResult(
            intents=[IntentItem(
                intent=IntentCategory.POLICY_QUERY, confidence=ConfidenceLevel.HIGH,
                reason="021 固定分类", evidence=[query.question],
            )],
            primary_intent=IntentCategory.POLICY_QUERY,
            multi_intent=False,
            overall_reason="021 固定分类，不执行外部模型",
        )
        return FastMatch(
            status=MatchStatus.HIT, result=result, threshold=None,
            candidates=[IntentCandidate(
                intent=IntentCategory.POLICY_QUERY, layer=RecognitionLayer.RULE,
                score=None, reason="021 离线规则",
            )],
            reason="021 离线规则命中",
        )


class UnusedRewriter:
    """固定规则命中时不会调用改写器。"""

    async def rewrite(self, context):
        raise AssertionError("021 演示不应调用改写器")


class RecordingPipeline(IntentPipelineService):
    """记录执行器传来的原始上下文，然后运行真实预处理。"""

    def __init__(self, history: ChatHistoryService, captured: list[RequestContext]):
        super().__init__(
            RewriteContextBuilder(history), UnusedRewriter(),
            IntentRecognizer(_FallbackMockModel(), rule_matcher=FixedRule()),
        )
        self.captured = captured

    async def prepare(self, request: RequestContext, *, reference_date=None):
        self.captured.append(request)
        return await super().prepare(request, reference_date=reference_date)


class OfflineExecutor(ChatAgentExecutor):
    """HTTP GoGo 只用本地模型，不读取环境中的网关凭证。"""

    def _build_model(self, stream=False):
        return _FallbackMockModel(stream=stream)


class FakeProfileTool:
    """假资料工具按可信上下文定位用户；不可信工具参数不能选择账户。"""

    def read(self, request: RequestContext, untrusted_arguments: dict) -> str:
        print("    模拟工具参数声称 userId =", untrusted_arguments.get("userId"))
        print("    工具实际使用 context.user_id =", request.user_id)
        return {"u001": "Alice 的资料", "u002": "Bob 的资料"}[request.user_id]


class FakeChild:
    """只演示 016 协调接口中的显式上下文传递，不代表生产业务 Agent。"""

    writes = False

    def __init__(self, name: str, tool: FakeProfileTool):
        self.name = name
        self.tool = tool

    async def run(self, item, question, context: RequestContext, completed):
        print(f"  子 Agent {self.name}：trace_id = {context.trace_id}，此前完成 {len(completed)} 项")
        # 用用户消息模拟模型可能传来的工具参数；本演示没有真实 LLM 工具调用。
        untrusted_arguments = json.loads(question.question)
        return self.tool.read(context, untrusted_arguments)


def fixed_two_intents(message: str) -> IntentResult:
    """让假协调器执行两个有序事项，观察同一上下文跨步骤传递。"""
    return IntentResult(
        intents=[
            IntentItem(intent=category, confidence=ConfidenceLevel.HIGH,
                       reason="021 固定事项", evidence=[message])
            for category in (IntentCategory.GENERAL_INFO, IntentCategory.POLICY_QUERY)
        ],
        primary_intent=IntentCategory.GENERAL_INFO,
        multi_intent=True,
        overall_reason="先资料查询，再政策查询",
    )


def run() -> None:
    """先通过本地 FastAPI 生成可信上下文，再把它传给假子 Agent 和工具。"""
    auth = AuthService(
        repository=InMemoryUserAccountRepository(),
        token_manager=TokenManager(store=InMemoryTokenStore()),
    )
    alice_token = auth.login("alice", "123456")
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    captured: list[RequestContext] = []

    @asynccontextmanager
    async def pipeline_factory(history_service):
        yield RecordingPipeline(history_service, captured)

    executor = OfflineExecutor(
        history, InMemoryAgentSessionStore(), pipeline_factory=pipeline_factory,
    )
    previous_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_auth_service] = lambda: auth
    app.dependency_overrides[get_chat_executor] = lambda: executor
    app.dependency_overrides[get_chat_history_service] = lambda: history
    forged_message = json.dumps({
        "userId": "u002", "planReference": "foreign-plan", "question": "查差旅政策",
    }, ensure_ascii=False)
    try:
        response = TestClient(app).post(
            "/api/chat/learning-021",
            headers={"Authorization": alice_token, "Accept": "application/json"},
            json={"message": forged_message},
        )
        response.raise_for_status()
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)

    request = captured[0]
    messages = history.list_messages("learning-021", "u001")
    reply_extra = messages[-1].extra["intent_pipeline"]
    print("[HTTP 鉴权与上下文创建]")
    print("  用户消息伪造 userId = u002、planReference = foreign-plan")
    print("  Token 认证得到 context.user_id =", request.user_id)
    print("  服务端计划引用 =", request.plan_reference)
    print("  已保存用户消息 ID =", messages[0].id)
    print("  context.request_id =", request.request_id)
    print("  助手消息 trace_id =", reply_extra["trace_id"])

    tool = FakeProfileTool()
    coordinator = OrderedMasterCoordinator({
        IntentCategory.GENERAL_INFO: FakeChild("FakeInfoAgent", tool),
        IntentCategory.POLICY_QUERY: FakeChild("FakePolicyAgent", tool),
    }, InMemoryWriteLedger())
    print("\n[假 Master → 子 Agent → 假资料工具]")
    report = asyncio.run(coordinator.execute(
        fixed_two_intents(forged_message), QueryInput(question=forged_message),
        context=request,
    ))
    for step in report.steps:
        print(f"  第 {step.index + 1} 项：{step.agent_name} / {step.output} / trace_id={step.trace_id}")
    print("  整轮结果：", report.status, "/ trace_id=", report.trace_id)
    assert request.user_id == "u001" and request.plan_reference is None
    assert request.request_id == messages[0].id
    assert reply_extra["trace_id"] == report.trace_id
    assert all(step.output == "Alice 的资料" and step.trace_id == report.trace_id for step in report.steps)
    print("\n本演示的 Master、子 Agent 和资料工具仅为内存替身；生产 HTTP 尚未装配真实业务工具。")


if __name__ == "__main__":
    run()
