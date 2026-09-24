"""001 startup, credential handling, and the AgentScope tool loop."""

import asyncio
import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from gogo_agent.cli import ask, build_agent
from gogo_agent.api import app
from gogo_agent.tools import get_current_date


def test_get_current_date_is_local_iso_date():
    before = date.today()
    result = get_current_date()
    after = date.today()
    assert date.fromisoformat(result) in (before, after)


def test_missing_model_settings_are_reported_before_network(monkeypatch):
    for name in ("GOGO_MODEL_API_KEY", "GOGO_MODEL_NAME", "GOGO_MODEL_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(
        ValueError,
        match="GOGO_MODEL_API_KEY, GOGO_MODEL_NAME, GOGO_MODEL_BASE_URL",
    ):
        build_agent()


def test_base_url_must_be_gateway_root(monkeypatch):
    monkeypatch.setenv("GOGO_MODEL_API_KEY", "local-test-key")
    monkeypatch.setenv("GOGO_MODEL_NAME", "test-model")
    monkeypatch.setenv("GOGO_MODEL_BASE_URL", "http://127.0.0.1:1/v1")
    with pytest.raises(ValueError, match="网关根地址"):
        build_agent()


def test_health_without_model_settings(monkeypatch):
    for name in ("GOGO_MODEL_API_KEY", "GOGO_MODEL_NAME", "GOGO_MODEL_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "agentscope_version": "2.0.8"}


def test_scalar_docs_endpoint():
    client = TestClient(app)
    scalar_resp = client.get("/scalar")
    assert scalar_resp.status_code == 200
    doc_resp = client.get("/doc.html", follow_redirects=False)
    assert doc_resp.status_code == 307
    assert doc_resp.headers["location"] == "/scalar"



@pytest.mark.asyncio
async def test_agent_calls_only_date_tool_then_answers_from_mock_chat_completions_api(monkeypatch):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            if len(requests) == 1:
                assert [tool["function"]["name"] for tool in body["tools"]] == ["get_current_date"]
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "tool-1",
                        "type": "function",
                        "function": {"name": "get_current_date", "arguments": "{}"},
                    }],
                }
                finish_reason = "tool_calls"
            else:
                assert '"role": "tool"' in json.dumps(body, ensure_ascii=False)
                assert get_current_date() in json.dumps(body, ensure_ascii=False)
                message = {"role": "assistant", "content": f"今天是 {get_current_date()}。"}
                finish_reason = "stop"
            response = json.dumps({
                "id": f"chatcmpl-{len(requests)}",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, _format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("GOGO_MODEL_API_KEY", "local-test-key")
        monkeypatch.setenv("GOGO_MODEL_NAME", "test-model")
        monkeypatch.setenv("GOGO_MODEL_BASE_URL", f"http://127.0.0.1:{server.server_port}")
        answer, tool_used = await asyncio.wait_for(ask("今天是几月几日？"), timeout=20)
        assert tool_used
        assert get_current_date() in answer
        assert len(requests) == 2
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
