"""真实 SDK、HTTP 和隔离 SQLite 验证执行时间轴，不访问外部模型/数据库。"""

import asyncio
import json

import pytest
from agentscope.message import ThinkingBlock, TextBlock
from agentscope.model import ChatResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gogo_agent.chat.dependencies import get_chat_history_service
from gogo_agent.chat.repository import SQLChatHistoryRepository, SQLAgentSessionStore
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.db.base import Base
from gogo_agent.request_context import current_trace_id
from scripts.demo_execution_trace import create_demo_app, DemoModel, QUESTION
from tests.test_023_master_agent import FixedInfoModel, ScriptedMasterModel, master_http_case


def parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        name = next(line[7:] for line in lines if line.startswith("event: "))
        data = "\n".join(line[6:] for line in lines if line.startswith("data: "))
        events.append((name, json.loads(data) if name in ("progress", "thinking") else data))
    return events


@pytest.mark.parametrize("accept", ["application/json", "text/event-stream"])
@pytest.mark.parametrize("fail_search", [False, True])
def test_trace_real_delegation_tools_and_authenticated_history(tmp_path, accept, fail_search):
    application, auth, _, executor = create_demo_app(fail_search=fail_search)
    # 使用真正的 SQL 仓储重新读取，避免内存对象恰好还在造成假回放。
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'trace.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    history = ChatHistoryService(SQLChatHistoryRepository(factory))
    executor._history_service = history
    executor._session_store = SQLAgentSessionStore(factory)
    application.dependency_overrides[get_chat_history_service] = lambda: ChatHistoryService(SQLChatHistoryRepository(factory))
    try:
        with TestClient(application) as client:
            token = auth.login("alice", "123456")
            response = client.post("/api/chat/trace-http", headers={"Authorization": token, "Accept": accept},
                                   json={"message": QUESTION})
            assert response.status_code == 200
            if accept == "text/event-stream":
                events = parse_sse(response.text)
                assert any(name == "message_id" for name, _ in events), response.text
                assert not any(name == "error" for name, _ in events)
            messages = client.get("/api/chat/trace-http/messages", headers={"Authorization": token}).json()
            trace = messages[-1]["extra"]["timeline"]
            assert [item["agentName"] for item in trace if item["kind"] == "agent"] == ["GoGo", "ItineraryPlanAgent"]
            assert all(item["status"] == "done" for item in trace if item["kind"] == "agent")
            assert trace[0]["thinkingAvailable"] is True
            assert "itinerary_plan_agent" in trace[0]["detail"]
            child = next(item for item in trace if item["kind"] == "agent" and item["agentName"] == "ItineraryPlanAgent")
            assert child["parentAgentName"] == "GoGo"
            tools = [item for item in trace if item["kind"] == "tool"]
            assert len(tools) == 3
            assert json.loads(tools[1]["arguments"])["origin"] == "北京"
            assert json.loads(tools[1]["result"])["success"] is True
            assert tools[2]["status"] == ("error" if fail_search else "done")
            assert json.loads(tools[2]["result"])["success"] is (not fail_search)
            assert len([item for item in trace if item["kind"] == "thinking"]) == 4
            assert "GoGo 汇总" in messages[-1]["content"]
            if accept == "text/event-stream":
                # 开始事件和思考块的实际到达顺序必须与刷新后的时间轴一致。
                live_order = [
                    ("thinking", data["agentName"]) if name == "thinking" else
                    ("agent" if data["type"] == "agent_start" else "tool", data["agentName"])
                    for name, data in events
                    if (name == "thinking" and data.get("text")) or
                    (name == "progress" and data["type"] in ("agent_start", "tool_call"))
                ]
                assert live_order == [(item["kind"], item["agentName"]) for item in trace]
            denied = client.get("/api/chat/trace-http/messages", headers={"Authorization": auth.login("bob", "123456")})
            assert denied.status_code == 403
    finally:
        engine.dispose()


def test_no_thinking_is_reported_without_fabrication(master_http_case, monkeypatch):
    _, history, executor = master_http_case
    monkeypatch.setattr(executor, "_build_model", lambda stream=False, role="master":
                        ScriptedMasterModel(stream=stream) if role == "master" else FixedInfoModel())
    asyncio.run(executor.execute_turn("no-thinking", "u001", "请介绍一处景点"))
    timeline = history.list_messages("no-thinking", "u001")[-1].extra["timeline"]
    assert not any(item["kind"] == "thinking" for item in timeline)
    assert all(not item["thinkingAvailable"] for item in timeline if item["kind"] == "agent")


@pytest.mark.asyncio
async def test_sse_disconnect_cancels_nested_agent_and_closes_models():
    _, _, history, executor = create_demo_app()
    entered = asyncio.Event()
    stopped = asyncio.Event()
    built = []

    class BlockingPlan(DemoModel):
        async def _call_api(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    def build(stream=False, *, role="master"):
        model = BlockingPlan(role) if role == "plan" else DemoModel(role, stream)
        built.append(model)
        return model

    executor._build_model = build
    stream = await executor.stream_turn_sse("disconnect", "u001", QUESTION)
    received = []
    # 拉到子 Agent 开始后让它阻塞；此时应已经收到父工具和子开始事件。
    while not entered.is_set():
        received.append(await asyncio.wait_for(anext(stream), 3))
    await stream.aclose()
    assert stopped.is_set()
    assert all(model.client.is_closed() for model in built)
    assert current_trace_id() is None
    assert len(history.list_messages("disconnect", "u001")) == 1
    assert not any("event: message_id" in event for event in received)


@pytest.mark.asyncio
async def test_thinking_secret_across_chunks_is_masked_in_sse_and_history():
    _, _, history, executor = create_demo_app()

    class SplitSecret(DemoModel):
        async def _call_api(self, *args, **kwargs):
            async def chunks():
                for fragment in ("api_key=sk-demo-", "secret-value-1234"):
                    yield ChatResponse(content=[ThinkingBlock(id="secret-block", thinking=fragment)], is_last=False)
                yield ChatResponse(content=[TextBlock(text="固定回复")], is_last=False)
            return chunks()

    executor._build_model = lambda stream=False, role="master": SplitSecret(role, stream)
    stream = await executor.stream_turn_sse("masked", "u001", QUESTION)
    output = "".join([event async for event in stream])
    stored = history.list_messages("masked", "u001")[-1].extra["timeline"]
    assert "secret-value" not in output + json.dumps(stored)
    assert "****" in output


@pytest.mark.asyncio
async def test_slow_consumer_applies_backpressure_and_cancels_producer():
    from gogo_agent.chat.trace import ExecutionTrace

    trace = ExecutionTrace(streaming=True)
    stopped = asyncio.Event()
    produced = []

    async def source():
        try:
            for number in range(1000):
                produced.append(number)
                yield str(number)
        finally:
            stopped.set()

    stream = trace.stream(source())
    assert await anext(stream) == "0"
    # 第一项送出后，生产者只能在队列容量附近领先，不能无限堆积。
    assert len(produced) < 1000
    await stream.aclose()
    assert stopped.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected", [("CONFIRMATION_REQUIRED", "pending"), ("NEEDS_INPUT", "pending"), ("SEARCH_FAILED", "error")])
async def test_business_waiting_is_distinct_from_failed_tool(code, expected):
    from types import SimpleNamespace
    from agentscope.event import ToolCallStartEvent, ToolCallEndEvent, ToolResultTextDeltaEvent, ToolResultEndEvent
    from gogo_agent.chat.trace import ExecutionTrace

    trace = ExecutionTrace()
    agent = SimpleNamespace(name="ItineraryManageAgent")
    ids = {"reply_id": "r", "tool_call_id": "t"}
    for event in (
        ToolCallStartEvent(**ids, tool_call_name="check"), ToolCallEndEvent(**ids),
        ToolResultTextDeltaEvent(**ids, delta=json.dumps({"success": False, "errorCode": code})),
        ToolResultEndEvent(**ids, state="success"),
    ):
        await trace._record(agent, event)
    assert trace.timeline[-1]["status"] == expected


def test_nested_profile_pinyin_is_masked_without_masking_city_text():
    from gogo_agent.chat.trace import mask_trace_text
    raw = json.dumps({"profile_json": json.dumps({"name_pinyin": "ZHANG SAN", "base_city": "BEIJING"})})
    masked = json.loads(json.loads(mask_trace_text(raw))["profile_json"])
    assert masked == {"name_pinyin": "****", "base_city": "BEIJING"}
