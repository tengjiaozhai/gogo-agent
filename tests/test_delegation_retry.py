"""子 Agent 重试关系、真实确认令牌恢复与失败保存策略。"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from agentscope.message import ToolResultBlock

from gogo_agent.chat.delegation import DelegationAttempts
from gogo_agent.chat.executor import ChatAgentExecutor
from scripts.demo_confirmation_routing import SESSION_ID, create_demo_app, tool_text
from tests.test_023_master_agent import master_http_case


@pytest.mark.parametrize("accept", ["application/json", "text/event-stream"])
def test_recovered_manage_retry_saves_token_for_next_confirmation(accept):
    application, auth, history, executor, business, engine = create_demo_app(retry_scenario="recovered")
    try:
        with TestClient(application) as client:
            headers = {"Authorization": auth.login("alice", "123456"), "Accept": accept}
            first = client.post(f"/api/chat/{SESSION_ID}", headers=headers, json={"message": "确认提交"})
            assert first.status_code == 200
            if accept == "text/event-stream":
                assert "event: error" not in first.text
                assert "event: message_id" in first.text
            saved = history.list_messages(SESSION_ID, "u001")[-1]
            assert saved.role == "agent"
            calls = saved.extra["tool_calls"]
            assert [call["status"] for call in calls] == ["failed", "completed"]
            assert calls[1]["retry_of"] == calls[0]["attempt_id"]
            assert calls[1]["task_id"] == calls[0]["task_id"]
            assert business.list_orders("u001") == []
            state_key = executor._state_session_id(SESSION_ID, "u001")
            state = executor.session_store.load_agent_state(state_key, executor.MANAGE_AGENT_NAME)
            assert state is not None
            assert any(json.loads(tool_text(block)).get("data", {}).get("confirmation_token")
                       for message in state.context for block in message.content
                       if isinstance(block, ToolResultBlock))
            second = client.post(f"/api/chat/{SESSION_ID}", headers=headers, json={"message": "确认提交"})
            assert second.status_code == 200
            assert len(business.list_orders("u001")) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("accept", ["application/json", "text/event-stream"])
def test_another_task_success_does_not_recover_failed_manage_call(accept):
    application, auth, history, executor, business, engine = create_demo_app(retry_scenario="unresolved")
    try:
        with TestClient(application) as client:
            response = client.post(f"/api/chat/{SESSION_ID}",
                                   headers={"Authorization": auth.login("alice", "123456"), "Accept": accept},
                                   json={"message": "确认提交"})
            if accept == "text/event-stream":
                assert "请确认差旅申请摘要" in response.text
                assert "event: error" in response.text
                assert "event: message_id" not in response.text
            else:
                assert response.status_code == 503
            assert history.list_messages(SESSION_ID, "u001")[-1].role == "user"
            state_key = executor._state_session_id(SESSION_ID, "u001")
            assert executor.session_store.load_agent_state(state_key, executor.MANAGE_AGENT_NAME) is None
            assert business.list_orders("u001") == []
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_retry_reuses_original_arguments_and_only_recovers_its_task():
    records = []
    attempts = DelegationAttempts(records, "trace-demo")
    questions = []

    async def fail(question):
        questions.append(question)
        raise TimeoutError

    async def succeed(question):
        questions.append(question)
        return "已生成摘要"

    result = json.loads(await attempts.run("manage", "Manage", "原始差旅申请", "", fail))
    assert await attempts.run("manage", "Manage", "", result["retry_of"], succeed) == "已生成摘要"
    assert questions == ["原始差旅申请", "原始差旅申请"]
    assert ChatAgentExecutor._completion_error(records, None) is None


@pytest.mark.asyncio
async def test_repeated_failures_are_resolved_only_by_success_on_their_retry_chain():
    records = []
    attempts = DelegationAttempts(records, "trace-demo")

    async def fail(question):
        raise TimeoutError

    async def succeed(question):
        return "已生成摘要"

    first = json.loads(await attempts.run("manage", "Manage", "原始问题", "", fail))
    second = json.loads(await attempts.run("manage", "Manage", "", first["retry_of"], fail))
    assert ChatAgentExecutor._completion_error(records, None) is not None
    await attempts.run("manage", "Manage", "", second["retry_of"], succeed)
    assert len({record["task_id"] for record in records}) == 1
    assert [record["status"] for record in records] == ["failed", "failed", "completed"]
    assert ChatAgentExecutor._completion_error(records, None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unknown", "different_tool", "changed_question", "different_turn", "completed"])
async def test_invalid_retry_cannot_execute_child_or_hide_failure(case):
    records = []
    attempts = DelegationAttempts(records, "trace-demo")

    async def fail(question):
        raise TimeoutError

    async def forbidden(question):
        pytest.fail("无效重试不应运行子 Agent")

    async def succeed(question):
        return "成功"

    result = json.loads(await attempts.run("manage", "Manage", "原始问题", "", fail))
    retry_of = result["retry_of"]
    if case == "completed":
        await attempts.run("manage", "Manage", "独立任务", "", succeed)
        retry_of = records[-1]["attempt_id"]
    if case == "different_turn":
        attempts = DelegationAttempts(records, "trace-next-turn")
    invalid = json.loads(await attempts.run(
        "info" if case == "different_tool" else "manage", "Manage",
        "修改过的申请" if case == "changed_question" else "",
        "missing" if case == "unknown" else retry_of, forbidden,
    ))
    assert invalid["success"] is False
    assert "retry_of" not in invalid
    assert ChatAgentExecutor._completion_error(records, None) is not None


@pytest.mark.asyncio
async def test_delegation_cancellation_propagates_without_retrying():
    records = []

    async def cancel(question):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await DelegationAttempts(records, "trace-demo").run("manage", "Manage", "原始问题", "", cancel)
    assert records[0]["error_type"] == "CancelledError"


@pytest.mark.parametrize("accept", ["application/json", "text/event-stream"])
@pytest.mark.parametrize("failed_agent", ["ItineraryManageAgent", "GoGo"])
def test_sql_state_save_failure_rolls_back_visible_reply_and_all_states(monkeypatch, accept, failed_agent):
    application, auth, history, executor, business, engine = create_demo_app()
    original_save = executor.session_store.save_agent_state

    def fail_save(session_id, state, agent_name="GoGo"):
        if agent_name == failed_agent:
            raise RuntimeError("隔离状态写入失败")
        original_save(session_id, state, agent_name)

    monkeypatch.setattr(executor.session_store, "save_agent_state", fail_save)
    try:
        with TestClient(application, raise_server_exceptions=False) as client:
            response = client.post(f"/api/chat/{SESSION_ID}",
                                   headers={"Authorization": auth.login("alice", "123456"), "Accept": accept},
                                   json={"message": "确认提交"})
            if accept == "text/event-stream":
                assert "event: error" in response.text and "event: message_id" not in response.text
            else:
                assert response.status_code >= 500
            assert history.list_messages(SESSION_ID, "u001")[-1].role == "user"
            state_key = executor._state_session_id(SESSION_ID, "u001")
            assert executor.session_store.load_agent_state(state_key, "ItineraryManageAgent") is None
            assert executor.session_store.load_agent_state(state_key, "GoGo") is None
            assert business.list_orders("u001") == []
    finally:
        engine.dispose()


def test_reply_transaction_failure_does_not_undo_already_committed_business_submission(monkeypatch):
    application, auth, history, executor, business, engine = create_demo_app()
    try:
        with TestClient(application) as client:
            headers = {"Authorization": auth.login("alice", "123456")}
            first = client.post(f"/api/chat/{SESSION_ID}", headers=headers, json={"message": "确认提交"})
            assert "event: message_id" in first.text
            state_key = executor._state_session_id(SESSION_ID, "u001")
            previous = executor.session_store.load_agent_state(state_key, "ItineraryManageAgent").model_dump_json()

            def fail_save(*args, **kwargs):
                raise RuntimeError("隔离聊天状态写入失败")

            monkeypatch.setattr(executor.session_store, "save_agent_state", fail_save)
            second = client.post(f"/api/chat/{SESSION_ID}", headers=headers, json={"message": "确认提交"})
            assert "event: error" in second.text and "event: message_id" not in second.text
            assert history.list_messages(SESSION_ID, "u001")[-1].role == "user"
            assert executor.session_store.load_agent_state(state_key, "ItineraryManageAgent").model_dump_json() == previous
            assert len(business.list_orders("u001")) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("autoflush", [False, True])
def test_first_sql_info_reply_and_active_continuation_commit_together(master_http_case, monkeypatch, autoflush):
    from tests.test_023_master_agent import ScriptedMasterModel, FixedInfoModel

    application, auth, history, executor, _, engine = create_demo_app()
    executor._pipeline_factory = master_http_case[2]._pipeline_factory
    history.repository._session_factory.configure(autoflush=autoflush)
    monkeypatch.setattr(executor, "_build_model", lambda stream=False, role="master":
                        ScriptedMasterModel(stream=stream) if role == "master" else FixedInfoModel())
    try:
        with TestClient(application, raise_server_exceptions=False) as client:
            headers = {"Authorization": auth.login("alice", "123456"), "Accept": "application/json"}
            first = client.post(f"/api/chat/{SESSION_ID}", headers=headers, json={"message": "请介绍一处景点"})
            assert first.status_code == 200
            state_key = executor._state_session_id(SESSION_ID, "u001")
            assert executor.session_store.load_agent_state(state_key, "InfoAgent") is not None
            assert executor.session_store.load_active_agent(state_key).agent_name == "InfoAgent"
            continued = client.post(f"/api/chat/{SESSION_ID}", headers=headers, json={"message": "继续"})
            assert continued.status_code == 200
            assert history.list_messages(SESSION_ID, "u001")[-1].agentName == "InfoAgent"
    finally:
        engine.dispose()
