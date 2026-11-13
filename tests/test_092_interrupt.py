"""阶段 H 092：同会话中断请求的开关、一次性消费与跨用户拒绝。

中断标记存于与续聊相同的路由记录 `active_agent` 中（选项 2）。
本文件仅用内存仓储与共享后端的两个实例模拟“跨进程”；真实进程隔离需要两个进程运行，未在此覆盖。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentscope.state import AgentState
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gogo_agent.chat.repository import ActiveAgentRecord, InMemoryAgentSessionStore, SQLAgentSessionStore
from gogo_agent.db.base import Base


def test_interrupt_record_needs_agent_or_marker():
    now = datetime.now(timezone.utc)
    assert ActiveAgentRecord(agent_name=None, expires_at=now, interrupted_at=now).agent_name is None
    try:
        ActiveAgentRecord(agent_name=None, expires_at=now)
    except ValidationError:
        pass
    else:
        raise AssertionError("既无 Agent 也无中断标记的路由记录应被拒绝")


def test_interrupt_marker_round_trips_through_sql_store_across_instances():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        now = datetime.now(timezone.utc)
        first = SQLAgentSessionStore(factory)
        key = "4:u001:sql-092"
        first.save_agent_state(key, AgentState(session_id="sql-092"), agent_name="InfoAgent")
        first.save_active_agent(key, ActiveAgentRecord(
            agent_name="InfoAgent", expires_at=now + timedelta(minutes=30), interrupted_at=now,
        ))
        second = SQLAgentSessionStore(factory)
        loaded = second.load_active_agent(key)
        assert loaded is not None and loaded.agent_name == "InfoAgent" and loaded.interrupted_at is not None
    finally:
        engine.dispose()


def test_marker_only_record_round_trips_and_deletes_across_sql_instances():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        now = datetime.now(timezone.utc)
        first = SQLAgentSessionStore(factory)
        key = "4:u001:sql-092-marker"
        first.save_active_agent(key, ActiveAgentRecord(
            agent_name=None, expires_at=now + timedelta(minutes=10), interrupted_at=now,
        ))
        second = SQLAgentSessionStore(factory)
        loaded = second.load_active_agent(key)
        assert loaded is not None and loaded.agent_name is None
        second.delete_active_agent(key)
        assert first.load_active_agent(key) is None
    finally:
        engine.dispose()


def test_in_memory_marker_is_isolated_per_session_key():
    store = InMemoryAgentSessionStore()
    now = datetime.now(timezone.utc)
    store.save_active_agent("4:u001:a", ActiveAgentRecord(agent_name=None, expires_at=now, interrupted_at=now))
    assert store.load_active_agent("4:u002:a") is None
    owner = store.load_active_agent("4:u001:a")
    assert owner is not None and owner.interrupted_at is not None


# ---- 端到端：HTTP 路由 + 执行器（复用 019 的识别桩与模拟模型）----

from fastapi import Request
from fastapi.testclient import TestClient

from gogo_agent.api import app
from gogo_agent.auth.dependencies import get_current_user
from gogo_agent.auth.models import UserAccount
from gogo_agent.chat.dependencies import get_chat_executor, get_chat_history_service
from gogo_agent.chat.repository import InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from tests.test_019_routing import create_executor


def _user_override(request: Request) -> UserAccount:
    user_id = request.headers["x-test-user"]
    return UserAccount(user_id=user_id, username=user_id, password_hash="test")


def test_interrupt_blocks_only_the_next_turn_then_resumes(monkeypatch):
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    executor, built, _, _ = create_executor(monkeypatch, case="l1", history=history)
    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[get_chat_executor] = lambda: executor
    app.dependency_overrides[get_chat_history_service] = lambda: history
    headers = {"Accept": "application/json", "X-Test-User": "u001"}
    session = "interrupt-092"
    try:
        client = TestClient(app)
        # 中断要求会话已存在且归本人；先由 owner 发送一轮建立会话（这一轮不计入断言）。
        first = client.post(f"/api/chat/{session}", headers=headers, json={"message": "帮我查机票"})
        assert first.status_code == 200
        assert len(built) == 1
        built.clear()
        assert client.post(f"/api/chat/{session}/interrupt", headers={"X-Test-User": "u001"}).status_code == 200
        assert len(built) == 0

        blocked = client.post(f"/api/chat/{session}", headers=headers, json={"message": "帮我查机票"})
        assert blocked.status_code == 200
        assert blocked.json()["content"].startswith("已收到中断请求")
        assert len(built) == 0, "中断轮不应构造 Agent"

        resumed = client.post(f"/api/chat/{session}", headers=headers, json={"message": "再查一次机票"})
        assert resumed.status_code == 200
        assert not resumed.json()["content"].startswith("已收到中断请求")
        assert len(built) == 1, "中断只生效一次，下一轮应恢复正常执行"
    finally:
        app.dependency_overrides.clear()


def test_interrupt_is_owner_only_and_unknown_session_is_404(monkeypatch):
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    executor, _built, _, _ = create_executor(monkeypatch, case="l1", history=history)
    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[get_chat_executor] = lambda: executor
    app.dependency_overrides[get_chat_history_service] = lambda: history
    try:
        client = TestClient(app)
        # 先有 owner 创建会话，再验证 owner 可中断、他人被拒
        created = client.post("/api/chat/owned-092", headers={"Accept": "application/json", "X-Test-User": "u001"},
                              json={"message": "帮我查机票"})
        assert created.status_code == 200
        assert client.post("/api/chat/owned-092/interrupt", headers={"X-Test-User": "u001"}).status_code == 200
        denied = client.post("/api/chat/owned-092/interrupt", headers={"X-Test-User": "u002"})
        assert denied.status_code == 403
        missing = client.post("/api/chat/no-such-session-092/interrupt", headers={"X-Test-User": "u001"})
        assert missing.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_sse_interrupt_branch_blocks_one_turn_then_resumes(monkeypatch):
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    executor, built, _, _ = create_executor(monkeypatch, case="l1", history=history)
    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[get_chat_executor] = lambda: executor
    app.dependency_overrides[get_chat_history_service] = lambda: history
    sse = {"Accept": "text/event-stream", "X-Test-User": "u001"}
    session = "sse-interrupt-092"
    try:
        client = TestClient(app)
        created = client.post(f"/api/chat/{session}", headers=sse, json={"message": "帮我查机票"})
        assert created.status_code == 200
        built.clear()

        assert client.post(f"/api/chat/{session}/interrupt", headers={"X-Test-User": "u001"}).status_code == 200
        blocked = client.post(f"/api/chat/{session}", headers=sse, json={"message": "再查机票"})
        assert blocked.status_code == 200
        assert "event: message" in blocked.text and "已收到中断请求" in blocked.text
        assert "event: message_id" in blocked.text
        assert len(built) == 0, "SSE 中断轮不应构造 Agent"

        resumed = client.post(f"/api/chat/{session}", headers=sse, json={"message": "再查一次"})
        assert resumed.status_code == 200
        assert "已收到中断请求" not in resumed.text
        assert len(built) == 1, "SSE 中断只生效一次"
    finally:
        app.dependency_overrides.clear()
