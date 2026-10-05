"""026：模型分工、离线替换与单次调用的安全观测。"""

import logging

import pytest
from agentscope.credential import OpenAICredential
from agentscope.message import TextBlock, UserMsg
from agentscope.model import ChatResponse, ChatUsage
from fastapi import HTTPException

from gogo_agent.chat.config import require_model_configuration
from gogo_agent.chat.executor import ChatAgentExecutor
from gogo_agent.chat.repository import InMemoryAgentSessionStore, InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.intent import runtime as intent_runtime
from gogo_agent.intent.service import create_text_model


@pytest.mark.asyncio
async def test_role_names_change_without_changing_trusted_identity_or_stores(monkeypatch):
    monkeypatch.setenv("GOGO_MODEL_API_KEY", "secret-026-not-in-logs")
    monkeypatch.setenv("GOGO_MODEL_BASE_URL", "https://gateway.example.test/v1")
    monkeypatch.setenv("GOGO_MODEL_NAME", "main-026")
    monkeypatch.setenv("GOGO_STABLE_MODEL_NAME", "stable-026")
    history = ChatHistoryService(InMemoryChatHistoryRepository())
    sessions = InMemoryAgentSessionStore()
    executor = ChatAgentExecutor(history, sessions)
    first = executor._save_user_turn("session-026", "u001", "请介绍一处景点")
    key = executor._state_session_id(first.session_id, first.user_id)
    master = executor._build_model()
    info = executor._build_model(role="info")
    settings = require_model_configuration()
    intent = create_text_model(
        OpenAICredential(api_key=settings.api_key, base_url=settings.base_url),
        settings.stable_model_name,
    )
    try:
        assert (master.model, master.role) == ("main-026", "master")
        assert (info.model, info.role) == ("stable-026", "info")
        assert (intent.model, intent.role) == ("stable-026", "intent")
        assert master.max_retries == info.max_retries == intent.max_retries == 0
        assert master.client.max_retries == info.client.max_retries == intent.client.max_retries == 0
        monkeypatch.setenv("GOGO_MODEL_NAME", "main-026-switched")
        switched = executor._build_model()
        try:
            assert switched.model == "main-026-switched"
            assert executor.history_service is history
            assert executor.session_store is sessions
            assert executor._state_session_id(first.session_id, first.user_id) == key
            assert history.list_messages("session-026", "u001")[0].content == "请介绍一处景点"
            with pytest.raises(HTTPException) as error:
                history.list_messages("session-026", "u002")
            assert error.value.status_code == 403
        finally:
            await switched.client.close()
    finally:
        await master.client.close()
        await info.client.close()
        await intent.client.close()


def test_explicit_empty_stable_model_fails_at_startup_configuration(monkeypatch):
    monkeypatch.setenv("GOGO_MODEL_API_KEY", "local-test-key")
    monkeypatch.setenv("GOGO_MODEL_BASE_URL", "https://gateway.example.test/v1")
    monkeypatch.setenv("GOGO_MODEL_NAME", "main-026")
    monkeypatch.setenv("GOGO_STABLE_MODEL_NAME", "")
    with pytest.raises(ValueError, match="GOGO_STABLE_MODEL_NAME"):
        require_model_configuration()


@pytest.mark.asyncio
async def test_request_pipeline_uses_stable_model_without_network(monkeypatch):
    monkeypatch.setenv("GOGO_MODEL_API_KEY", "local-test-key")
    monkeypatch.setenv("GOGO_MODEL_BASE_URL", "https://gateway.example.test/v1")
    monkeypatch.setenv("GOGO_MODEL_NAME", "main-026")
    monkeypatch.setenv("GOGO_STABLE_MODEL_NAME", "stable-026")

    class OfflineStore:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(intent_runtime, "QdrantStore", OfflineStore)
    monkeypatch.setattr(intent_runtime, "IntentVectorIndex", lambda *args: object())
    monkeypatch.setattr(intent_runtime, "IntentVectorMatcher", lambda index: object())
    history = ChatHistoryService(InMemoryChatHistoryRepository())
    async with intent_runtime.open_intent_pipeline(history) as pipeline:
        rewrite_model = pipeline._rewriter._model
        recognize_model = pipeline._recognizer._model
        assert rewrite_model is recognize_model
        assert rewrite_model.model == "stable-026"
        assert rewrite_model.role == "intent"


@pytest.mark.asyncio
async def test_model_call_records_available_tokens_without_key_or_message(monkeypatch, caplog):
    credential = OpenAICredential(api_key="secret-026-not-in-logs", base_url="https://gateway.example.test/v1")
    model = create_text_model(credential, "stable-026")

    async def fixed_call(model_name, messages, tools=None, tool_choice=None, **kwargs):
        assert model_name == "stable-026"
        return ChatResponse(
            content=[TextBlock(text="fixed-answer-026")],
            is_last=True,
            usage=ChatUsage(input_tokens=17, output_tokens=4, time=0.01),
        )

    monkeypatch.setattr(model, "_call_api", fixed_call)
    try:
        with caplog.at_level(logging.INFO, logger="uvicorn.error"):
            response = await model([UserMsg(name="user", content="private-message-026")])
        assert response.content[0].text == "fixed-answer-026"
        record = caplog.records[-1].getMessage()
        assert "role=intent" in record and "model='stable-026'" in record
        assert "duration_ms=" in record and "input_tokens=17" in record and "output_tokens=4" in record
        assert "secret-026-not-in-logs" not in record
        assert "private-message-026" not in record
        assert "fixed-answer-026" not in record
    finally:
        await model.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("usage, expected", (
    (None, "input_tokens=unavailable output_tokens=unavailable"),
    (ChatUsage(input_tokens=9, output_tokens=3, time=0.01), "input_tokens=9 output_tokens=3"),
))
async def test_stream_records_usage_when_available(monkeypatch, caplog, usage, expected):
    from gogo_agent.model import create_chat_model

    model = create_chat_model(
        OpenAICredential(api_key="local-test-key", base_url="https://gateway.example.test/v1"),
        "main-026", role="master", stream=True, timeout_seconds=3,
    )

    async def fixed_stream(model_name, messages, tools=None, tool_choice=None, **kwargs):
        async def chunks():
            yield ChatResponse(content=[TextBlock(text="离线响应")], is_last=False)
            if usage is not None:
                yield ChatResponse(content=[], is_last=False, usage=usage)

        return chunks()

    monkeypatch.setattr(model, "_call_api", fixed_stream)
    try:
        with caplog.at_level(logging.INFO, logger="uvicorn.error"):
            stream = await model([UserMsg(name="user", content="你好")])
            chunks = [chunk async for chunk in stream]
        assert chunks[-1].is_last
        record = caplog.records[-1].getMessage()
        assert "role=master" in record and "model='main-026'" in record
        assert expected in record
    finally:
        await model.client.close()
