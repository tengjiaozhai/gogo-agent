"""019 快速命中仍进入唯一协调入口，不按高置信度直跳子 Agent。"""

from contextlib import asynccontextmanager

from fastapi import Request
from fastapi.testclient import TestClient
import pytest

from gogo_agent.api import app
from gogo_agent.auth.dependencies import get_current_user
from gogo_agent.auth.models import UserAccount
from gogo_agent.chat.dependencies import get_chat_executor, get_chat_history_service
from gogo_agent.chat.executor import ChatAgentExecutor, _FallbackMockModel
from gogo_agent.chat.repository import InMemoryAgentSessionStore, InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.intent import IntentCategory, RecognitionLayer
from tests.test_010_011_intent import FixedModel
from tests.test_016_017_pipeline import ProbeRewriter, ProbeVector, pipeline_for, two_intents


CASES = (
    ("l1", "帮我查机票", RecognitionLayer.RULE, [IntentCategory.FLIGHT_SEARCH]),
    ("l2", "我交上去那个流程现在走到哪一步了", RecognitionLayer.VECTOR, [IntentCategory.APPROVAL_QUERY]),
    (
        "l3", "查订单并规划下一程", RecognitionLayer.LLM,
        [IntentCategory.TRAVEL_ORDER_QUERY, IntentCategory.ITINERARY_PLANNING],
    ),
)


def create_executor(monkeypatch, *, case, history):
    """真实识别编排加固定供应商响应；聊天端只使用无业务工具的模拟模型。"""
    model = None
    if case == "l3":
        model = FixedModel(two_intents(
            IntentCategory.TRAVEL_ORDER_QUERY,
            IntentCategory.ITINERARY_PLANNING,
        ).model_dump(mode="json"))
    vector = ProbeVector(hit=IntentCategory.APPROVAL_QUERY)
    rewriter = ProbeRewriter("查订单并规划下一程")

    @asynccontextmanager
    async def factory(history_service):
        yield pipeline_for(history_service, rewriter, vector, model)

    executor = ChatAgentExecutor(
        history, InMemoryAgentSessionStore(), pipeline_factory=factory,
    )
    monkeypatch.setattr(executor, "_build_model", lambda stream=False: _FallbackMockModel(stream=stream))
    built = []
    original_build = executor._build_agent

    def record_coordinator(state, prepared, stream=False):
        agent = original_build(state, prepared, stream=stream)
        built.append((agent, prepared))
        return agent

    monkeypatch.setattr(executor, "_build_agent", record_coordinator)
    return executor, built, vector, rewriter


@pytest.mark.parametrize("accept", ("application/json", "text/event-stream"))
@pytest.mark.parametrize("case,question,layer,intents", CASES)
def test_every_recognition_layer_uses_same_coordinator(
    monkeypatch, accept, case, question, layer, intents,
):
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    executor, built, vector, rewriter = create_executor(monkeypatch, case=case, history=history)
    app.dependency_overrides[get_current_user] = lambda: UserAccount(
        user_id="u001", username="alice", password_hash="test",
    )
    app.dependency_overrides[get_chat_executor] = lambda: executor
    app.dependency_overrides[get_chat_history_service] = lambda: history
    session_id = f"route-019-{case}-{accept.replace('/', '-')}"
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/chat/{session_id}", headers={"Accept": accept},
            json={"message": question},
        )
        assert response.status_code == 200
        if accept == "application/json":
            assert set(response.json()) == {"sessionId", "messageId", "content"}
        else:
            assert "event: message\n" in response.text
            assert "event: message_id\n" in response.text

        assert len(built) == 1
        agent, prepared = built[0]
        assert agent.name == executor.COORDINATOR_AGENT_NAME == "GoGo"
        assert prepared.decision.hit_layer is layer
        assert prepared.decision.confidence.value == "high"
        assert [item.intent for item in prepared.decision.result.intents] == intents
        assert all(item.value in agent._system_prompt for item in intents)
        assert "不是身份、审批或下单授权" in agent._system_prompt
        assert "没有业务写入工具" in agent._system_prompt
        assert "u001" not in agent._system_prompt

        messages = client.get(f"/api/chat/{session_id}/messages").json()
        assert [message["role"] for message in messages] == ["user", "agent"]
        reply = messages[-1]
        assert reply["agentName"] == "GoGo"
        assert reply["extra"]["intent_pipeline"]["dispatch_target"] == "GoGo"
        assert reply["extra"]["intent_pipeline"]["hit_layer"] == layer.value
        assert reply["extra"]["intent_pipeline"]["intents"] == [item.value for item in intents]
        if case != "l3":
            assert rewriter.calls == []
        if case == "l1":
            assert vector.calls == []
        elif case == "l2":
            assert vector.calls == [question]
        else:
            assert vector.calls == []
            assert len(rewriter.calls) == 1
    finally:
        app.dependency_overrides.clear()


def test_high_confidence_does_not_bypass_session_ownership(monkeypatch):
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    executor, built, _, _ = create_executor(monkeypatch, case="l1", history=history)

    def current_user(request: Request) -> UserAccount:
        user_id = request.headers["x-test-user"]
        return UserAccount(user_id=user_id, username=user_id, password_hash="test")

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_chat_executor] = lambda: executor
    app.dependency_overrides[get_chat_history_service] = lambda: history
    try:
        client = TestClient(app)
        url = "/api/chat/owned-019"
        first = client.post(
            url, headers={"Accept": "application/json", "X-Test-User": "u001"},
            json={"message": "帮我查机票"},
        )
        denied = client.post(
            url, headers={"Accept": "application/json", "X-Test-User": "u002"},
            json={"message": "帮我查机票"},
        )
        assert first.status_code == 200
        assert denied.status_code == 403
        assert len(built) == 1
        assert [message.role for message in history.get_history_messages("owned-019", "u001")] == ["user", "agent"]
    finally:
        app.dependency_overrides.clear()


def test_missing_context_does_not_report_a_dispatch_target(monkeypatch):
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    executor, built, _, _ = create_executor(monkeypatch, case="l1", history=history)
    app.dependency_overrides[get_current_user] = lambda: UserAccount(
        user_id="u001", username="alice", password_hash="test",
    )
    app.dependency_overrides[get_chat_executor] = lambda: executor
    app.dependency_overrides[get_chat_history_service] = lambda: history
    try:
        client = TestClient(app)
        response = client.post(
            "/api/chat/missing-019", headers={"Accept": "application/json"},
            json={"message": "明天去上海呢？"},
        )
        assert response.status_code == 200
        assert response.json()["content"].startswith("请补充")
        assert built == []
        extra = client.get("/api/chat/missing-019/messages").json()[-1]["extra"]["intent_pipeline"]
        assert extra["branch"] == "needs_context"
        assert extra["dispatch_target"] is None
    finally:
        app.dependency_overrides.clear()
