"""027：真实 InfoAgent 状态记录、精确续聊、过期回退与会话隔离。"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from agentscope.state import AgentState
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gogo_agent.api import app
from gogo_agent.chat.config import ChatAgentSettings
from gogo_agent.chat.repository import ActiveAgentRecord, SQLAgentSessionStore
from gogo_agent.db.base import Base
from tests.test_023_master_agent import FixedInfoModel, ScriptedMasterModel, master_http_case
from tests.test_024_agent_config import SlowInfoModel


def test_active_record_rejects_empty_agent_name():
    with pytest.raises(ValidationError):
        ActiveAgentRecord(agent_name="", expires_at=datetime.now(timezone.utc))
    with pytest.raises(ValidationError):
        ActiveAgentRecord(agent_name="InfoAgent", expires_at=datetime.now())


def test_corrupt_active_record_falls_back_to_full_pipeline(master_http_case):
    _, _, executor = master_http_case
    key = executor._state_session_id("corrupt-027", "u001")
    executor.session_store._active_agents[key] = '{"agent_name": 7}'
    request = executor._save_user_turn("corrupt-027", "u001", "继续")
    assert executor._active_continuation.get_active_agent(request) is None
    assert key not in executor.session_store._active_agents


@pytest.mark.parametrize("accept", ("application/json", "text/event-stream"))
def test_http_info_agent_continues_without_reopening_intent_pipeline(master_http_case, monkeypatch, accept):
    auth, history, executor = master_http_case
    pipeline_calls = []
    original_pipeline = executor._pipeline_factory

    @asynccontextmanager
    async def counted_pipeline(history_service):
        pipeline_calls.append(True)
        async with original_pipeline(history_service) as pipeline:
            yield pipeline

    executor._pipeline_factory = counted_pipeline
    masters = []
    children = []

    def build_model(stream=False, *, role="master"):
        if role == "info":
            model = FixedInfoModel()
            children.append(model)
        else:
            model = ScriptedMasterModel(stream=stream)
            masters.append(model)
        return model

    monkeypatch.setattr(executor, "_build_model", build_model)
    client = TestClient(app)
    session_id = f"info-027-{accept.replace('/', '-') }"
    headers = {"Authorization": auth.login("alice", "123456"), "Accept": accept}
    first = client.post(f"/api/chat/{session_id}", headers=headers, json={"message": "请介绍一处景点"})
    assert first.status_code == 200
    assert len(pipeline_calls) == 1 and len(masters) == 1 and len(children) == 1
    key = executor._state_session_id(session_id, "u001")
    state_before = executor.session_store.load_agent_state(key, agent_name="InfoAgent")
    active = executor.session_store.load_active_agent(key)
    assert state_before is not None and active.agent_name == "InfoAgent"

    continued = client.post(f"/api/chat/{session_id}", headers=headers, json={"message": "继续"})
    assert continued.status_code == 200
    if accept == "application/json":
        assert "离线信息子 Agent" in continued.json()["content"]
    else:
        assert "event: message\n" in continued.text and "event: message_id\n" in continued.text
    assert len(pipeline_calls) == 1 and len(masters) == 1 and len(children) == 2
    state_after = executor.session_store.load_agent_state(key, agent_name="InfoAgent")
    assert len(state_after.context) > len(state_before.context)
    assert executor.session_store.load_active_agent(key).expires_at >= active.expires_at
    final = history.list_messages(session_id, "u001")[-1]
    assert final.agentName == "InfoAgent"
    assert final.extra["turn_entry"] == "continue_active"

    denied = client.post(
        f"/api/chat/{session_id}",
        headers={"Authorization": auth.login("bob", "123456"), "Accept": accept},
        json={"message": "继续"},
    )
    assert denied.status_code == 403
    assert executor.session_store.load_active_agent(executor._state_session_id(session_id, "u002")) is None
    assert client.delete(f"/api/chat/{session_id}", headers=headers).status_code == 200
    assert executor.session_store.load_active_agent(key) is None
    assert executor.session_store.load_agent_state(key, agent_name="InfoAgent") is None


@pytest.mark.parametrize("stale", ("expired", "missing_state", "unknown_agent", "new_topic"))
def test_stale_or_new_topic_reenters_full_pipeline(master_http_case, monkeypatch, stale):
    auth, _, executor = master_http_case
    pipeline_calls = []
    original_pipeline = executor._pipeline_factory

    @asynccontextmanager
    async def counted_pipeline(history_service):
        pipeline_calls.append(True)
        async with original_pipeline(history_service) as pipeline:
            yield pipeline

    executor._pipeline_factory = counted_pipeline
    masters = []

    def build_model(stream=False, *, role="master"):
        if role == "info":
            return FixedInfoModel()
        model = ScriptedMasterModel(stream=stream)
        masters.append(model)
        return model

    monkeypatch.setattr(executor, "_build_model", build_model)
    client = TestClient(app)
    session_id = f"stale-027-{stale}"
    headers = {"Authorization": auth.login("alice", "123456"), "Accept": "application/json"}
    assert client.post(f"/api/chat/{session_id}", headers=headers, json={"message": "请介绍一处景点"}).status_code == 200
    key = executor._state_session_id(session_id, "u001")
    if stale == "expired":
        executor.session_store.save_active_agent(key, ActiveAgentRecord(
            agent_name="InfoAgent", expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        ))
    elif stale == "missing_state":
        executor.session_store.delete_agent_state(key, agent_name="InfoAgent")
    elif stale == "unknown_agent":
        executor.session_store.save_active_agent(key, ActiveAgentRecord(
            agent_name="PlanAgent", expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        ))
    message = "另外一件事" if stale == "new_topic" else "继续"
    assert client.post(f"/api/chat/{session_id}", headers=headers, json={"message": message}).status_code == 200
    assert len(pipeline_calls) == 2 and len(masters) == 2


def test_sql_active_record_and_child_state_survive_store_recreation():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        first = SQLAgentSessionStore(factory)
        key = "4:u001:sql-027"
        first.save_agent_state(key, AgentState(session_id="sql-027"), agent_name="InfoAgent")
        first.save_active_agent(key, ActiveAgentRecord(
            agent_name="InfoAgent", expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        ))
        second = SQLAgentSessionStore(factory)
        assert second.load_active_agent(key).agent_name == "InfoAgent"
        assert second.load_agent_state(key, agent_name="InfoAgent") is not None
        second.delete_active_agent(key)
        assert first.load_active_agent(key) is None
    finally:
        engine.dispose()


def test_direct_info_continuation_uses_configured_timeout(master_http_case, monkeypatch):
    auth, history, executor = master_http_case
    child_calls = []

    def build_model(stream=False, *, role="master"):
        if role == "master":
            return ScriptedMasterModel(stream=stream)
        child_calls.append(True)
        return FixedInfoModel() if len(child_calls) == 1 else SlowInfoModel()

    monkeypatch.setattr(executor, "_build_model", build_model)
    client = TestClient(app)
    session_id = "timeout-027"
    headers = {"Authorization": auth.login("alice", "123456"), "Accept": "application/json"}
    assert client.post(f"/api/chat/{session_id}", headers=headers, json={"message": "请介绍一处景点"}).status_code == 200
    executor._settings = ChatAgentSettings(info_tool_timeout_seconds=0.01)
    failed = client.post(f"/api/chat/{session_id}", headers=headers, json={"message": "继续"})
    assert failed.status_code == 503 and "TimeoutError" in failed.json()["message"]
    assert len(child_calls) == 2
    assert [message.role for message in history.list_messages(session_id, "u001")] == ["user", "agent", "user"]


@pytest.mark.asyncio
async def test_failed_visible_reply_save_does_not_advance_info_state(master_http_case, monkeypatch):
    _, history, executor = master_http_case
    monkeypatch.setattr(executor, "_build_model", lambda stream=False, role="master": (
        ScriptedMasterModel(stream=stream) if role == "master" else FixedInfoModel()
    ))
    session_id = "save-failure-027"
    await executor.execute_turn(session_id, "u001", "请介绍一处景点")
    key = executor._state_session_id(session_id, "u001")
    before_state = executor.session_store.load_agent_state(key, agent_name="InfoAgent").model_dump_json()
    before_active = executor.session_store.load_active_agent(key)
    original_save = history.save_assistant_message

    def fail_info_save(*args, **kwargs):
        if kwargs.get("agent_name") == "InfoAgent":
            raise RuntimeError("固定消息保存失败")
        return original_save(*args, **kwargs)

    monkeypatch.setattr(history, "save_assistant_message", fail_info_save)
    with pytest.raises(HTTPException, match="活跃 Agent 回复保存失败（RuntimeError）") as error:
        await executor.execute_turn(session_id, "u001", "继续")
    assert error.value.status_code == 503
    assert executor.session_store.load_agent_state(key, agent_name="InfoAgent").model_dump_json() == before_state
    assert executor.session_store.load_active_agent(key) == before_active
