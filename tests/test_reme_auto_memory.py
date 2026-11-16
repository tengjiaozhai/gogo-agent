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


def test_write_keeps_existing_lines_and_appends_only_new_facts():
    """动作协议下，等价行为由 ADD 动作承担：已存在的条目不再重复写入。"""
    from gogo_agent.context import memory_actions as ma

    entries = ma.parse_entries("# 自动记忆\n\n- 2026-11-01 用户偏好乘坐国航\n")
    change = ma.apply_actions(
        entries,
        [ma.Action(ma.ADD, None, "用户偏好乘坐国航"), ma.Action(ma.ADD, None, "用户喜欢靠窗座位")],
        utterance="", today="2026-11-13",
    )

    assert change.applied == 1  # 重复的那条视为无变化
    assert "用户偏好乘坐国航" in change.body
    assert "2026-11-13 用户喜欢靠窗座位" in change.body


def test_actions_evict_oldest_entries_beyond_limit():
    from gogo_agent.context import memory_actions as ma

    entries = ma.parse_entries("# 自动记忆\n\n" + "".join(f"- 2026-11-01 事实{i}\n" for i in range(5)))
    change = ma.apply_actions(
        entries, [ma.Action(ma.ADD, None, "新事实")], utterance="", today="2026-11-13", limit=3,
    )

    assert "事实0" not in change.body and "事实1" not in change.body and "事实2" not in change.body
    assert "事实3" in change.body and "事实4" in change.body and "新事实" in change.body
    assert [line.split(" ")[2] for line in change.evicted] == ["事实0", "事实1", "事实2"]
    assert all("reason:evicted" in line for line in change.evicted)


def test_normalize_facts_counts_overflow_without_stopping_scan(caplog):
    raw = [f"用户偏好第{i}项" for i in range(7)]

    with caplog.at_level("WARNING"):
        selected = reme_memory.normalize_facts(raw, limit=5)

    assert selected == [f"用户偏好第{i}项" for i in range(5)]
    assert "dropped=2" in caplog.text


def test_remember_turn_writes_only_into_callers_own_prefix():
    model = _FixedModel(json.dumps({"actions": [
        {"action": "ADD", "content": "用户偏好乘坐国航"},
        {"action": "ADD", "content": "用户喜欢靠窗座位"},
    ]}))
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
    model = _FixedModel(json.dumps({"actions": [{"action": "ADD", "content": "用户偏好乘坐国航"}]}))

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


def test_remember_turn_reads_state_and_writes_nothing_when_model_returns_no_actions():
    """动作协议下必须先读现有条目才能判定动作；空动作时不写文件。"""
    model = _FixedModel(json.dumps({"actions": []}))
    fake = _FakeReme()

    assert asyncio.run(reme_memory.remember_turn(
        "u001", "问题", "答复", base_url="http://reme.test", model=model,
        transport=_transport(fake.handler)[0],
    )) == 0
    assert fake.saves == []  # 只读不写
    assert fake.stats == 1


def test_remember_turn_rejects_blank_user_before_any_call():
    model = _FixedModel(json.dumps({"actions": [{"action": "ADD", "content": "用户偏好乘坐国航"}]}))

    with pytest.raises(ValueError):
        asyncio.run(reme_memory.remember_turn(
            "  ", "问题", "答复", base_url="http://reme.test", model=model,
        ))
    assert model.calls == []


def test_executor_schedules_memory_write_as_background_task(monkeypatch):
    from gogo_agent.chat import executor as executor_module
    from gogo_agent.request_context import RequestContext

    calls: list[tuple[str, str, str, str, str | None]] = []

    async def fake_remember(user_id, question, answer, *, base_url, model, source=None,
                            explicit_preferences=None):
        calls.append((user_id, question, answer, base_url, source))

    monkeypatch.setattr(executor_module, "remember_turn", fake_remember)
    # 偏好读取不得连真实 Redis：本用例只验证后台调度与参数传递。
    monkeypatch.setattr(executor_module, "preferences_for_model",
                        lambda _store, _user: [{"key": "seat", "value": "aisle"}])
    monkeypatch.setattr(executor_module, "get_preference_store", lambda: object())
    monkeypatch.setenv("GOGO_REME_BASE_URL", "http://reme.test")
    executor = object.__new__(executor_module.ChatAgentExecutor)
    executor._memory_tasks = set()
    executor._build_model = lambda stream=False, *, role="master": object()
    request = RequestContext(user_id="u001", session_id="s-1", request_id="r-1")

    async def scenario() -> None:
        executor._schedule_memory_write(request, "问题", "答复")
        assert executor._memory_tasks
        await asyncio.gather(*executor._memory_tasks)

    asyncio.run(scenario())

    # 后台写入必须带上本轮 request_id 作为来源，供历史行追溯。
    assert calls == [("u001", "问题", "答复", "http://reme.test", "r-1")]


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
    model = _FixedModel(json.dumps({"actions": [{"action": "ADD", "content": "用户喜欢靠窗座位"}]}))

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
    model = _FixedModel(json.dumps({"actions": [{"action": "ADD", "content": "用户喜欢靠窗座位"}]}))

    added = asyncio.run(reme_memory.remember_turn(
        "u001", "问题", "答复", base_url="http://reme.test", model=model, transport=transport,
    ))

    assert added == 1
    assert [s["path"] for s in fake.saves] == [history, main]  # 先落历史，后写主文件
    assert "- 2026-11-01 旧事实0" in fake.files[history]
    assert "reason:evicted" in fake.files[history]
    assert "2026-11-01 旧事实0 " not in fake.files[main]
    assert "2026-11-01 旧事实1 " in fake.files[main]
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
