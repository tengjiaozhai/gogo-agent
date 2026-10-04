"""023 离线断点：HTTP → 意图流水线 → GoGo Toolkit → 只读 InfoAgent。"""

import argparse
import json
from contextlib import asynccontextmanager

from agentscope.message import TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatResponse
from fastapi.testclient import TestClient

from gogo_agent.api import app
from gogo_agent.auth.dependencies import get_auth_service
from gogo_agent.auth.repository import InMemoryUserAccountRepository
from gogo_agent.auth.security import InMemoryTokenStore, TokenManager
from gogo_agent.auth.service import AuthService
from gogo_agent.chat.dependencies import get_chat_executor, get_chat_history_service
from gogo_agent.chat.executor import ChatAgentExecutor, _FallbackMockModel
from gogo_agent.chat.repository import InMemoryAgentSessionStore, InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.intent import (
    ConfidenceLevel, FastMatch, IntentCandidate, IntentCategory, IntentItem,
    IntentPipelineService, IntentRecognizer, IntentResult, MatchStatus,
    RecognitionLayer, RewriteContextBuilder,
)


class FixedRule:
    """离线固定通用信息意图；真实流水线仍读取经归属校验的业务历史。"""

    def match(self, query):
        result = IntentResult(
            intents=[IntentItem(
                intent=IntentCategory.GENERAL_INFO, confidence=ConfidenceLevel.HIGH,
                reason="023 离线分类", evidence=[query.question],
            )],
            primary_intent=IntentCategory.GENERAL_INFO,
            multi_intent=False,
            overall_reason="023 只读信息查询演示",
        )
        return FastMatch(
            status=MatchStatus.HIT, result=result, threshold=None,
            candidates=[IntentCandidate(
                intent=IntentCategory.GENERAL_INFO, layer=RecognitionLayer.RULE,
                score=None, reason="离线 L1 命中",
            )],
            reason="离线 L1 命中",
        )


class UnusedRewriter:
    """L1 命中时改写器不应运行。"""

    async def rewrite(self, context):
        raise AssertionError("023 演示不应调用改写器")


class MasterModel(_FallbackMockModel):
    """先请求工具，再根据真正的工具结果返回文本。"""

    def __init__(self, requested_tool: str):
        super().__init__(stream=False)
        self.requested_tool = requested_tool

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.call_count += 1
        names = [item["function"]["name"] for item in tools or []]
        print(f"  主 Agent 第 {self.call_count} 次模型调用，可见工具：{names}")
        if self.call_count == 1:
            return ChatResponse(content=[ToolCallBlock(
                id="demo-023-tool", name=self.requested_tool,
                input=json.dumps({"question": "请介绍一处景点"}, ensure_ascii=False),
            )], is_last=True)
        results = [
            block for msg in messages for block in msg.content
            if isinstance(block, ToolResultBlock)
        ]
        answer = "信息子 Agent 的离线固定回答" if any(
            "信息子 Agent 的离线固定回答" in str(result.output) for result in results
        ) else "该工具没有执行；主 Agent 未取得信息子 Agent 的结果"
        return ChatResponse(content=[TextBlock(text=f"GoGo 汇总：{answer}")], is_last=True)


class InfoModel(_FallbackMockModel):
    """InfoAgent 的固定模型；没有任何业务工具。"""

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        assert not tools
        print("  InfoAgent 收到问题，工具 schema 为空")
        return ChatResponse(content=[TextBlock(text="信息子 Agent 的离线固定回答")], is_last=True)


def run_case(requested_tool: str) -> None:
    """使用内存认证和状态走实际 FastAPI JSON 对话入口。"""
    auth = AuthService(
        repository=InMemoryUserAccountRepository(load_default_seeds=True),
        token_manager=TokenManager(store=InMemoryTokenStore()),
    )
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())

    @asynccontextmanager
    async def pipeline_factory(history_service):
        model = _FallbackMockModel()
        try:
            yield IntentPipelineService(
                RewriteContextBuilder(history_service), UnusedRewriter(),
                IntentRecognizer(model, rule_matcher=FixedRule()),
            )
        finally:
            await model.client.close()

    executor = ChatAgentExecutor(
        history, InMemoryAgentSessionStore(), pipeline_factory=pipeline_factory,
    )
    master_model = MasterModel(requested_tool)
    info_model = InfoModel()
    created = 0

    def build_model(stream=False):
        nonlocal created
        created += 1
        return master_model if created == 1 else info_model

    executor._build_model = build_model
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_auth_service] = lambda: auth
    app.dependency_overrides[get_chat_executor] = lambda: executor
    app.dependency_overrides[get_chat_history_service] = lambda: history
    session_id = f"demo-023-{requested_tool}"
    try:
        response = TestClient(app).post(
            f"/api/chat/{session_id}",
            headers={"Authorization": auth.login("alice", "123456"),
                     "Accept": "application/json"},
            json={"message": "请介绍一处景点"},
        )
        response.raise_for_status()
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)

    messages = history.list_messages(session_id, "u001")
    tool_calls = messages[-1].extra["tool_calls"]
    print("  HTTP 回复：", response.json()["content"])
    print("  助手消息工具记录：", tool_calls)
    assert messages[-1].extra["intent_pipeline"]["dispatch_target"] == "GoGo"
    if requested_tool == "info_agent":
        assert len(tool_calls) == 1 and tool_calls[0]["status"] == "completed"
        assert created == 2
    else:
        assert tool_calls == [] and created == 1


def main() -> None:
    parser = argparse.ArgumentParser(description="023 主 Agent 与只读子 Agent 离线断点演示")
    parser.add_argument("--case", choices=("all", "success", "unregistered"), default="all")
    case = parser.parse_args().case
    if case in ("all", "success"):
        print("[已注册的 info_agent]")
        run_case("info_agent")
    if case in ("all", "unregistered"):
        print("\n[模型尝试未注册的 write_order]")
        run_case("write_order")
    print("\n本演示使用真实 HTTP、意图流水线、AgentScope Agent/Toolkit；模型、信息内容和存储是本地替身。")


if __name__ == "__main__":
    main()
