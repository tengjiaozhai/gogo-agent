"""016 假子 Agent 顺序/写去重与 017 HTTP 前的四层识别流水线。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import date

from fastapi import HTTPException
from fastapi.testclient import TestClient
from agentscope.model import OpenAIChatModel
import pytest

from gogo_agent.api import app
from gogo_agent.auth.dependencies import get_current_user
from gogo_agent.auth.models import UserAccount
from gogo_agent.chat.dependencies import get_chat_executor, get_chat_history_service
from gogo_agent.chat.executor import ChatAgentExecutor, _FallbackMockModel
from gogo_agent.chat.repository import InMemoryAgentSessionStore, InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.intent import (
    ConfidenceLevel,
    FastMatch,
    InMemoryWriteLedger,
    IntentCandidate,
    IntentCategory,
    IntentItem,
    IntentPipelineService,
    IntentRecognizer,
    IntentResult,
    IntentRuleMatcher,
    MatchStatus,
    OrderedMasterCoordinator,
    QueryInput,
    RecognitionLayer,
    QueryRewriter,
    RewriteContextBuilder,
    RewriteResult,
)
from gogo_agent.request_context import RequestContext
from tests.test_010_011_intent import FixedModel


REFERENCE_DATE = date(2026, 9, 28)


def request_context(request_id: str, *, user_id: str = "user-1") -> RequestContext:
    return RequestContext(
        user_id=user_id, session_id="session-1", request_id=request_id,
        trace_id=f"trace-{request_id}",
    )


def pipeline_request(session_id: str, request_id: str) -> RequestContext:
    return RequestContext(
        user_id="user-1", session_id=session_id, request_id=request_id,
        trace_id=f"trace-{request_id}",
    )


def one_intent(category: IntentCategory) -> IntentResult:
    return IntentResult(
        intents=[IntentItem(
            intent=category, confidence=ConfidenceLevel.HIGH,
            reason="固定分类", evidence=["原问题"],
        )],
        primary_intent=category, multi_intent=False, overall_reason="固定分类",
    )


def two_intents(first: IntentCategory, second: IntentCategory) -> IntentResult:
    return IntentResult(
        intents=[
            IntentItem(intent=first, confidence=ConfidenceLevel.HIGH, reason="第一项", evidence=["第一项原文"]),
            IntentItem(intent=second, confidence=ConfidenceLevel.HIGH, reason="第二项", evidence=["第二项原文"]),
        ],
        primary_intent=second, multi_intent=True, overall_reason="先第一项，再第二项",
    )


class ProbeVector:
    """只控制 L2 得分与调用次数，不替代真实 IntentRecognizer 的短路。"""

    def __init__(self, hit: IntentCategory | None = None):
        self.hit = hit
        self.calls: list[str] = []

    async def match(self, query: QueryInput) -> FastMatch:
        self.calls.append(query.question)
        if self.hit is None:
            return FastMatch(status=MatchStatus.MISS, result=None, threshold=.75, reason="L2 固定未命中")
        return FastMatch(
            status=MatchStatus.HIT, result=one_intent(self.hit), threshold=.75,
            candidates=[IntentCandidate(
                intent=self.hit, layer=RecognitionLayer.VECTOR, score=.91,
                reason="固定近邻样例",
            )],
            reason="L2 固定近邻命中",
        )


class ProbeRewriter:
    """记录改写次数，可返回独立问题、缺上下文或明确异常。"""

    def __init__(self, rewritten: str | None = None, *, fail: bool = False):
        self.rewritten = rewritten
        self.fail = fail
        self.calls = []

    async def rewrite(self, context):
        self.calls.append(context)
        if self.fail:
            raise RuntimeError("rewriter deliberately failed")
        return RewriteResult(
            related=False,
            rewritten_question=self.rewritten,
            reason="固定改写" if self.rewritten else "缺少指代对象",
            evidence=[context.query.question],
            missing_context=[] if self.rewritten else ["要查询的订单或行程"],
        )


def pipeline_for(history, rewriter, vector, model=None):
    return IntentPipelineService(
        RewriteContextBuilder(history),
        rewriter,
        IntentRecognizer(
            model or FixedModel(),
            rule_matcher=IntentRuleMatcher(),
            vector_matcher=vector,
        ),
    )


@pytest.mark.asyncio
async def test_fast_l1_and_l2_hits_skip_rewrite_and_model():
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    rewriter = ProbeRewriter("不应执行")
    vector = ProbeVector()
    pipeline = pipeline_for(history, rewriter, vector)
    first_id = history.save_user_message("session-017", "user-1", "查机票")
    l1 = await pipeline.prepare(pipeline_request("session-017", first_id), reference_date=REFERENCE_DATE)
    assert l1.branch == "fast"
    assert l1.fast_decision.hit_layer is RecognitionLayer.RULE
    assert l1.effective_question.question == "查机票"
    assert vector.calls == rewriter.calls == []

    vector.hit = IntentCategory.APPROVAL_QUERY
    second_question = "我交上去那个流程现在走到哪一步了"
    second_id = history.save_user_message("session-017", "user-1", second_question)
    l2 = await pipeline.prepare(pipeline_request("session-017", second_id), reference_date=REFERENCE_DATE)
    assert l2.branch == "fast"
    assert l2.fast_decision.hit_layer is RecognitionLayer.VECTOR
    assert vector.calls == [second_question]
    assert rewriter.calls == []


@pytest.mark.asyncio
async def test_miss_rewrites_once_then_full_l3_recognizes_ordered_multi_intent():
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    question = "查订单并规划下一程"
    message_id = history.save_user_message("session-017", "user-1", question)
    rewriter = ProbeRewriter(question)
    vector = ProbeVector()
    model = FixedModel(two_intents(
        IntentCategory.TRAVEL_ORDER_QUERY, IntentCategory.ITINERARY_PLANNING,
    ).model_dump(mode="json"))
    prepared = await pipeline_for(history, rewriter, vector, model).prepare(
        pipeline_request("session-017", message_id),
        reference_date=REFERENCE_DATE,
    )
    assert prepared.branch == "rewritten"
    assert len(rewriter.calls) == len(model.calls) == 1
    assert vector.calls == []  # L1 两次弃权，整句不进入单标签 L2。
    assert prepared.fast_decision.attempted_layers == [RecognitionLayer.RULE]
    assert prepared.decision.attempted_layers == [RecognitionLayer.RULE, RecognitionLayer.LLM]
    assert [item.intent for item in prepared.decision.result.intents] == [
        IntentCategory.TRAVEL_ORDER_QUERY, IntentCategory.ITINERARY_PLANNING,
    ]


@pytest.mark.asyncio
async def test_l0_strong_conjunction_skips_vector_and_reaches_l3():
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    question = "帮我查一下差旅政策，并且报销这张发票"
    message_id = history.save_user_message("l0-017", "user-1", question)
    vector = ProbeVector()
    rewriter = ProbeRewriter(question)
    model = FixedModel(two_intents(
        IntentCategory.POLICY_QUERY, IntentCategory.REIMBURSEMENT,
    ).model_dump(mode="json"))
    prepared = await pipeline_for(history, rewriter, vector, model).prepare(
        pipeline_request("l0-017", message_id),
        reference_date=REFERENCE_DATE,
    )
    assert prepared.branch == "rewritten"
    assert "L0" in prepared.fast_decision.reason
    assert vector.calls == []
    assert len(rewriter.calls) == len(model.calls) == 1
    assert prepared.decision.hit_layer is RecognitionLayer.LLM
    assert [item.intent for item in prepared.decision.result.intents] == [
        IntentCategory.POLICY_QUERY, IntentCategory.REIMBURSEMENT,
    ]


@pytest.mark.asyncio
async def test_missing_context_or_rewrite_failure_never_dispatches():
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    message_id = history.save_user_message("session-017", "user-1", "它呢？")
    vector = ProbeVector()
    missing = ProbeRewriter()
    prepared = await pipeline_for(history, missing, vector).prepare(
        pipeline_request("session-017", message_id),
        reference_date=REFERENCE_DATE,
    )
    assert prepared.branch == "needs_context"
    assert prepared.decision is None and prepared.effective_question is None
    assert missing.calls == []  # 无历史的指代短问句直接要求补齐，不浪费模型调用。

    failing = ProbeRewriter(fail=True)
    failure_id = history.save_user_message("failure-017", "user-1", "帮我写一个Python函数")
    with pytest.raises(RuntimeError, match="deliberately failed"):
        await pipeline_for(history, failing, ProbeVector()).prepare(
            pipeline_request("failure-017", failure_id),
            reference_date=REFERENCE_DATE,
        )
    assert len(failing.calls) == 1


class FakeChild:
    """016 假子 Agent 只记录委派；writes 用于验证同请求去重。"""

    def __init__(self, name: str, *, writes: bool = False, fail: bool = False):
        self.name = name
        self.writes = writes
        self.fail = fail
        self.calls = []

    async def run(self, item, question, context, completed):
        self.calls.append((item.intent, question.question, [step.output for step in completed], context))
        if self.fail:
            raise RuntimeError("fake child failed")
        return f"{item.intent.value} 已模拟完成"


class PausingChild(FakeChild):
    """让两轮请求真正重叠，验证 ledger 的同键去重与异键并发。"""

    def __init__(self, name: str):
        super().__init__(name, writes=True)
        self.entered = asyncio.Event()
        self.two_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, item, question, context, completed):
        self.calls.append((item.intent, question.question, [step.output for step in completed], context))
        self.entered.set()
        if len(self.calls) == 2:
            self.two_entered.set()
        await self.release.wait()
        return f"{item.intent.value} 已模拟完成"


@pytest.mark.asyncio
async def test_master_preserves_query_then_plan_order_and_reuses_same_write_request():
    query = FakeChild("ManageAgent")
    plan = FakeChild("PlanAgent", writes=True)
    master = OrderedMasterCoordinator({
        IntentCategory.TRAVEL_ORDER_QUERY: query,
        IntentCategory.ITINERARY_PLANNING: plan,
    }, InMemoryWriteLedger())
    result = two_intents(IntentCategory.TRAVEL_ORDER_QUERY, IntentCategory.ITINERARY_PLANNING)
    first_context = request_context("message-1")
    repeated_context = RequestContext(
        user_id=first_context.user_id, session_id=first_context.session_id,
        request_id=first_context.request_id, trace_id="trace-repeated",
    )
    first = await master.execute(
        result, QueryInput(question="先查订单再规划"),
        context=first_context,
    )
    repeated = await master.execute(
        result, QueryInput(question="先查订单再规划"),
        context=repeated_context,
    )
    assert first.status == repeated.status == "completed"
    assert [step.agent_name for step in first.steps] == ["ManageAgent", "PlanAgent"]
    assert plan.calls[0][2] == ["travel_order_query 已模拟完成"]
    assert len(query.calls) == 2 and len(plan.calls) == 1
    assert repeated.steps[1].reused is True
    assert [step.trace_id for step in first.steps] == [first_context.trace_id] * 2
    assert [step.trace_id for step in repeated.steps] == [repeated_context.trace_id] * 2
    assert first.trace_id == first_context.trace_id and repeated.trace_id == repeated_context.trace_id
    assert "1. ManageAgent" in first.summary and "2. PlanAgent" in first.summary


@pytest.mark.asyncio
async def test_plan_then_booking_dependency_failure_and_write_retry_boundary():
    plan = FakeChild("PlanAgent", writes=True, fail=True)
    booking = FakeChild("BookingAgent", writes=True)
    master = OrderedMasterCoordinator({
        IntentCategory.ITINERARY_PLANNING: plan,
        IntentCategory.BOOKING: booking,
    }, InMemoryWriteLedger())
    result = two_intents(IntentCategory.ITINERARY_PLANNING, IntentCategory.BOOKING)
    for attempt in range(2):
        report = await master.execute(
            result, QueryInput(question="先规划后预订"),
            context=request_context("message-2"),
        )
        assert report.status == "failed"
        assert [step.status for step in report.steps] == ["failed", "skipped"]
        assert all(step.trace_id == report.trace_id for step in report.steps)
        assert "未完成" in report.summary
        if attempt:
            assert report.steps[0].reused is True
    assert len(plan.calls) == 1 and booking.calls == []


@pytest.mark.asyncio
async def test_successful_plan_then_booking_uses_previous_result_and_each_write_once():
    plan = FakeChild("PlanAgent", writes=True)
    booking = FakeChild("BookingAgent", writes=True)
    master = OrderedMasterCoordinator({
        IntentCategory.ITINERARY_PLANNING: plan,
        IntentCategory.BOOKING: booking,
    }, InMemoryWriteLedger())
    result = two_intents(IntentCategory.ITINERARY_PLANNING, IntentCategory.BOOKING)
    for attempt in range(2):
        report = await master.execute(
            result, QueryInput(question="先规划后预订"),
            context=request_context("message-success"),
        )
        assert report.status == "completed"
        if attempt:
            assert [step.reused for step in report.steps] == [True, True]
    assert booking.calls[0][2] == ["itinerary_planning 已模拟完成"]
    assert len(plan.calls) == len(booking.calls) == 1


@pytest.mark.asyncio
async def test_partial_result_and_concurrent_duplicate_write_calls_once():
    plan = PausingChild("PlanAgent")
    booking = FakeChild("BookingAgent", writes=True, fail=True)
    master = OrderedMasterCoordinator({
        IntentCategory.ITINERARY_PLANNING: plan,
        IntentCategory.BOOKING: booking,
    }, InMemoryWriteLedger())
    result = two_intents(IntentCategory.ITINERARY_PLANNING, IntentCategory.BOOKING)

    async def run():
        return await master.execute(
            result, QueryInput(question="先规划后预订"),
            context=request_context("message-3"),
        )

    first = asyncio.create_task(run())
    await asyncio.wait_for(plan.entered.wait(), 1)
    second = asyncio.create_task(run())
    await asyncio.sleep(0)
    assert not first.done() and not second.done()
    assert len(plan.calls) == 1
    plan.release.set()
    reports = await asyncio.gather(first, second)
    assert all(report.status == "partial" for report in reports)
    assert all([step.status for step in report.steps] == ["completed", "failed"] for report in reports)
    assert len(plan.calls) == len(booking.calls) == 1
    assert "1. PlanAgent" in reports[0].summary and "第 2 项未完成" in reports[0].summary


@pytest.mark.asyncio
async def test_different_write_requests_can_run_concurrently():
    plan = PausingChild("PlanAgent")
    master = OrderedMasterCoordinator({
        IntentCategory.ITINERARY_PLANNING: plan,
    }, InMemoryWriteLedger())
    result = one_intent(IntentCategory.ITINERARY_PLANNING)

    async def run(request_id):
        return await master.execute(
            result, QueryInput(question="规划行程"),
            context=request_context(request_id),
        )

    first = asyncio.create_task(run("message-a"))
    await asyncio.wait_for(plan.entered.wait(), 1)
    second = asyncio.create_task(run("message-b"))
    await asyncio.wait_for(plan.two_entered.wait(), 1)
    plan.release.set()
    reports = await asyncio.gather(first, second)
    assert len(plan.calls) == 2
    assert all(report.status == "completed" for report in reports)


def test_http_json_sse_share_preprocessing_and_keep_message_contract(monkeypatch):
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    sessions = InMemoryAgentSessionStore()
    vector = ProbeVector()
    rewriter = ProbeRewriter("不应执行")

    @asynccontextmanager
    async def factory(history_service):
        yield pipeline_for(history_service, rewriter, vector)

    executor = ChatAgentExecutor(history, sessions, pipeline_factory=factory)
    monkeypatch.setattr(executor, "_build_model", lambda stream=False: _FallbackMockModel(stream=stream))
    app.dependency_overrides[get_current_user] = lambda: UserAccount(
        user_id="user-1", username="demo", password_hash="test",
    )
    app.dependency_overrides[get_chat_executor] = lambda: executor
    app.dependency_overrides[get_chat_history_service] = lambda: history
    try:
        client = TestClient(app)
        json_response = client.post(
            "/api/chat/learning-017", headers={"Accept": "application/json"},
            json={"message": "查机票"},
        )
        assert json_response.status_code == 200
        assert set(json_response.json()) == {"sessionId", "messageId", "content"}
        sse_response = client.post(
            "/api/chat/learning-017", headers={"Accept": "text/event-stream"},
            json={"message": "你好"},
        )
        assert sse_response.status_code == 200
        assert "event: message\n" in sse_response.text
        assert "event: message_id\n" in sse_response.text
        messages = client.get("/api/chat/learning-017/messages").json()
        assert [message["role"] for message in messages] == ["user", "agent", "user", "agent"]
        assert messages[1]["extra"]["intent_pipeline"]["branch"] == "fast"
        assert messages[1]["extra"]["intent_pipeline"]["hit_layer"] == "rule"
        assert rewriter.calls == vector.calls == []
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("accept", ("application/json", "text/event-stream"))
def test_http_rewrite_failure_is_visible_and_never_runs_agent(monkeypatch, accept):
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    sessions = InMemoryAgentSessionStore()
    invalid_model = FixedModel("不是JSON")

    @asynccontextmanager
    async def factory(history_service):
        yield pipeline_for(history_service, QueryRewriter(invalid_model), ProbeVector())

    executor = ChatAgentExecutor(history, sessions, pipeline_factory=factory)
    agent_calls = []
    monkeypatch.setattr(executor, "_build_agent", lambda *args, **kwargs: agent_calls.append(True))
    app.dependency_overrides[get_current_user] = lambda: UserAccount(
        user_id="user-1", username="demo", password_hash="test",
    )
    app.dependency_overrides[get_chat_executor] = lambda: executor
    try:
        response = TestClient(app).post(
            "/api/chat/failure-017", headers={"Accept": accept},
            json={"message": "帮我写一个Python函数"},
        )
        assert response.status_code == 503
        assert response.json()["code"] == 503
        assert "ModelOutputError" in response.json()["message"]
        assert agent_calls == []
        assert sessions.load_agent_state(executor._state_session_id("failure-017", "user-1"), agent_name="GoGo") is None
        assert len(history.get_history_messages("failure-017", "user-1")) == 1
        assert len(invalid_model.calls) == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_empty_input_fails_before_message_save_or_pipeline_open():
    repository = InMemoryChatHistoryRepository()
    history = ChatHistoryService(repository=repository)
    executor = ChatAgentExecutor(history, InMemoryAgentSessionStore())
    with pytest.raises(HTTPException) as error:
        await executor.execute_turn("empty-017", "user-1", "   ")
    assert error.value.status_code == 422
    assert repository.find_conversation_by_id("empty-017") is None


@pytest.mark.asyncio
async def test_compatible_v1_gateway_url_keeps_real_chat_model(monkeypatch):
    monkeypatch.setenv("GOGO_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("GOGO_MODEL_NAME", "test-model")
    monkeypatch.setenv("GOGO_MODEL_BASE_URL", "https://gateway.example.test/v1")
    executor = ChatAgentExecutor(
        ChatHistoryService(repository=InMemoryChatHistoryRepository()),
        InMemoryAgentSessionStore(),
    )
    model = executor._build_model()
    try:
        assert isinstance(model, OpenAIChatModel)
        assert str(model.client.base_url).rstrip("/") == "https://gateway.example.test/v1"
    finally:
        await model.client.close()


@pytest.mark.parametrize("accept", ("application/json", "text/event-stream"))
def test_chat_model_failure_never_reports_completed_message(monkeypatch, accept):
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    sessions = InMemoryAgentSessionStore()

    @asynccontextmanager
    async def factory(history_service):
        yield pipeline_for(history_service, ProbeRewriter("不应调用"), ProbeVector())

    class FailingAgent:
        async def reply(self, *_args, **_kwargs):
            raise RuntimeError("generation failed")

        async def reply_stream(self, *_args, **_kwargs):
            raise RuntimeError("generation failed")
            yield  # 使方法保持异步生成器形态。

    executor = ChatAgentExecutor(history, sessions, pipeline_factory=factory)
    monkeypatch.setattr(executor, "_build_agent", lambda *args, **kwargs: FailingAgent())
    app.dependency_overrides[get_current_user] = lambda: UserAccount(
        user_id="user-1", username="demo", password_hash="test",
    )
    app.dependency_overrides[get_chat_executor] = lambda: executor
    try:
        response = TestClient(app).post(
            "/api/chat/chat-error-017", headers={"Accept": accept},
            json={"message": "查机票"},
        )
        if accept == "application/json":
            assert response.status_code == 503
            assert "RuntimeError" in response.json()["message"]
        else:
            assert response.status_code == 200
            assert "event: error\n" in response.text
            assert "event: message_id\n" not in response.text
        assert len(history.get_history_messages("chat-error-017", "user-1")) == 1
        assert sessions.load_agent_state(executor._state_session_id("chat-error-017", "user-1"), agent_name="GoGo") is None
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", (False, True))
async def test_reply_save_failure_does_not_commit_new_agent_state(monkeypatch, stream):
    repository = InMemoryChatHistoryRepository()
    history = ChatHistoryService(repository=repository)
    sessions = InMemoryAgentSessionStore()

    @asynccontextmanager
    async def factory(history_service):
        yield pipeline_for(history_service, ProbeRewriter("不应调用"), ProbeVector())

    executor = ChatAgentExecutor(history, sessions, pipeline_factory=factory)
    monkeypatch.setattr(executor, "_build_model", lambda stream=False: _FallbackMockModel(stream=stream))
    original_save = repository.save_message

    def fail_assistant_save(message):
        if message.role == "agent":
            raise RuntimeError("assistant write unavailable")
        return original_save(message)

    monkeypatch.setattr(repository, "save_message", fail_assistant_save)
    session_id = "save-error-017"
    if stream:
        events = await executor.stream_turn_sse(session_id, "user-1", "查机票")
        output = "".join([event async for event in events])
        assert "event: error\n" in output
        assert "event: message_id\n" not in output
    else:
        with pytest.raises(RuntimeError, match="assistant write unavailable"):
            await executor.execute_turn(session_id, "user-1", "查机票")
    state_key = executor._state_session_id(session_id, "user-1")
    assert sessions.load_agent_state(state_key, agent_name="GoGo") is None
    assert [message.role for message in history.get_history_messages(session_id, "user-1")] == ["user"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", (False, True))
async def test_chat_model_client_is_closed_after_turn(monkeypatch, stream):
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())

    @asynccontextmanager
    async def factory(history_service):
        yield pipeline_for(history_service, ProbeRewriter("不应调用"), ProbeVector())

    executor = ChatAgentExecutor(history, InMemoryAgentSessionStore(), pipeline_factory=factory)
    models = []

    def build_model(stream=False):
        model = _FallbackMockModel(stream=stream)
        models.append(model)
        return model

    monkeypatch.setattr(executor, "_build_model", build_model)
    if stream:
        events = await executor.stream_turn_sse("close-017", "user-1", "查机票")
        assert "event: message_id\n" in "".join([event async for event in events])
    else:
        reply, _ = await executor.execute_turn("close-017", "user-1", "查机票")
        assert reply
    assert len(models) == 1
    assert models[0].client.is_closed()
