"""020 新请求真实调用顺序、GoGo 状态恢复与活跃 Agent 续跑入口选择。"""

from contextlib import asynccontextmanager
import json

from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
import pytest

from gogo_agent.api import app
from gogo_agent.auth.dependencies import get_current_user
from gogo_agent.auth.models import UserAccount
from gogo_agent.chat.continuation import TurnEntry, choose_turn_entry
from gogo_agent.chat.dependencies import get_chat_executor, get_chat_history_service
from gogo_agent.chat.executor import ChatAgentExecutor, _FallbackMockModel
from gogo_agent.chat.repository import InMemoryAgentSessionStore, InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.intent import (
    IntentCategory, IntentPipelineService, IntentRecognizer,
    IntentRuleMatcher, RewriteContextBuilder,
)
from tests.test_010_011_intent import FixedModel
from tests.test_016_017_pipeline import ProbeRewriter, ProbeVector, two_intents


QUESTION = "查订单并规划下一程"


def recorded_executor(monkeypatch, events, *, active_continuation=None):
    """只记录实际方法边界；保留 017 编排和 AgentScope Agent 的真实调用。"""
    repository = InMemoryChatHistoryRepository()
    session_store = InMemoryAgentSessionStore()
    history = ChatHistoryService(repository=repository)
    result = two_intents(
        IntentCategory.TRAVEL_ORDER_QUERY, IntentCategory.ITINERARY_PLANNING,
    ).model_dump(mode="json")
    model = FixedModel(result, result)
    rewriter = ProbeRewriter(QUESTION)
    vector = ProbeVector()
    loaded_context_sizes = []

    original_save_conversation = repository.save_conversation
    original_update_title = repository.update_title
    original_save_message = repository.save_message
    original_get_history = history.get_history_messages
    original_load_state = session_store.load_agent_state
    original_save_state = session_store.save_agent_state
    original_llm_call = model._call_api

    def save_conversation(conversation):
        events.append("conversation_created")
        return original_save_conversation(conversation)

    def update_title(*args, **kwargs):
        events.append("title_updated")
        return original_update_title(*args, **kwargs)

    def save_message(message):
        events.append("user_saved" if message.role == "user" else "assistant_saved")
        return original_save_message(message)

    def get_history(*args, **kwargs):
        events.append("history_loaded")
        return original_get_history(*args, **kwargs)

    def load_state(*args, **kwargs):
        events.append("state_loaded")
        return original_load_state(*args, **kwargs)

    def save_state(*args, **kwargs):
        events.append("state_saved")
        return original_save_state(*args, **kwargs)

    async def llm_call(*args, **kwargs):
        events.append("l3_model")
        return await original_llm_call(*args, **kwargs)

    class RecordingRule:
        def match(self, query):
            events.append("rule_checked")
            return IntentRuleMatcher().match(query)

    class RecordingRewriter:
        async def rewrite(self, context):
            events.append("rewrite_called")
            return await rewriter.rewrite(context)

    @asynccontextmanager
    async def pipeline_factory(history_service):
        yield IntentPipelineService(
            RewriteContextBuilder(history_service),
            RecordingRewriter(),
            IntentRecognizer(
                model, rule_matcher=RecordingRule(), vector_matcher=vector,
            ),
        )

    for target, attribute, replacement in (
        (repository, "save_conversation", save_conversation),
        (repository, "update_title", update_title),
        (repository, "save_message", save_message),
        (history, "get_history_messages", get_history),
        (session_store, "load_agent_state", load_state),
        (session_store, "save_agent_state", save_state),
        (model, "_call_api", llm_call),
    ):
        monkeypatch.setattr(target, attribute, replacement)

    executor = ChatAgentExecutor(
        history, session_store, pipeline_factory=pipeline_factory,
        active_continuation=active_continuation,
    )
    monkeypatch.setattr(executor, "_build_model", lambda stream=False: _FallbackMockModel(stream=stream))
    original_build = executor._build_agent

    def build_agent(state, prepared, stream=False):
        events.append("coordinator_built")
        loaded_context_sizes.append(len(state.context))
        agent = original_build(state, prepared, stream=stream)
        original_reply = agent.reply

        async def reply(*args, **kwargs):
            events.append("coordinator_replied")
            return await original_reply(*args, **kwargs)

        agent.reply = reply
        return agent

    monkeypatch.setattr(executor, "_build_agent", build_agent)
    return executor, history, session_store, vector, rewriter, loaded_context_sizes


@pytest.mark.asyncio
async def test_new_request_order_includes_title_history_rewrite_and_coordinator(monkeypatch):
    events = []
    executor, history, store, vector, rewriter, sizes = recorded_executor(monkeypatch, events)
    reply, message_id = await executor.execute_turn("order-020", "user-1", QUESTION)

    assert reply and message_id
    assert events == [
        "conversation_created", "title_updated", "user_saved",
        "history_loaded", "rule_checked", "rewrite_called",
        "rule_checked", "l3_model", "state_loaded",
        "coordinator_built", "coordinator_replied",
        "assistant_saved", "state_saved",
    ]
    assert sizes == [0]
    assert vector.calls == []  # L1 报复合歧义，两次都跳过单标签 L2。
    assert len(rewriter.calls) == 1
    messages = history.get_history_messages("order-020", "user-1")
    assert [message.role for message in messages] == ["user", "agent"]
    assert store.load_agent_state(executor._state_session_id("order-020", "user-1")) is not None


@pytest.mark.asyncio
async def test_same_gogo_session_restores_state_but_still_runs_new_request_pipeline(monkeypatch):
    events = []
    executor, _, _, _, rewriter, sizes = recorded_executor(monkeypatch, events)
    await executor.execute_turn("repeat-020", "user-1", QUESTION)
    events.clear()
    await executor.execute_turn("repeat-020", "user-1", QUESTION)

    assert events == [
        "user_saved", "history_loaded", "rule_checked", "rewrite_called",
        "rule_checked", "l3_model", "state_loaded",
        "coordinator_built", "coordinator_replied",
        "assistant_saved", "state_saved",
    ]
    assert sizes == [0, 2]
    assert len(rewriter.calls) == 2


@pytest.mark.parametrize("active_name,message,available,expected", (
    (None, "继续", {"PlanAgent"}, TurnEntry.FULL_PIPELINE),
    ("", "继续", {"PlanAgent"}, TurnEntry.FULL_PIPELINE),
    ("MissingAgent", "继续", {"PlanAgent"}, TurnEntry.FULL_PIPELINE),
    ("PlanAgent", "请规划下一程", {"PlanAgent"}, TurnEntry.FULL_PIPELINE),
    ("PlanAgent", "继续 ", {"PlanAgent"}, TurnEntry.FULL_PIPELINE),
    ("PlanAgent", "继续", {"PlanAgent"}, TurnEntry.CONTINUE_ACTIVE),
    ("PlanAgent", "YES", {"PlanAgent"}, TurnEntry.CONTINUE_ACTIVE),
))
def test_active_agent_entry_selector_falls_back_for_missing_or_invalid_state(
    active_name, message, available, expected,
):
    assert choose_turn_entry(active_name, message, available) is expected


@pytest.mark.asyncio
async def test_injected_active_agent_continues_through_real_executor_without_pipeline(monkeypatch):
    events = []
    port = FakeActiveContinuation(events, active_name="PlanAgent")
    executor, history, store, _, rewriter, sizes = recorded_executor(
        monkeypatch, events, active_continuation=port,
    )
    history.save_user_message("active-020", "user-1", "请规划行程")
    history.save_assistant_message("active-020", "user-1", "请确认下一步", agent_name="PlanAgent")
    events.clear()
    reply, message_id = await executor.execute_turn("active-020", "user-1", "继续")

    assert reply == "PlanAgent 已继续处理" and message_id
    assert events == [
        "user_saved", "active_lookup", "active_agent_replied", "assistant_saved",
    ]
    assert sizes == [] and rewriter.calls == []
    assert store.load_agent_state(executor._state_session_id("active-020", "user-1")) is None
    messages = history.get_history_messages("active-020", "user-1")
    assert [message.role for message in messages] == ["user", "agent", "user", "agent"]
    assert messages[-1].agent_name == "PlanAgent"
    assert len(port.calls) == 1
    context, agent_name, question = port.calls[0]
    assert (context.session_id, context.user_id, agent_name, question) == (
        "active-020", "user-1", "PlanAgent", "继续",
    )
    assert context.request_id == messages[-2].message_id
    assert context.trace_id
    assert port.lookups[0] is context
    reply_extra = json.loads(messages[-1].extra)
    assert reply_extra["request_id"] == context.request_id
    assert reply_extra["trace_id"] == context.trace_id


@pytest.mark.asyncio
async def test_invalid_active_agent_falls_back_to_real_full_pipeline(monkeypatch):
    events = []
    port = FakeActiveContinuation(events, active_name="StaleAgent")
    executor, history, _, _, rewriter, sizes = recorded_executor(
        monkeypatch, events, active_continuation=port,
    )
    reply, _ = await executor.execute_turn("invalid-020", "user-1", "继续")

    assert reply
    assert events == [
        "conversation_created", "title_updated", "user_saved", "active_lookup",
        "history_loaded", "rule_checked", "rewrite_called", "rule_checked",
        "l3_model", "state_loaded", "coordinator_built", "coordinator_replied",
        "assistant_saved", "state_saved",
    ]
    assert port.calls == []
    assert len(rewriter.calls) == 1 and sizes == [0]
    assert history.get_history_messages("invalid-020", "user-1")[-1].agent_name == "GoGo"


@pytest.mark.asyncio
async def test_injected_active_agent_sse_preserves_message_events(monkeypatch):
    events = []
    port = FakeActiveContinuation(events, active_name="PlanAgent")
    executor, history, _, _, _, sizes = recorded_executor(
        monkeypatch, events, active_continuation=port,
    )
    history.save_user_message("stream-020", "user-1", "请规划行程")
    history.save_assistant_message("stream-020", "user-1", "请确认下一步", agent_name="PlanAgent")
    events.clear()
    stream = await executor.stream_turn_sse("stream-020", "user-1", "继续")
    output = "".join([event async for event in stream])
    assert "event: message\ndata: PlanAgent 已继续处理\n\n" in output
    assert "event: message_id\n" in output
    assert events == [
        "user_saved", "active_lookup", "active_agent_replied", "assistant_saved",
    ]
    assert sizes == []
    assert history.get_history_messages("stream-020", "user-1")[-1].agent_name == "PlanAgent"


@pytest.mark.asyncio
async def test_active_state_lookup_failure_stops_before_pipeline(monkeypatch):
    events = []
    port = FakeActiveContinuation(events, active_name="PlanAgent", fail_lookup=True)
    executor, history, _, _, rewriter, sizes = recorded_executor(
        monkeypatch, events, active_continuation=port,
    )
    with pytest.raises(HTTPException) as error:
        await executor.execute_turn("lookup-020", "user-1", "继续")
    assert error.value.status_code == 503
    assert events == ["conversation_created", "title_updated", "user_saved", "active_lookup"]
    assert sizes == [] and rewriter.calls == [] and port.calls == []
    assert [message.role for message in history.get_history_messages("lookup-020", "user-1")] == ["user"]


@pytest.mark.asyncio
async def test_active_continuation_cannot_bypass_session_ownership(monkeypatch):
    events = []
    port = FakeActiveContinuation(events, active_name="PlanAgent")
    executor, history, _, _, _, _ = recorded_executor(
        monkeypatch, events, active_continuation=port,
    )
    history.save_user_message("owned-020", "user-1", "原用户的问题")
    events.clear()
    with pytest.raises(HTTPException) as error:
        await executor.execute_turn("owned-020", "user-2", "继续")
    assert error.value.status_code == 403
    assert events == []
    assert port.calls == []


class FakeActiveContinuation:
    """只注入测试；027 才提供真实活跃 Agent 状态和业务执行者。"""

    available_agents = frozenset({"PlanAgent"})

    def __init__(self, events, *, active_name, fail_lookup=False):
        self.events = events
        self.active_name = active_name
        self.fail_lookup = fail_lookup
        self.calls = []
        self.lookups = []

    def get_active_agent(self, request):
        self.events.append("active_lookup")
        self.lookups.append(request)
        if self.fail_lookup:
            raise RuntimeError("active store unavailable")
        return self.active_name

    async def continue_turn(self, request, agent_name, query):
        self.events.append("active_agent_replied")
        self.calls.append((request, agent_name, query.question))
        return f"{agent_name} 已继续处理"


@pytest.mark.parametrize("accept", ("application/json", "text/event-stream"))
def test_http_active_continuation_and_stale_record_fallback(monkeypatch, accept):
    events = []
    port = FakeActiveContinuation(events, active_name="PlanAgent")
    executor, history, _, _, _, _ = recorded_executor(
        monkeypatch, events, active_continuation=port,
    )

    def current_user(request: Request) -> UserAccount:
        user_id = request.headers.get("x-test-user", "user-1")
        return UserAccount(user_id=user_id, username=user_id, password_hash="test")

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_chat_executor] = lambda: executor
    app.dependency_overrides[get_chat_history_service] = lambda: history
    headers = {"Accept": accept}
    try:
        client = TestClient(app)
        active_session = f"http-active-020-{accept.replace('/', '-')}"
        history.save_user_message(active_session, "user-1", "请规划行程")
        history.save_assistant_message(
            active_session, "user-1", "请确认下一步", agent_name="PlanAgent",
        )
        events.clear()
        continued = client.post(
            f"/api/chat/{active_session}", headers=headers, json={"message": "继续"},
        )
        assert continued.status_code == 200
        if accept == "application/json":
            assert continued.json()["content"] == "PlanAgent 已继续处理"
        else:
            assert "event: message\n" in continued.text
            assert "event: message_id\n" in continued.text
        assert "history_loaded" not in events and "coordinator_built" not in events
        active_reply = client.get(f"/api/chat/{active_session}/messages").json()[-1]
        assert active_reply["agentName"] == "PlanAgent"
        assert active_reply["extra"]["turn_entry"] == "continue_active"

        lookups_before = events.count("active_lookup")
        denied = client.post(
            f"/api/chat/{active_session}",
            headers={"Accept": accept, "X-Test-User": "user-2"},
            json={"message": "继续"},
        )
        assert denied.status_code == 403
        assert events.count("active_lookup") == lookups_before
        assert len(client.get(f"/api/chat/{active_session}/messages").json()) == 4

        port.active_name = "StaleAgent"
        events.clear()
        stale_session = f"http-stale-020-{accept.replace('/', '-')}"
        fallback = client.post(
            f"/api/chat/{stale_session}", headers=headers, json={"message": "继续"},
        )
        assert fallback.status_code == 200
        assert "history_loaded" in events and "coordinator_built" in events
        stale_reply = client.get(f"/api/chat/{stale_session}/messages").json()[-1]
        assert stale_reply["agentName"] == "GoGo"
        assert stale_reply["extra"]["intent_pipeline"]["dispatch_target"] == "GoGo"
    finally:
        app.dependency_overrides.clear()
