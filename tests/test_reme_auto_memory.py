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

    body, added, evicted = reme_memory.merge_facts(
        existing, ["用户偏好乘坐国航", "用户喜欢靠窗座位"], today="2026-11-13",
    )

    assert added == 1
    assert evicted == []
    assert "- 2026-11-01 用户偏好乘坐国航" in body
    assert "- 2026-11-13 用户喜欢靠窗座位" in body


def test_merge_facts_keeps_only_the_most_recent_limit():
    existing = "# 自动记忆\n\n" + "".join(f"- 2026-11-01 事实{i}\n" for i in range(5))

    body, added, evicted = reme_memory.merge_facts(existing, ["新事实"], today="2026-11-13", limit=3)

    assert added == 1
    assert "事实0" not in body and "事实1" not in body and "事实2" not in body
    assert "事实3" in body and "事实4" in body and "新事实" in body
    assert evicted == [
        "- 2026-11-01 事实0", "- 2026-11-01 事实1", "- 2026-11-01 事实2",
    ]


def test_normalize_facts_counts_overflow_without_stopping_scan(caplog):
    raw = [f"用户偏好第{i}项" for i in range(7)]

    with caplog.at_level("WARNING"):
        selected = reme_memory.normalize_facts(raw, limit=5)

    assert selected == [f"用户偏好第{i}项" for i in range(5)]
    assert "dropped=2" in caplog.text


def test_remember_turn_writes_only_into_callers_own_prefix():
    model = _FixedModel(json.dumps({"facts": ["用户偏好乘坐国航", "用户喜欢靠窗座位"]}))
    fake = _FakeReme()
    transport, _ = _transport(fake.handler)

    added = asyncio.run(reme_memory.remember_turn(
        "u001", "帮我查航班", "已为您查询。", base_url="http://reme.test",
        model=model, transport=transport,
    ))

    main = user_memory_prefix("u001") + "auto-memory.md"
    assert added == 2
    assert [s["path"] for s in fake.saves] == [main]
    assert "expected_mtime" not in fake.saves[0]  # 文件此前不存在
    content = fake.files[main]
    assert content.startswith("---\nname: auto-memory\n")
    assert "用户偏好乘坐国航" in content
    assert "用户喜欢靠窗座位" in content


def test_remember_turn_refuses_to_overwrite_when_read_fails_unexpectedly():
    model = _FixedModel(json.dumps({"facts": ["用户偏好乘坐国航"]}))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/stat":
            return httpx.Response(200, json={"success": True, "answer": "ok", "metadata": {
                "exists": True, "mtime": "2026-11-15T13:40:46.495873",
            }})
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


class _FakeReme:
    """内存版 ReMe 文件接口：/stat 返回 mtime，/save 校验 expected_mtime 并返回 file_conflict。"""

    def __init__(self):
        self.files: dict[str, str] = {}
        self.mtimes: dict[str, str] = {}
        self.saves: list[dict] = []
        self.stats = 0
        self.before_read = None
        self._tick = 0

    def bump(self, path: str) -> None:
        self._tick += 1
        self.mtimes[path] = f"2026-11-16T10:00:{self._tick:06d}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = body.get("path")
        if request.url.path == "/stat":
            self.stats += 1
            if path not in self.files:
                return httpx.Response(200, json={"success": False, "answer": "not found", "metadata": {"exists": False}})
            return httpx.Response(200, json={"success": True, "answer": "ok", "metadata": {
                "exists": True, "mtime": self.mtimes[path],
            }})
        if request.url.path == "/read":
            if self.before_read is not None:
                self.before_read(path)
            if path not in self.files:
                return httpx.Response(200, json={"success": False, "answer": "Error: file does not exist"})
            return httpx.Response(200, json={"success": True, "answer": self.files[path], "metadata": {}})
        if request.url.path == "/save":
            self.saves.append(body)
            expected = body.get("expected_mtime")
            if expected is not None and self.mtimes.get(path) != expected:
                return httpx.Response(200, json={"success": False, "answer": "conflict",
                                                 "metadata": {"code": "file_conflict"}})
            self.files[path] = body["content"]
            self.bump(path)
            return httpx.Response(200, json={"success": True, "answer": "ok", "metadata": {}})
        raise AssertionError(f"unexpected path {request.url.path}")


def test_write_conflict_rereads_and_merges_both_writers_facts():
    fake = _FakeReme()
    main = user_memory_prefix("u001") + "auto-memory.md"
    fake.files[main] = "# 自动记忆\n\n- 2026-11-01 用户偏好乘坐国航\n"
    fake.bump(main)
    fired = {"done": False}

    def other_writer(path: str) -> None:
        # 在本进程 stat 之后、read 之前，另一个写入者追加了一条事实。
        if path == main and not fired["done"]:
            fired["done"] = True
            fake.files[main] += "- 2026-11-02 用户住酒店偏好大床房\n"
            fake.bump(main)

    fake.before_read = other_writer
    transport, _ = _transport(fake.handler)
    model = _FixedModel(json.dumps({"facts": ["用户喜欢靠窗座位"]}))

    added = asyncio.run(reme_memory.remember_turn(
        "u001", "问题", "答复", base_url="http://reme.test", model=model, transport=transport,
    ))

    assert added == 1
    assert fake.stats == 2  # 第一次 stat 后被冲突打回，重读一次
    assert [s["path"] for s in fake.saves] == [main, main]
    assert "expected_mtime" in fake.saves[0]
    final = fake.files[main]
    assert "用户偏好乘坐国航" in final
    assert "用户住酒店偏好大床房" in final
    assert "用户喜欢靠窗座位" in final


def test_overflow_is_moved_to_history_and_not_left_in_recall_prefix():
    fake = _FakeReme()
    prefix = user_memory_prefix("u001")
    main = prefix + "auto-memory.md"
    history = "history/" + prefix.removeprefix("digest/") + "auto-memory-history.md"
    fake.files[main] = "# 自动记忆\n\n" + "".join(
        f"- 2026-11-01 旧事实{i}\n" for i in range(reme_memory.MAX_STORED_FACTS)
    )
    fake.bump(main)
    transport, _ = _transport(fake.handler)
    model = _FixedModel(json.dumps({"facts": ["用户喜欢靠窗座位"]}))

    added = asyncio.run(reme_memory.remember_turn(
        "u001", "问题", "答复", base_url="http://reme.test", model=model, transport=transport,
    ))

    assert added == 1
    assert [s["path"] for s in fake.saves] == [history, main]  # 先落历史，后写主文件
    assert "- 2026-11-01 旧事实0" in fake.files[history]
    assert "- 2026-11-01 旧事实0\n" not in fake.files[main]
    assert "- 2026-11-01 旧事实1\n" in fake.files[main]
    assert history.startswith("history/") and not history.startswith("digest/")


def test_history_append_is_idempotent_across_retries():
    fake = _FakeReme()
    history = "history/users/x/auto-memory-history.md"
    fake.files[history] = "# 自动记忆历史\n\n- 2026-11-01 旧事实0\n"
    fake.bump(history)
    transport, _ = _transport(fake.handler)
    client = httpx.Client(base_url="http://reme.test", transport=transport)

    reme_memory._append_history(client, history, ["- 2026-11-01 旧事实0", "- 2026-11-01 旧事实1"])

    lines = [line for line in fake.files[history].splitlines() if line.startswith("- ")]
    assert lines == ["- 2026-11-01 旧事实0", "- 2026-11-01 旧事实1"]
