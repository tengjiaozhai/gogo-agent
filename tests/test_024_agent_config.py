"""024：启动配置、轮次/超时、错误出口和状态生命周期。"""

import asyncio

import pytest
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatResponse
from agentscope.state import AgentState
from fastapi.testclient import TestClient

from gogo_agent.api import app
from gogo_agent.chat.config import ChatAgentSettings, load_chat_agent_settings
from gogo_agent.chat.executor import ChatAgentExecutor, _FallbackMockModel
from gogo_agent.chat.repository import InMemoryAgentSessionStore, InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.request_context import current_trace_id
from tests.test_023_master_agent import FixedInfoModel, ScriptedMasterModel, master_http_case


def test_api_startup_rejects_missing_model_settings_without_exposing_key(monkeypatch):
    """ASGI lifespan 在接受请求前失败；错误只列配置名。"""
    monkeypatch.setenv("GOGO_MODEL_API_KEY", "secret-that-must-not-appear")
    monkeypatch.setenv("GOGO_MODEL_NAME", "")
    monkeypatch.setenv("GOGO_MODEL_BASE_URL", "")
    with pytest.raises(ValueError) as error:
        with TestClient(app):
            pass
    assert "GOGO_MODEL_BASE_URL" in str(error.value)
    assert "secret-that-must-not-appear" not in str(error.value)


def test_api_startup_accepts_complete_settings_without_network(monkeypatch):
    """提供三个模型变量后启动 API；健康检查无需探测模型网关。"""
    monkeypatch.setenv("GOGO_MODEL_API_KEY", "local-test-key")
    monkeypatch.setenv("GOGO_MODEL_NAME", "test-model")
    monkeypatch.setenv("GOGO_MODEL_BASE_URL", "https://gateway.example.test/v1")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_agent_settings_use_java_baseline_and_can_change_from_environment(monkeypatch):
    """轮次取 Java Master/Info 的 15/5，改变配置无需改业务规则。"""
    names = (
        "GOGO_MASTER_MAX_ITERS", "GOGO_INFO_MAX_ITERS",
        "GOGO_INFO_TOOL_TIMEOUT_SECONDS", "GOGO_AGENT_MODEL_TIMEOUT_SECONDS",
    )
    for name in names:
        monkeypatch.setenv(name, "")
    assert load_chat_agent_settings() == ChatAgentSettings(
        master_max_iters=15,
        info_max_iters=5,
        info_tool_timeout_seconds=60,
        model_timeout_seconds=60,
    )
    monkeypatch.setenv("GOGO_MASTER_MAX_ITERS", "2")
    monkeypatch.setenv("GOGO_INFO_MAX_ITERS", "3")
    monkeypatch.setenv("GOGO_INFO_TOOL_TIMEOUT_SECONDS", "0.25")
    monkeypatch.setenv("GOGO_AGENT_MODEL_TIMEOUT_SECONDS", "8")
    assert load_chat_agent_settings() == ChatAgentSettings(
        master_max_iters=2,
        info_max_iters=3,
        info_tool_timeout_seconds=0.25,
        model_timeout_seconds=8,
    )


@pytest.mark.parametrize("name,value", (
    ("GOGO_MASTER_MAX_ITERS", "0"),
    ("GOGO_INFO_MAX_ITERS", "invalid"),
    ("GOGO_INFO_TOOL_TIMEOUT_SECONDS", "-1"),
    ("GOGO_AGENT_MODEL_TIMEOUT_SECONDS", "nan"),
))
def test_invalid_agent_settings_name_the_field_without_echoing_value(monkeypatch, name, value):
    for setting in (
        "GOGO_MASTER_MAX_ITERS", "GOGO_INFO_MAX_ITERS",
        "GOGO_INFO_TOOL_TIMEOUT_SECONDS", "GOGO_AGENT_MODEL_TIMEOUT_SECONDS",
    ):
        monkeypatch.setenv(setting, "")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as error:
        load_chat_agent_settings()
    assert name in str(error.value)
    assert value not in str(error.value)


@pytest.mark.asyncio
async def test_agent_configures_turn_limits_serial_tool_and_model_timeout(
    master_http_case, monkeypatch,
):
    """同一份设置落实到主/子 Agent、只读工具与模型客户端。"""
    _, _, executor = master_http_case
    executor._settings = ChatAgentSettings(
        master_max_iters=2, info_max_iters=3,
        info_tool_timeout_seconds=0.25, model_timeout_seconds=8,
    )
    monkeypatch.setattr(executor, "_build_model", lambda stream=False, role="master": _FallbackMockModel(stream=stream))
    request = executor._save_user_turn("session-024-config", "u001", "请介绍一处景点")
    prepared = await executor._prepare_turn(request)
    master = executor._build_agent(
        state=AgentState(session_id=request.session_id),
        prepared=prepared, request=request, tool_calls=[],
    )
    info = executor._build_info_agent(request)
    try:
        assert master.react_config.max_iters == 2
        assert info.react_config.max_iters == 3
        assert master.model_config.max_retries == info.model_config.max_retries == 0
        tool = await master.toolkit.get_tool("info_agent")
        assert tool.is_read_only and not tool.is_concurrency_safe
        assert await info.toolkit.get_tool_schemas() == []
    finally:
        await executor._close_agent_model(master)
        await executor._close_agent_model(info)


@pytest.mark.asyncio
async def test_chat_model_has_bounded_client_timeout_and_no_hidden_retries(monkeypatch):
    """主 Agent 使用同一网关配置，并关闭框架与底层 SDK 的自动重试。"""
    monkeypatch.setenv("GOGO_MODEL_API_KEY", "local-test-key")
    monkeypatch.setenv("GOGO_MODEL_NAME", "test-model")
    monkeypatch.setenv("GOGO_MODEL_BASE_URL", "https://gateway.example.test/v1")
    configured = ChatAgentExecutor(
        ChatHistoryService(InMemoryChatHistoryRepository()),
        InMemoryAgentSessionStore(),
        settings=ChatAgentSettings(model_timeout_seconds=8),
    )
    model = configured._build_model()
    try:
        assert model.max_retries == model.client.max_retries == 0
        assert model.client.timeout == 8
    finally:
        await model.client.close()


class SlowInfoModel(_FallbackMockModel):
    """只用于证明 InfoAgent 工具超时，不连接外部模型。"""

    def __init__(self):
        super().__init__(stream=False)

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        await asyncio.sleep(1)
        return ChatResponse(content=[TextBlock(text="迟到的结果")], is_last=True)


class RepeatingMasterModel(_FallbackMockModel):
    """始终提出工具调用，用于触发真实 AgentScope 最大轮次出口。"""

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.call_count += 1
        response = ChatResponse(content=[ToolCallBlock(
            id=f"repeat-{self.call_count}",
            name="info_agent",
            input='{"question":"请介绍一处景点"}',
        )], is_last=not self.stream)
        if not self.stream:
            return response

        async def chunks():
            yield response

        return chunks()


@pytest.mark.parametrize("accept", ("application/json", "text/event-stream"))
def test_info_timeout_is_reported_without_success_reply(master_http_case, monkeypatch, accept):
    """工具超时后不保存助手成功消息和新 AgentState。"""
    auth, history, executor = master_http_case
    executor._settings = ChatAgentSettings(info_tool_timeout_seconds=0.01)
    master = ScriptedMasterModel(stream=accept == "text/event-stream")
    child = SlowInfoModel()
    built = []

    def build_model(stream=False, *, role="master"):
        built.append(stream)
        return master if role == "master" else child

    monkeypatch.setattr(executor, "_build_model", build_model)
    session_id = f"session-024-timeout-{accept.replace('/', '-')}"
    response = TestClient(app).post(
        f"/api/chat/{session_id}",
        headers={"Authorization": auth.login("alice", "123456"), "Accept": accept},
        json={"message": "请介绍一处景点"},
    )
    if accept == "application/json":
        assert response.status_code == 503
        assert "TimeoutError" in response.json()["message"]
    else:
        assert "event: error" in response.text and "TimeoutError" in response.text
        assert "event: message_id" not in response.text
    assert [message.role for message in history.list_messages(session_id, "u001")] == ["user"]
    assert executor.session_store.load_agent_state(
        executor._state_session_id(session_id, "u001"), agent_name="GoGo",
    ) is None
    assert executor.session_store.load_agent_state(
        executor._state_session_id(session_id, "u001"), agent_name="InfoAgent",
    ) is None
    assert executor.session_store.load_active_agent(executor._state_session_id(session_id, "u001")) is None
    assert child.client.is_closed() and master.client.is_closed()


@pytest.mark.parametrize("accept", ("application/json", "text/event-stream"))
def test_iteration_limit_is_reported_without_success_reply(master_http_case, monkeypatch, accept):
    """AgentScope 轮次耗尽不被业务保存为完成回复。"""
    auth, history, executor = master_http_case
    executor._settings = ChatAgentSettings(master_max_iters=1)
    master = RepeatingMasterModel(stream=accept == "text/event-stream")
    child = FixedInfoModel()
    built = []

    def build_model(stream=False, *, role="master"):
        built.append(stream)
        return master if role == "master" else child

    monkeypatch.setattr(executor, "_build_model", build_model)
    session_id = f"session-024-limit-{accept.replace('/', '-')}"
    response = TestClient(app).post(
        f"/api/chat/{session_id}",
        headers={"Authorization": auth.login("alice", "123456"), "Accept": accept},
        json={"message": "请介绍一处景点"},
    )
    if accept == "application/json":
        assert response.status_code == 503
        assert "最大推理轮次" in response.json()["message"]
    else:
        assert "event: error" in response.text and "最大推理轮次" in response.text
        assert "event: message_id" not in response.text
    assert master.call_count == 2
    assert [message.role for message in history.list_messages(session_id, "u001")] == ["user"]
    assert executor.session_store.load_agent_state(
        executor._state_session_id(session_id, "u001"), agent_name="GoGo",
    ) is None
    assert executor.session_store.load_agent_state(
        executor._state_session_id(session_id, "u001"), agent_name="InfoAgent",
    ) is None
    assert executor.session_store.load_active_agent(executor._state_session_id(session_id, "u001")) is None


@pytest.mark.asyncio
async def test_cancellation_during_master_model_closes_client_without_saving_state(
    master_http_case, monkeypatch,
):
    """取消在 Agent 推理中发生时，请求清理追踪值和模型客户端。"""
    _, history, executor = master_http_case
    entered = asyncio.Event()
    never = asyncio.Event()
    after: list[str | None] = []

    class BlockingMasterModel(_FallbackMockModel):
        async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
            entered.set()
            await never.wait()

    model = BlockingMasterModel()
    monkeypatch.setattr(executor, "_build_model", lambda stream=False: model)

    async def run_turn():
        try:
            await executor.execute_turn("session-024-cancel", "u001", "请介绍一处景点")
        finally:
            after.append(current_trace_id())

    task = asyncio.create_task(run_turn())
    await asyncio.wait_for(entered.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert after == [None]
    assert [message.role for message in history.list_messages("session-024-cancel", "u001")] == ["user"]
    assert executor.session_store.load_agent_state(
        executor._state_session_id("session-024-cancel", "u001"), agent_name="GoGo",
    ) is None
    assert model.client.is_closed()
