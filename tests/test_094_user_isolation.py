"""阶段 H 094：用户级隔离矩阵（会话、消息、标题、反馈、删除、预订确认凭据）。

两个用户共用同一进程内仓储与同一套路由。每个负例之前都先证明 owner 能正常访问，
避免因接口整体不可用而“假通过”。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import cast

from fastapi.testclient import TestClient
import pytest

from gogo_agent.api import app
from gogo_agent.auth.dependencies import get_current_user
from gogo_agent.auth.models import UserAccount
from gogo_agent.chat.dependencies import get_chat_executor, get_chat_history_service
from gogo_agent.chat.executor import ChatAgentExecutor
from gogo_agent.chat.repository import InMemoryAgentSessionStore, InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.intent.pipeline import IntentPipelineService

ALICE = "u-alice-094"
BOB = "u-bob-094"
ALICE_SESSION = "sess-alice-094"
ALICE_SECRET = "上海出差，预算五千元"
ALICE_REPLY = "助手回复：已记录上海行程"


def _login(user_id: str) -> None:
    app.dependency_overrides[get_current_user] = lambda: UserAccount(
        user_id=user_id, username=user_id, password_hash="test",
    )


@pytest.fixture
def world():
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    pipeline_calls: list[str] = []

    @asynccontextmanager
    async def forbidden_pipeline(*_args):
        # 记录而不中断：若隔离失效，断言 pipeline_calls == [] 会明确报错，且状态码断言先行不会被掩盖。
        pipeline_calls.append("pipeline")
        yield cast(IntentPipelineService, None)

    executor = ChatAgentExecutor(history, InMemoryAgentSessionStore(), pipeline_factory=forbidden_pipeline)
    app.dependency_overrides[get_chat_executor] = lambda: executor
    app.dependency_overrides[get_chat_history_service] = lambda: history
    history.save_user_message(ALICE_SESSION, ALICE, ALICE_SECRET)
    reply_id = history.save_assistant_message(
        conversation_id=ALICE_SESSION, user_id=ALICE, content=ALICE_REPLY, agent_name="GoGo",
    )
    try:
        yield {"client": TestClient(app), "history": history, "reply_id": reply_id, "pipeline_calls": pipeline_calls}
    finally:
        app.dependency_overrides.clear()


def _alice_messages(world) -> list[dict]:
    _login(ALICE)
    response = world["client"].get(f"/api/chat/{ALICE_SESSION}/messages")
    assert response.status_code == 200, "owner 必须能读取自己的会话，否则负例没有意义"
    return response.json()


def test_owner_can_read_own_session_and_list_it(world):
    _login(ALICE)
    client = world["client"]
    assert [c["id"] for c in client.get("/api/chat/conversations").json()] == [ALICE_SESSION]
    contents = [m["content"] for m in _alice_messages(world)]
    assert ALICE_SECRET in contents and ALICE_REPLY in contents


def test_other_user_cannot_read_messages_and_sees_no_conversations(world):
    before = _alice_messages(world)
    _login(BOB)
    client = world["client"]

    response = client.get(f"/api/chat/{ALICE_SESSION}/messages")
    assert response.status_code == 403
    assert ALICE_SECRET not in response.text and ALICE_REPLY not in response.text
    assert client.get("/api/chat/conversations").json() == []
    assert _alice_messages(world) == before


def test_other_user_cannot_send_into_session_even_with_a_confirmation_token(world):
    _alice_messages(world)
    _login(BOB)
    client = world["client"]
    history = world["history"]
    before = len(history.list_messages(ALICE_SESSION, ALICE))

    for body in (
        {"message": "帮我改成三亚"},
        {"message": "确认", "booking_confirmation_token": "forged-token-value"},
    ):
        response = client.post(
            f"/api/chat/{ALICE_SESSION}", json=body, headers={"Accept": "application/json"},
        )
        assert response.status_code == 403, body

    assert len(history.list_messages(ALICE_SESSION, ALICE)) == before
    assert world["pipeline_calls"] == [], "他人会话的请求不应进入识别或 Agent 执行"


def test_other_user_cannot_rename_feedback_or_delete_session(world):
    before = _alice_messages(world)
    _login(BOB)
    client = world["client"]

    assert client.put(f"/api/chat/{ALICE_SESSION}/title", json={"title": "被改名"}).status_code == 403
    assert client.put(
        f"/api/chat/{ALICE_SESSION}/messages/{world['reply_id']}/feedback", json={"feedback": "LIKE"},
    ).status_code == 403
    assert client.delete(f"/api/chat/{ALICE_SESSION}").status_code == 403

    after = _alice_messages(world)
    assert after == before
    assert all(m["feedback"] is None for m in after)
    assert [c["id"] for c in _alice_conversations(world)] == [ALICE_SESSION]
    assert _alice_conversations(world)[0]["title"] != "被改名"


def test_owner_feedback_and_delete_still_work(world):
    _alice_messages(world)
    client = world["client"]

    assert client.put(
        f"/api/chat/{ALICE_SESSION}/messages/{world['reply_id']}/feedback", json={"feedback": "LIKE"},
    ).status_code == 200
    liked = next(m for m in _alice_messages(world) if m["id"] == world["reply_id"])
    assert liked["feedback"] == "LIKE"

    assert client.delete(f"/api/chat/{ALICE_SESSION}").status_code == 200
    assert client.get("/api/chat/conversations").json() == []


def _alice_conversations(world) -> list[dict]:
    _login(ALICE)
    return world["client"].get("/api/chat/conversations").json()
