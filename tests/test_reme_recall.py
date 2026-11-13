"""阶段 H 只读语义召回（context/reme.py）的隔离、降级与请求形状测试。

真实用例在 pytest 临时目录启动本机 `reme start`（系统分配端口、临时 workspace），
不连接 127.0.0.1:2333 或公网服务器。无法启动 reme 时，真实检索用例跳过并写明原因；
请求形状、输入校验与异常降级由 httpx.MockTransport 和本地静默套接字覆盖，不依赖 reme。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from agentscope.message import SystemMsg, UserMsg

from gogo_agent.context.input import TURN_MARKER, ModelInputContext
from gogo_agent.context.reme import semantic_preferences
from gogo_agent.request_context import RequestContext

USER_A = "travel-user-a"
USER_B = "travel-user-b"
USER_EMPTY = "travel-user-empty"
USER_NOTES = "travel-user-notes"
QUERY = "只坐哪家航空"
NOTES = ["住宿偏好：经济型连锁酒店即可。", "交通偏好：高铁优先于飞机。", "饮食偏好：不吃辣。"]


def _prefix(user_id: str) -> str:
    """测试侧独立派生目录前缀，不调用被测代码，避免自证。"""
    return "digest/users/" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16] + "/"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_memory(workspace: Path, user_id: str, name: str, text: str) -> None:
    folder = workspace / _prefix(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(f"# 出行偏好\n\n{text}\n", encoding="utf-8")


def _health_ok(base_url: str) -> bool:
    with httpx.Client(base_url=base_url, timeout=2.0, trust_env=False) as client:
        return client.post("/health_check", json={}).status_code == 200


def _search_paths(base_url: str, query: str, user_id: str, top_k: int) -> list[str]:
    body = {"query": query, "limit": top_k, "search_filter": {"prefix": _prefix(user_id)}}
    with httpx.Client(base_url=base_url, timeout=5.0, trust_env=False) as client:
        response = client.post("/search", json=body)
    response.raise_for_status()
    return [item["path"] for item in response.json()["metadata"]["results"]]


def _wait_until(check, *, timeout: float, process: subprocess.Popen) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            if check():
                return True
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        time.sleep(0.5)
    return False


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@pytest.fixture(scope="module")
def reme_base_url(tmp_path_factory):
    binary = Path(sys.executable).with_name("reme")
    if not binary.exists():
        pytest.skip("当前 Python 环境没有 reme 命令，真实检索用例跳过")
    root = tmp_path_factory.mktemp("reme")
    workspace = root / "workspace"
    workspace.mkdir()
    log_path = root / "server.log"
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [str(binary), "start", f"workspace_dir={workspace}", "service.host=127.0.0.1", f"service.port={port}"],
            cwd=root,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        if not _wait_until(lambda: _health_ok(base_url), timeout=90, process=process):
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-800:]
            pytest.skip(f"reme 未能启动，真实检索用例跳过。日志末尾：\n{tail}")
        _write_memory(workspace, USER_A, "prefs.md", "偏好：只坐国航，不喜欢红眼航班。")
        _write_memory(workspace, USER_B, "prefs.md", "偏好：只坐东航，喜欢靠窗座位。")
        for name, note in zip(("notes-1.md", "notes-2.md", "notes-3.md"), NOTES):
            _write_memory(workspace, USER_NOTES, name, note)
        indexed = _wait_until(
            lambda: all(
                [
                    _search_paths(base_url, QUERY, USER_A, 10),
                    _search_paths(base_url, QUERY, USER_B, 10),
                    _search_paths(base_url, "出行偏好", USER_NOTES, 10),
                ]
            ),
            timeout=120,
            process=process,
        )
        assert indexed, "reme 已启动，但写入的记忆在 120 秒内未建立索引"
        yield base_url
    finally:
        _stop(process)


def test_real_recall_returns_only_the_callers_own_memory(reme_base_url):
    own_a = semantic_preferences(USER_A, QUERY, base_url=reme_base_url)
    own_b = semantic_preferences(USER_B, QUERY, base_url=reme_base_url)

    assert own_a and own_b
    assert all("国航" in item["value"] and "东航" not in item["value"] for item in own_a)
    assert all("东航" in item["value"] and "国航" not in item["value"] for item in own_b)
    assert {item["source"] for item in own_a + own_b} == {"prefs.md"}
    assert all(item["key"] == "semantic_recall" for item in own_a + own_b)
    assert json.loads(json.dumps(own_a, ensure_ascii=False)) == own_a


def test_real_recall_for_user_without_memory_is_empty(reme_base_url):
    assert semantic_preferences(USER_EMPTY, QUERY, base_url=reme_base_url) == []


def test_real_recall_honours_top_k_inside_own_prefix(reme_base_url):
    result = semantic_preferences(USER_NOTES, "出行偏好", base_url=reme_base_url, top_k=2)

    assert len(result) == 2
    assert all(any(note in item["value"] for note in NOTES) for item in result)
    assert all(item["source"].startswith("notes-") for item in result)


def test_stopped_reme_returns_empty_without_raising():
    base_url = f"http://127.0.0.1:{_free_port()}"  # 无人监听，等同 ReMe 停服
    assert semantic_preferences(USER_A, QUERY, base_url=base_url, timeout=1.0) == []


def test_silent_reme_times_out_and_returns_empty():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(4)  # 内核接受连接，但没有任何应答，触发读超时
        base_url = f"http://127.0.0.1:{server.getsockname()[1]}"
        started = time.monotonic()
        result = semantic_preferences(USER_A, QUERY, base_url=base_url, timeout=0.5)
        elapsed = time.monotonic() - started
    assert result == []
    assert elapsed < 5


def _recorder(handler):
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(record), seen


def _reply(*results: dict):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "answer": "", "metadata": {"results": list(results)}})

    return handler


def test_every_request_carries_the_callers_prefix_and_foreign_hits_are_dropped():
    transport, seen = _recorder(
        _reply(
            {"path": _prefix(USER_A) + "prefs.md", "text": "偏好：只坐国航。"},
            {"path": _prefix(USER_B) + "prefs.md", "text": "偏好：只坐东航。"},
        )
    )

    result = semantic_preferences(USER_A, QUERY, base_url="http://reme.test", top_k=3, transport=transport)

    assert result == [{"key": "semantic_recall", "value": "偏好：只坐国航。", "source": "prefs.md"}]
    assert len(seen) == 1
    assert seen[0].url.path == "/search"
    assert json.loads(seen[0].content) == {"query": QUERY, "limit": 3, "search_filter": {"prefix": _prefix(USER_A)}}


@pytest.mark.parametrize(("user_id", "top_k"), [("", 5), (USER_A, 0)], ids=["empty-user", "zero-top-k"])
def test_invalid_input_is_rejected_before_any_request(user_id, top_k):
    transport, seen = _recorder(_reply())

    with pytest.raises(ValueError):
        semantic_preferences(user_id, QUERY, base_url="http://reme.test", top_k=top_k, transport=transport)
    assert seen == []


def test_blank_query_returns_empty_without_request():
    transport, seen = _recorder(_reply())

    assert semantic_preferences(USER_A, "   ", base_url="http://reme.test", transport=transport) == []
    assert seen == []


@pytest.mark.parametrize(
    "handler",
    [
        lambda _request: httpx.Response(500, text="internal error"),
        lambda _request: httpx.Response(200, json={"success": False, "answer": "Error: query cannot be empty"}),
        lambda _request: httpx.Response(200, text="<html>not json</html>"),
        lambda _request: httpx.Response(200, json={"success": True, "metadata": {}}),
        _reply({"path": "digest/users/0000000000000000/prefs.md", "text": "偏好：只坐东航。"}),
    ],
    ids=["http-500", "success-false", "non-json", "missing-results", "foreign-only"],
)
def test_failed_or_malformed_replies_degrade_to_empty(handler):
    transport, seen = _recorder(handler)

    assert semantic_preferences(USER_A, QUERY, base_url="http://reme.test", transport=transport) == []
    assert len(seen) == 1
    assert json.loads(seen[0].content)["search_filter"] == {"prefix": _prefix(USER_A)}


def _run_model_input(mw, messages):
    async def receive(**kwargs):
        return kwargs["messages"]

    return asyncio.run(mw.on_model_call(None, {"messages": messages}, receive))


def _turn(query: str | None):
    turn = [SystemMsg(name="system", content="角色" + TURN_MARKER + "路由")]
    if query is not None:
        turn.append(UserMsg(name="user", content=query))
    return turn


def _find(messages, name):
    return next((m for m in messages if m.name == name), None)


def test_recall_is_injected_as_its_own_message_off_the_event_loop():
    calls = []

    def recall(query):
        calls.append((query, threading.current_thread() is threading.main_thread()))
        return [{"key": "semantic_recall", "value": "偏好：只坐国航。", "source": "prefs.md"}]

    mw = ModelInputContext(RequestContext(user_id=USER_A, session_id="s-recall", request_id="r-recall"), recall=recall)
    out = _run_model_input(mw, _turn(QUERY))

    assert calls == [(QUERY, False)]
    recalled = _find(out, "recalled_context")
    assert recalled is not None and "偏好：只坐国航。" in recalled.get_text_content()
    server = _find(out, "server_context")
    assert server is not None and "semantic_recall" not in server.get_text_content()
    assert _find(out, "saved_preferences") is None


def test_recall_is_skipped_when_unconfigured_or_no_user_text():
    calls = []

    def recall(query):
        calls.append(query)
        return []

    off = ModelInputContext(RequestContext(user_id=USER_A, session_id="s-off", request_id="r-off"))
    assert _find(_run_model_input(off, _turn(QUERY)), "recalled_context") is None

    no_text = ModelInputContext(RequestContext(user_id=USER_A, session_id="s-txt", request_id="r-txt"), recall=recall)
    assert _find(_run_model_input(no_text, _turn(None)), "recalled_context") is None
    assert calls == []

    empty = ModelInputContext(RequestContext(user_id=USER_A, session_id="s-empty", request_id="r-empty"), recall=recall)
    assert _find(_run_model_input(empty, _turn(QUERY)), "recalled_context") is None
    assert calls == [QUERY]
