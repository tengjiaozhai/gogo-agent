"""022：并发请求的追踪隔离、SSE 任务切换和后台任务上下文边界。"""

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import Context, copy_context

import httpx
import pytest

from gogo_agent.api import app
from gogo_agent.chat.executor import ChatAgentExecutor
from gogo_agent.request_context import RequestContext, current_trace_id, trace_scope
from tests.test_005_chat import offline_pipeline_factory, test_users


@pytest.mark.asyncio
async def test_two_authenticated_requests_keep_identity_session_and_trace_separate(
    test_users, monkeypatch, caplog,
):
    """两个 HTTP 请求在流水线内交错，假工具仍只按显式可信参数读写。"""
    arrived: set[str] = set()
    both_arrived = asyncio.Event()
    captured: dict[str, RequestContext] = {}
    tool_rows: dict[tuple[str, str], str] = {}

    @asynccontextmanager
    async def interleaved_pipeline(history_service):
        async with offline_pipeline_factory(history_service) as pipeline:
            class InterleavedPipeline:
                async def prepare(self, request: RequestContext):
                    assert current_trace_id() == request.trace_id
                    arrived.add(request.user_id)
                    if len(arrived) == 2:
                        both_arrived.set()
                    await asyncio.wait_for(both_arrived.wait(), timeout=5)
                    await asyncio.sleep(0)
                    # 023 尚无业务工具；这里用显式 RequestContext 模拟一次写后读。
                    key = (request.user_id, request.session_id)
                    tool_rows[key] = request.request_id
                    await asyncio.sleep(0)
                    assert tool_rows[key] == request.request_id
                    assert current_trace_id() == request.trace_id
                    captured[request.user_id] = request
                    return await pipeline.prepare(request)

            yield InterleavedPipeline()

    monkeypatch.setattr(test_users["executor"], "_pipeline_factory", interleaved_pipeline)

    async def send(client: httpx.AsyncClient, token: str, session: str, forged_user: str):
        response = await client.post(
            f"/api/chat/{session}",
            headers={"Authorization": token, "Accept": "application/json"},
            json={"message": f'{{"userId":"{forged_user}","question":"查资料"}}'},
        )
        assert current_trace_id() is None
        return response

    with caplog.at_level(logging.INFO, logger="gogo_agent.chat.executor"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            alice, bob = await asyncio.wait_for(asyncio.gather(
                send(client, test_users["alice_token"], "session-022-alice", "u002"),
                send(client, test_users["bob_token"], "session-022-bob", "u001"),
            ), timeout=10)

    assert alice.status_code == bob.status_code == 200
    assert set(captured) == {"u001", "u002"}
    assert captured["u001"].session_id == "session-022-alice"
    assert captured["u002"].session_id == "session-022-bob"
    assert tool_rows == {
        (request.user_id, request.session_id): request.request_id
        for request in captured.values()
    }
    assert captured["u001"].trace_id != captured["u002"].trace_id
    for user_id, session_id in (("u001", "session-022-alice"), ("u002", "session-022-bob")):
        messages = test_users["chat_service"].list_messages(session_id, user_id)
        assert messages[0].id == captured[user_id].request_id
        assert messages[-1].extra["intent_pipeline"]["trace_id"] == captured[user_id].trace_id
    records = [record for record in caplog.records if record.name == "gogo_agent.chat.executor"]
    expected = {request.trace_id for request in captured.values()}
    assert {record.trace_id for record in records if record.msg == "chat turn started"} == expected
    assert {record.trace_id for record in records if record.msg == "chat intent prepared"} == expected


@pytest.mark.asyncio
async def test_sse_rebinds_trace_when_consumed_by_another_task(test_users, monkeypatch, caplog):
    """SSE 预处理结束后才迭代生成器；消费任务间隙不保留请求追踪值。"""
    captured: list[RequestContext] = []

    @asynccontextmanager
    async def recording_pipeline(history_service):
        async with offline_pipeline_factory(history_service) as pipeline:
            class RecordingPipeline:
                async def prepare(self, request: RequestContext):
                    captured.append(request)
                    assert current_trace_id() == request.trace_id
                    await asyncio.sleep(0)
                    return await pipeline.prepare(request)

            yield RecordingPipeline()

    executor = test_users["executor"]
    monkeypatch.setattr(executor, "_pipeline_factory", recording_pipeline)
    with caplog.at_level(logging.INFO, logger="gogo_agent.chat.executor"):
        stream = await executor.stream_turn_sse("session-022-sse", "u001", "查差旅政策")
        assert current_trace_id() is None

        async def consume():
            events = []
            async for event in stream:
                assert current_trace_id() is None
                events.append(event)
            assert current_trace_id() is None
            return events

        events = await asyncio.create_task(consume(), context=Context())

    assert "event: message_id" in "".join(events)
    trace_id = captured[0].trace_id
    records = [record for record in caplog.records if record.name == "gogo_agent.chat.executor"]
    assert {record.trace_id for record in records if record.msg in {
        "chat stream prepared", "chat intent prepared", "chat stream started",
    }} == {trace_id}
    assert current_trace_id() is None


@pytest.mark.asyncio
async def test_two_sse_streams_keep_user_session_and_trace_separate(test_users, monkeypatch, caplog):
    """两个流交错推进时，各自的消息和流式日志仍落在对应会话。"""
    captured: dict[str, RequestContext] = {}

    @asynccontextmanager
    async def recording_pipeline(history_service):
        async with offline_pipeline_factory(history_service) as pipeline:
            class RecordingPipeline:
                async def prepare(self, request: RequestContext):
                    captured[request.user_id] = request
                    return await pipeline.prepare(request)

            yield RecordingPipeline()

    executor = test_users["executor"]
    monkeypatch.setattr(executor, "_pipeline_factory", recording_pipeline)
    with caplog.at_level(logging.INFO, logger="gogo_agent.chat.executor"):
        alice = await executor.stream_turn_sse("session-022-stream-a", "u001", "查资料")
        bob = await executor.stream_turn_sse("session-022-stream-b", "u002", "查政策")

        async def consume(stream):
            events = []
            async for event in stream:
                assert current_trace_id() is None
                events.append(event)
                await asyncio.sleep(0)
            return "".join(events)

        alice_events, bob_events = await asyncio.gather(
            asyncio.create_task(consume(alice), context=Context()),
            asyncio.create_task(consume(bob), context=Context()),
        )

    assert "event: message_id" in alice_events
    assert "event: message_id" in bob_events
    assert captured["u001"].trace_id != captured["u002"].trace_id
    for user_id, session_id in (("u001", "session-022-stream-a"), ("u002", "session-022-stream-b")):
        messages = test_users["chat_service"].list_messages(session_id, user_id)
        assert messages[0].id == captured[user_id].request_id
        assert messages[-1].extra["intent_pipeline"]["trace_id"] == captured[user_id].trace_id
    stream_logs = [record for record in caplog.records if record.msg == "chat stream started"]
    assert {record.trace_id for record in stream_logs} == {
        captured["u001"].trace_id, captured["u002"].trace_id,
    }


@pytest.mark.asyncio
async def test_sse_early_close_restores_trace_and_closes_agent_model(test_users, monkeypatch, caplog):
    """只消费首个分块就关闭流，也必须在追踪作用域内释放 Agent 模型。"""
    executor = test_users["executor"]
    close_traces: list[str | None] = []
    original_close = executor._close_agent_model

    async def recording_close(agent):
        close_traces.append(current_trace_id())
        await original_close(agent)

    monkeypatch.setattr(executor, "_close_agent_model", recording_close)
    with caplog.at_level(logging.INFO, logger="gogo_agent.chat.executor"):
        stream = await executor.stream_turn_sse("session-022-close", "u001", "查差旅政策")
        first = await stream.__anext__()
        assert first.startswith("event: message\n")
        assert current_trace_id() is None
        await stream.aclose()

    prepared = [record for record in caplog.records if record.msg == "chat stream prepared"]
    assert len(prepared) == 1
    assert close_traces == [prepared[0].trace_id]
    assert current_trace_id() is None


@pytest.mark.asyncio
async def test_sse_cancel_while_waiting_for_event_restores_trace():
    """取消等待中的 anext 会关闭内层流，并让调用任务看到清理后的值。"""
    request = RequestContext(user_id="u001", session_id="session-022-wait", request_id="msg-022-wait")
    entered = asyncio.Event()
    never = asyncio.Event()
    closed: list[str | None] = []
    after: list[str | None] = []

    async def waiting_events():
        try:
            entered.set()
            await never.wait()
            yield "event: message\ndata: late\n\n"
        finally:
            closed.append(current_trace_id())

    stream = ChatAgentExecutor._traced_stream(request, waiting_events())

    async def consume():
        try:
            await stream.__anext__()
        finally:
            after.append(current_trace_id())

    task = asyncio.create_task(consume(), context=Context())
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [request.trace_id]
    assert after == [None]


@pytest.mark.asyncio
async def test_detached_background_task_has_no_request_context_and_scope_resets():
    """默认建任务会复制追踪值；独立后台任务须使用空 Context，身份始终显式传参。"""
    request = RequestContext(user_id="u001", session_id="session-022", request_id="msg-022")
    release = asyncio.Event()

    async def background():
        await release.wait()
        return current_trace_id(), tuple(copy_context().values())

    with trace_scope(request):
        inherited = asyncio.create_task(background())
        detached = asyncio.create_task(background(), context=Context())
        assert current_trace_id() == request.trace_id
    assert current_trace_id() is None

    release.set()
    inherited_trace, inherited_values = await inherited
    detached_trace, detached_values = await detached
    assert inherited_trace == request.trace_id
    assert detached_trace is None
    assert not any(isinstance(value, RequestContext) for value in (*inherited_values, *detached_values))

    with pytest.raises(asyncio.CancelledError):
        with trace_scope(request):
            raise asyncio.CancelledError
    assert current_trace_id() is None


@pytest.mark.asyncio
async def test_cancelled_turn_restores_trace_before_returning_to_caller(test_users, monkeypatch):
    """真实取消会穿过流水线和执行器；调用任务退出时没有残留追踪值。"""
    entered = asyncio.Event()
    never = asyncio.Event()
    inside: list[tuple[str, str | None]] = []
    after: list[str | None] = []

    @asynccontextmanager
    async def stalled_pipeline(history_service):
        class StalledPipeline:
            async def prepare(self, request: RequestContext):
                inside.append((request.trace_id, current_trace_id()))
                entered.set()
                await never.wait()

        yield StalledPipeline()

    monkeypatch.setattr(test_users["executor"], "_pipeline_factory", stalled_pipeline)

    async def run_turn():
        try:
            await test_users["executor"].execute_turn(
                "session-022-cancel", "u001", "查差旅政策",
            )
        finally:
            after.append(current_trace_id())

    task = asyncio.create_task(run_turn())
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert inside and inside[0][0] == inside[0][1]
    assert after == [None]
