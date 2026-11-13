"""093 编排与调度：写入型工具不进入并行调度；一次聊天请求只由协调入口派发一次。

- 并行分批由 AgentScope 按 FunctionTool.is_concurrency_safe 决定（agentscope/agent/_agent.py:2114）。
  本测试静态解析 src/gogo_agent 中全部 FunctionTool(...) 构造：非只读工具必须显式 is_concurrency_safe=False。
- 派发唯一性：用内存仓库与离线模型运行 ChatAgentExecutor.execute_turn，统计协调入口的构造次数，
  并记录构造过的模型角色；普通问答不应构造任何子 Agent 的模型。
- 不连接数据库或 Redis：GOGO_PREFERENCE_STORE=memory，且 Redis 客户端工厂被替换为断言失败的函数。
"""

from __future__ import annotations

import ast
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from gogo_agent.chat.executor import ChatAgentExecutor, _FallbackMockModel
from gogo_agent.chat.repository import InMemoryAgentSessionStore, InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.context import preferences as preferences_module
from gogo_agent.intent import (
    ConfidenceLevel, IntentCandidate, IntentCategory, IntentItem, IntentResult,
    PreparedIntentTurn, RecognitionDecision, RecognitionLayer, RewriteContextBuilder,
)

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "gogo_agent"
_UNKNOWN = object()


def _literal(node: ast.expr | None, default: object) -> object:
    if node is None:
        return default
    return node.value if isinstance(node, ast.Constant) else _UNKNOWN


def _callee(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None


def function_tool_constructions() -> list[tuple[str, int, object, object]]:
    """列出 src 中每个 FunctionTool(...) 的 (文件, 行号, is_read_only, is_concurrency_safe)；缺省值与 agentscope 一致。"""
    rows = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and _callee(node.func) == "FunctionTool":
                kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
                rows.append((
                    path.relative_to(REPO).as_posix(), node.lineno,
                    _literal(kwargs.get("is_read_only"), False),
                    _literal(kwargs.get("is_concurrency_safe"), True),
                ))
    return rows


def test_093_every_non_read_only_tool_is_explicitly_non_concurrent():
    rows = function_tool_constructions()
    assert len(rows) >= 8 and any(path.endswith("chat/executor.py") for path, *_ in rows)  # 前置：解析到了工具构造
    assert sum(read_only is not True for _, _, read_only, _ in rows) >= 6  # 前置：存在写入型或无法静态判定只读的工具
    unsafe = [f"{path}:{line}" for path, line, read_only, concurrent in rows
              if read_only is not True and concurrent is not False]
    assert unsafe == []


@asynccontextmanager
async def offline_pipeline(history_service):
    """093 只验调度，用固定意图结果隔离 017 外部模型。"""

    class OfflinePipeline:
        async def prepare(self, request):
            query = RewriteContextBuilder(history_service).build_rewrite_context(
                request.session_id, request.user_id, current_message_id=request.request_id,
            ).query
            result = IntentResult(
                intents=[IntentItem(
                    intent=IntentCategory.GENERAL_INFO, confidence=ConfidenceLevel.MEDIUM,
                    reason="093 固定预处理", evidence=[query.question],
                )],
                primary_intent=IntentCategory.GENERAL_INFO, multi_intent=False, overall_reason="093 固定预处理",
            )
            decision = RecognitionDecision(
                result=result, hit_layer=RecognitionLayer.RULE, confidence=ConfidenceLevel.MEDIUM,
                vector_threshold=None,
                candidates=[IntentCandidate(intent=IntentCategory.GENERAL_INFO, layer=RecognitionLayer.RULE,
                                            score=None, reason="093 固定候选")],
                attempted_layers=[RecognitionLayer.RULE], reason="093 固定快速命中",
            )
            return PreparedIntentTurn(
                branch="fast", original_question=query, effective_question=query,
                fast_decision=decision, rewrite=None, decision=decision,
            )

    yield OfflinePipeline()


def _booking_service_must_not_be_used():
    raise AssertionError("普通聊天请求不应访问预订服务")


def _redis_must_not_be_used():
    raise AssertionError("093 不应连接 Redis")


@pytest.mark.asyncio
async def test_093_plain_chat_request_is_dispatched_once_to_coordinator(monkeypatch):
    monkeypatch.setenv("GOGO_PREFERENCE_STORE", "memory")
    monkeypatch.setenv("GOGO_REME_BASE_URL", "")
    monkeypatch.setattr(preferences_module, "_redis_client_from_env", _redis_must_not_be_used)
    executor = ChatAgentExecutor(
        chat_history_service=ChatHistoryService(repository=InMemoryChatHistoryRepository()),
        agent_session_store=InMemoryAgentSessionStore(),
        pipeline_factory=offline_pipeline,
        booking_service_factory=_booking_service_must_not_be_used,
    )
    roles: list[str] = []

    def build_model(stream=False, role="master"):
        roles.append(role)
        return _FallbackMockModel(stream=stream)

    monkeypatch.setattr(executor, "_build_model", build_model)
    dispatched: list[str] = []
    build_coordinator = executor._build_agent_for_turn

    def counting_build(**kwargs):
        dispatched.append(kwargs["request"].request_id)
        return build_coordinator(**kwargs)

    monkeypatch.setattr(executor, "_build_agent_for_turn", counting_build)

    reply, _ = await executor.execute_turn("s-093", "alice", "你好")
    assert reply and len(dispatched) == 1  # 一次请求只构造一次协调入口
    assert roles == ["master"]  # 普通问答只构造协调入口的模型，没有子 Agent 模型

    await executor.execute_turn("s-093", "alice", "谢谢")
    assert len(dispatched) == 2 and len(set(dispatched)) == 2  # 第二次请求同样只派发一次，请求标识不重复
    assert roles == ["master", "master"]


@pytest.mark.asyncio
async def test_093_stream_chat_request_is_dispatched_once_to_coordinator(monkeypatch):
    """SSE 入口（router.py:85）与 execute_turn 共用协调入口：一次流式请求只构造一次协调 Agent。"""
    monkeypatch.setenv("GOGO_PREFERENCE_STORE", "memory")
    monkeypatch.setenv("GOGO_REME_BASE_URL", "")
    monkeypatch.setattr(preferences_module, "_redis_client_from_env", _redis_must_not_be_used)
    executor = ChatAgentExecutor(
        chat_history_service=ChatHistoryService(repository=InMemoryChatHistoryRepository()),
        agent_session_store=InMemoryAgentSessionStore(),
        pipeline_factory=offline_pipeline,
        booking_service_factory=_booking_service_must_not_be_used,
    )
    roles: list[str] = []

    def build_model(stream=False, role="master"):
        roles.append(role)
        return _FallbackMockModel(stream=stream)

    monkeypatch.setattr(executor, "_build_model", build_model)
    dispatched: list[str] = []
    build_coordinator = executor._build_agent_for_turn

    def counting_build(**kwargs):
        dispatched.append(kwargs["request"].request_id)
        return build_coordinator(**kwargs)

    monkeypatch.setattr(executor, "_build_agent_for_turn", counting_build)

    events = [event async for event in await executor.stream_turn_sse("s-093-stream", "alice", "你好")]
    assert any(event.startswith("event: message_id") for event in events)  # 前置：流式回合正常结束
    assert len(dispatched) == 1  # 一次流式请求只构造一次协调入口
    assert {"info", "manage", "plan", "booking"}.isdisjoint(roles)  # 普通问答不构造子 Agent 模型
