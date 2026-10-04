"""023：HTTP 意图流水线经 AgentScope Toolkit 委派只读信息子 Agent。"""

import json
from contextlib import asynccontextmanager

import pytest
from agentscope.event import ToolResultEndEvent, ToolResultStartEvent
from agentscope.message import TextBlock, ToolCallBlock, ToolResultBlock, ToolResultState, UserMsg
from agentscope.model import ChatResponse
from agentscope.state import AgentState
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
    ConfidenceLevel, FastMatch, IntentCandidate, IntentCategory, IntentItem,
    IntentPipelineService, IntentRecognizer, IntentResult, MatchStatus,
    QueryInput, RecognitionLayer, RewriteContextBuilder,
)


CHILD_ANSWER = "离线信息子 Agent 的固定查询结果"


class FixedGeneralInfoRule:
    """让真实 017 流水线在 L1 得到通用信息意图。"""

    def match(self, query: QueryInput) -> FastMatch:
        result = IntentResult(
            intents=[IntentItem(
                intent=IntentCategory.GENERAL_INFO,
                confidence=ConfidenceLevel.HIGH,
                reason="023 固定通用信息分类",
                evidence=[query.question],
            )],
            primary_intent=IntentCategory.GENERAL_INFO,
            multi_intent=False,
            overall_reason="023 固定只读查询",
        )
        return FastMatch(
            status=MatchStatus.HIT,
            result=result,
            threshold=None,
            candidates=[IntentCandidate(
                intent=IntentCategory.GENERAL_INFO,
                layer=RecognitionLayer.RULE,
                score=None,
                reason="023 本地规则命中",
            )],
            reason="023 本地规则命中",
        )


class UnusedRewriter:
    """L1 命中时不调用外部改写模型。"""

    async def rewrite(self, context):
        raise AssertionError("023 查询不应触发改写")


class ScriptedMasterModel(_FallbackMockModel):
    """只替换供应商响应；真实 AgentScope Agent 仍执行工具循环。"""

    def __init__(self, *, stream: bool, requested_tool: str = "info_agent"):
        super().__init__(stream=stream)
        self.requested_tool = requested_tool
        self.tool_names: list[list[str]] = []
        self.seen_tool_result = False

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.call_count += 1
        self.tool_names.append([item["function"]["name"] for item in tools or []])
        if self.call_count == 1:
            content = [ToolCallBlock(
                id="tool-023", name=self.requested_tool,
                input=json.dumps({"question": "请介绍一处景点"}, ensure_ascii=False),
            )]
        else:
            tool_results = [
                block for msg in messages for block in msg.content
                if isinstance(block, ToolResultBlock)
            ]
            self.seen_tool_result = bool(tool_results)
            content = [TextBlock(text=(
                f"主 Agent 汇总：{CHILD_ANSWER}"
                if any(CHILD_ANSWER in str(result.output) for result in tool_results)
                else "主 Agent 未得到已注册工具的结果"
            ))]
        response = ChatResponse(content=content, is_last=not self.stream)
        if not self.stream:
            return response

        async def one_chunk():
            yield response

        return one_chunk()


class FixedInfoModel(_FallbackMockModel):
    """信息子 Agent 的离线模型；拒绝任何工具 schema。"""

    def __init__(self):
        super().__init__(stream=False)
        self.calls = 0

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        assert not tools
        self.calls += 1
        return ChatResponse(content=[TextBlock(text=CHILD_ANSWER)], is_last=True)


@pytest.fixture
def master_http_case(monkeypatch):
    """内存 Token、业务历史和状态，保留实际 FastAPI 路由与意图流水线。"""
    auth = AuthService(
        repository=InMemoryUserAccountRepository(load_default_seeds=True),
        token_manager=TokenManager(store=InMemoryTokenStore()),
    )
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())

    @asynccontextmanager
    async def pipeline_factory(history_service):
        model = _FallbackMockModel()
        try:
            yield IntentPipelineService(
                RewriteContextBuilder(history_service),
                UnusedRewriter(),
                IntentRecognizer(model, rule_matcher=FixedGeneralInfoRule()),
            )
        finally:
            await model.client.close()

    executor = ChatAgentExecutor(history, InMemoryAgentSessionStore(), pipeline_factory=pipeline_factory)
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_auth_service] = lambda: auth
    app.dependency_overrides[get_chat_executor] = lambda: executor
    app.dependency_overrides[get_chat_history_service] = lambda: history
    try:
        yield auth, history, executor
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize("accept", ("application/json", "text/event-stream"))
def test_http_master_calls_registered_read_only_info_agent_and_records_result(
    master_http_case, monkeypatch, accept,
):
    """真实 HTTP→L1→GoGo→FunctionTool→InfoAgent→GoGo→回复。"""
    auth, history, executor = master_http_case
    master = ScriptedMasterModel(stream=accept == "text/event-stream")
    child = FixedInfoModel()
    built = []
    child_requests = []
    original_build_info = executor._build_info_agent

    def build_info(request):
        child_requests.append(request)
        agent = original_build_info(request)
        assert agent.name == "InfoAgent"
        return agent

    def build_model(stream=False):
        built.append(stream)
        return master if len(built) == 1 else child

    monkeypatch.setattr(executor, "_build_model", build_model)
    monkeypatch.setattr(executor, "_build_info_agent", build_info)
    session_id = "session-023-json" if accept == "application/json" else "session-023-sse"
    response = TestClient(app).post(
        f"/api/chat/{session_id}",
        headers={"Authorization": auth.login("alice", "123456"), "Accept": accept},
        json={"message": json.dumps({"userId": "u002", "question": "请介绍一处景点"})},
    )
    assert response.status_code == 200
    if accept == "application/json":
        assert CHILD_ANSWER in response.json()["content"]
    else:
        assert CHILD_ANSWER in response.text
        assert "event: message_id" in response.text
    messages = history.list_messages(session_id, "u001")
    assert len(messages) == 2
    assert CHILD_ANSWER in messages[1].content
    assert messages[1].agentName == "GoGo"
    assert messages[1].extra["intent_pipeline"]["dispatch_target"] == "GoGo"
    assert messages[1].extra["tool_calls"] == [{
        "tool_name": "info_agent",
        "agent_name": "InfoAgent",
        "status": "completed",
        "trace_id": messages[1].extra["intent_pipeline"]["trace_id"],
    }]
    assert master.tool_names == [["info_agent"], ["info_agent"]]
    assert master.seen_tool_result and child.calls == 1
    assert len(child_requests) == 1
    assert child_requests[0].user_id == "u001"
    assert child_requests[0].session_id == session_id
    assert child.client.is_closed() and master.client.is_closed()


@pytest.mark.asyncio
async def test_unregistered_tool_is_rejected_before_child_execution(master_http_case, monkeypatch):
    """模型伪造 write_order 调用；AgentScope 返回错误事件，子 Agent 不执行。"""
    _, history, executor = master_http_case
    model = ScriptedMasterModel(stream=True, requested_tool="write_order")
    monkeypatch.setattr(executor, "_build_model", lambda stream=False: model)
    monkeypatch.setattr(
        executor, "_build_info_agent",
        lambda request: (_ for _ in ()).throw(AssertionError("未注册工具不得进入子 Agent")),
    )
    request = executor._save_user_turn("session-023-forged", "u001", "请介绍一处景点")
    prepared = await executor._prepare_turn(request)
    runs: list[dict[str, str]] = []
    agent = executor._build_agent(
        state=AgentState(session_id=request.session_id),
        prepared=prepared, request=request, tool_calls=runs, stream=True,
    )
    try:
        events = [event async for event in agent.reply_stream(
            UserMsg(name="user", content=prepared.effective_question.question),
            yield_final_msg=True,
        )]
    finally:
        await model.client.close()
    assert model.tool_names == [["info_agent"], ["info_agent"]]
    assert model.seen_tool_result
    assert any(isinstance(event, ToolResultStartEvent) and event.tool_call_name == "write_order" for event in events)
    assert any(isinstance(event, ToolResultEndEvent) and event.state == ToolResultState.ERROR for event in events)
    assert runs == []
    assert history.list_messages("session-023-forged", "u001")[0].content == "请介绍一处景点"


@pytest.mark.asyncio
async def test_non_info_intent_has_no_info_tool(master_http_case):
    """其他业务意图仍进入 GoGo，但不会获得信息子 Agent 工具。"""
    _, _, executor = master_http_case
    request = executor._save_user_turn("session-023-policy", "u001", "查询差旅政策")
    prepared = await executor._prepare_turn(request)
    policy = IntentResult(
        intents=[IntentItem(
            intent=IntentCategory.POLICY_QUERY,
            confidence=ConfidenceLevel.HIGH,
            reason="固定非通用信息意图",
            evidence=["查询差旅政策"],
        )],
        primary_intent=IntentCategory.POLICY_QUERY,
        multi_intent=False,
        overall_reason="固定非通用信息意图",
    )
    decision = prepared.decision.model_copy(update={"result": policy})
    prepared = prepared.model_copy(update={
        "fast_decision": decision,
        "decision": decision,
    })
    agent = executor._build_agent(
        state=AgentState(session_id=request.session_id),
        prepared=prepared,
        request=request,
        tool_calls=[],
    )
    try:
        assert await agent.toolkit.get_tool_schemas() == []
        assert "本轮没有可调用的业务子智能体" in agent._system_prompt
    finally:
        await executor._close_agent_model(agent)
