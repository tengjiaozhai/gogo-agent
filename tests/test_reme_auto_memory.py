"""自动长期记忆（context/reme_memory.py）的清洗、合并、用户隔离与降级测试。

全部使用 httpx.MockTransport 与固定模型替身，不连接真实 ReMe 或模型网关。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from agentscope.message import TextBlock
from agentscope.model import ChatResponse

from gogo_agent.context import reme_memory
from gogo_agent.context.reme import user_memory_prefix


class _FixedModel:
    """返回固定 JSON 的模型替身，记录调用次数。"""

    def __init__(self, payload: str):
        self.payload = payload
        self.calls: list[list] = []
        self.client = None

    async def __call__(self, messages, **kwargs):
        self.calls.append(messages)
        return ChatResponse(
            content=[TextBlock(text=self.payload)], is_last=True, finished_reason="completed",
        )


def _transport(handler):
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(record), seen


def test_normalize_facts_drops_sensitive_long_and_duplicate():
    raw = [
        "用户偏好乘坐国航。",
        " 用户偏好乘坐国航。 ",
        "用户手机号 13800138000，方便联系",
        "用户偏好" + "很" * 100,
        "用户喜欢靠窗座位",
        "用户密码是 abc123",
        123,
    ]

    assert reme_memory.normalize_facts(raw) == ["用户偏好乘坐国航。", "用户喜欢靠窗座位"]


def test_merge_facts_appends_only_new_and_keeps_existing_dates():
    existing = "# 自动记忆\n\n- 2026-11-01 用户偏好乘坐国航\n"

    body, added = reme_memory.merge_facts(
        existing, ["用户偏好乘坐国航", "用户喜欢靠窗座位"], today="2026-11-13",
    )

    assert added == 1
    assert "- 2026-11-01 用户偏好乘坐国航" in body
    assert "- 2026-11-13 用户喜欢靠窗座位" in body


def test_merge_facts_keeps_only_the_most_recent_limit():
    existing = "# 自动记忆\n\n" + "".join(f"- 2026-11-01 事实{i}\n" for i in range(5))

    body, added = reme_memory.merge_facts(existing, ["新事实"], today="2026-11-13", limit=3)

    assert added == 1
    assert "事实0" not in body and "事实1" not in body and "事实2" not in body
    assert "事实3" in body and "事实4" in body and "新事实" in body


def test_remember_turn_writes_only_into_callers_own_prefix():
    model = _FixedModel(json.dumps({"facts": ["用户偏好乘坐国航", "用户喜欢靠窗座位"]}))
    writes: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/read":
            return httpx.Response(200, json={"success": False, "answer": "file does not exist"})
        if request.url.path == "/write":
            writes.append(json.loads(request.content))
            return httpx.Response(200, json={"success": True, "answer": "ok"})
        raise AssertionError(f"unexpected path {request.url.path}")

    transport, seen = _transport(handler)

    added = asyncio.run(reme_memory.remember_turn(
        "u001", "帮我查航班", "已为您查询。", base_url="http://reme.test",
        model=model, transport=transport,
    ))

    assert added == 2
    assert [request.url.path for request in seen] == ["/read", "/write"]
    payload = writes[0]
    assert payload["path"] == user_memory_prefix("u001") + "auto-memory.md"
    assert payload["name"] == "auto-memory"
    assert "用户偏好乘坐国航" in payload["content"]
    assert "用户喜欢靠窗座位" in payload["content"]


def test_remember_turn_refuses_to_overwrite_when_read_fails_unexpectedly():
    model = _FixedModel(json.dumps({"facts": ["用户偏好乘坐国航"]}))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/read"
        return httpx.Response(200, json={"success": False, "answer": "permission denied"})

    transport, _ = _transport(handler)

    assert asyncio.run(reme_memory.remember_turn(
        "u001", "问题", "答复", base_url="http://reme.test", model=model, transport=transport,
    )) == 0


def test_remember_turn_skips_http_when_model_returns_no_facts():
    model = _FixedModel(json.dumps({"facts": []}))
    transport, seen = _transport(lambda _request: pytest.fail("不应发出 HTTP 请求"))

    assert asyncio.run(reme_memory.remember_turn(
        "u001", "问题", "答复", base_url="http://reme.test", model=model, transport=transport,
    )) == 0
    assert seen == []


def test_remember_turn_rejects_blank_user_before_any_call():
    model = _FixedModel(json.dumps({"facts": ["用户偏好乘坐国航"]}))

    with pytest.raises(ValueError):
        asyncio.run(reme_memory.remember_turn(
            "  ", "问题", "答复", base_url="http://reme.test", model=model,
        ))
    assert model.calls == []


def test_executor_schedules_memory_write_as_background_task(monkeypatch):
    from gogo_agent.chat import executor as executor_module
    from gogo_agent.request_context import RequestContext

    calls: list[tuple[str, str, str, str]] = []

    async def fake_remember(user_id, question, answer, *, base_url, model):
        calls.append((user_id, question, answer, base_url))

    monkeypatch.setattr(executor_module, "remember_turn", fake_remember)
    monkeypatch.setenv("GOGO_REME_BASE_URL", "http://reme.test")
    executor = object.__new__(executor_module.ChatAgentExecutor)
    executor._memory_tasks = set()
    executor._build_model = lambda **_kwargs: object()
    request = RequestContext(user_id="u001", session_id="s-1", request_id="r-1")

    async def scenario() -> None:
        executor._schedule_memory_write(request, "问题", "答复")
        assert executor._memory_tasks
        await asyncio.gather(*executor._memory_tasks)

    asyncio.run(scenario())

    assert calls == [("u001", "问题", "答复", "http://reme.test")]
