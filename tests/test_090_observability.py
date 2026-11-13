"""090 可观测性：失败日志能从 trace_id 定位到失败的运行器、工具与上游错误类别，且不含明文密钥。"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from agentscope.event import ToolCallEndEvent, ToolCallStartEvent, ToolResultEndEvent, ToolResultTextDeltaEvent

from gogo_agent.chat.trace import ExecutionTrace
from gogo_agent.integrations.skill_runner import SkillError
from gogo_agent.request_context import RequestContext, trace_scope
from tests.test_091_redaction import ARGS, KEY, assert_no_plain, env, make_runner

TRACE_ID = "trace-090"


def request_context() -> RequestContext:
    return RequestContext(user_id="alice", session_id="s-090", request_id="m-090", trace_id=TRACE_ID)


@pytest.mark.asyncio
async def test_090_runner_logs_carry_trace_and_error_category(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    with trace_scope(request_context()):
        await make_runner(tmp_path).run_json(user_id="alice", args=ARGS, env=env("ok"), secrets=[KEY])
        with pytest.raises(SkillError):
            await make_runner(tmp_path).run_json(user_id="alice", args=ARGS, env=env("fail"), secrets=[KEY])
    assert f"trace={TRACE_ID} ok exit=0" in caplog.text
    assert f"trace={TRACE_ID} failed code=NONZERO_EXIT" in caplog.text
    assert "skill=fake-cli" in caplog.text
    assert_no_plain(caplog.text)


@pytest.mark.asyncio
async def test_090_failed_tool_result_logs_tool_and_upstream_error_code(caplog):
    caplog.set_level(logging.WARNING)
    trace = ExecutionTrace()
    agent = SimpleNamespace(name="ItineraryPlanAgent")
    ids = {"reply_id": "r-090", "tool_call_id": "t-090"}
    failure = json.dumps({"success": False, "errorCode": "VENDOR_REJECTED", "message": f"供应商拒绝 {KEY}"},
                         ensure_ascii=False)
    with trace_scope(request_context()):
        for event in (
            ToolCallStartEvent(**ids, tool_call_name="search_flights"), ToolCallEndEvent(**ids),
            ToolResultTextDeltaEvent(**ids, delta=failure), ToolResultEndEvent(**ids, state="error"),
        ):
            await trace._record(agent, event)
    assert trace.timeline[-1]["status"] == "error"
    assert (f"trace={TRACE_ID} agent=ItineraryPlanAgent tool=search_flights state=error "
            "code=VENDOR_REJECTED") in caplog.text
    assert_no_plain(caplog.text + json.dumps(trace.timeline, ensure_ascii=False))


@pytest.mark.asyncio
async def test_090_agent_exception_logs_trace_agent_and_error_type_only(caplog):
    caplog.set_level(logging.WARNING)
    trace = ExecutionTrace()
    agent = SimpleNamespace(name="ItineraryPlanAgent")

    async def failing(**_kwargs):
        raise RuntimeError(f"上游拒绝 {KEY}")
        yield  # 使其成为异步生成器

    with trace_scope(request_context()):
        with pytest.raises(RuntimeError):
            async for _ in trace.on_reply(agent, {}, failing):
                pass
    assert f"trace={TRACE_ID} agent=ItineraryPlanAgent error_type=RuntimeError" in caplog.text
    assert_no_plain(caplog.text)
