"""028 离线断点入口：伪造 userId 工具参数与 SQL 会话归属复现。

从仓库根目录运行：
    PYTHONPATH=src .venv/bin/python scripts/demo_028_trusted_identity.py --case all
可选 --case tool 或 --case sql；不需要真实模型、数据库、Redis 或外部账号。
逐步准备、IDE 参数、断点表和预测练习见 docs/契约样例/028-可信身份与会话归属.md。

Java ChatController 从 Sa-Token 取 userId，BaseSubAgent 把 AgentSessionContext
旁路注入 ToolExecutionContext；Python auth.dependencies 从 Token 取用户，
ChatAgentExecutor._save_user_turn() 创建不可变 RequestContext，info_agent 闭包
仅让模型填写 question。Java 的工具反射注入与 Python 的闭包捕获职责相似，
对象生命周期和异步传播不同；Python 的 await 等待本轮模型/工具执行完成。

tool 场景使用真实 FastAPI、Toolkit、请求归属和内存 Token；固定 L1/模型响应
伪造 Bob 的 userId、sessionId、planReference。sql 场景使用真实 SQLAlchemy
仓储和本地 SQLite，展示删除后同 ID 仍归原用户，Bob 不能复用写入。
本演示没有计划/差旅业务表，因此不能证明未来计划工具的归属检查已实现。
"""

import argparse
from contextlib import asynccontextmanager

from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from demo_023_master_agent import FixedRule, InfoModel, UnusedRewriter
from gogo_agent.api import app
from gogo_agent.auth.dependencies import get_auth_service
from gogo_agent.auth.repository import InMemoryUserAccountRepository
from gogo_agent.auth.security import InMemoryTokenStore, TokenManager
from gogo_agent.auth.service import AuthService
from gogo_agent.chat.dependencies import get_chat_executor, get_chat_history_service
from gogo_agent.chat.executor import ChatAgentExecutor, _FallbackMockModel
from gogo_agent.chat.repository import InMemoryAgentSessionStore, InMemoryChatHistoryRepository, SQLChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.db.base import Base
from gogo_agent.intent import IntentPipelineService, IntentRecognizer, RewriteContextBuilder


def sql_case() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    history = ChatHistoryService(SQLChatHistoryRepository(sessionmaker(bind=engine)))
    try:
        history.save_user_message("demo-028-reuse", "u001", "Alice 的私有问题")
        history.delete_conversation("demo-028-reuse", "u001")
        try:
            history.save_user_message("demo-028-reuse", "u002", "Bob 的越权问题")
        except Exception as exc:
            print("  Bob 复用 Alice session：", getattr(exc, "status_code", type(exc).__name__))
        history.save_user_message("demo-028-reuse", "u001", "Alice 的新问题")
        print("  Alice 可见消息：", [msg.content for msg in history.list_messages("demo-028-reuse", "u001")])
        try:
            history.list_messages("demo-028-reuse", "u002")
        except Exception as exc:
            print("  Bob 读取 Alice 会话：", getattr(exc, "status_code", type(exc).__name__))
        else:
            raise AssertionError("Bob 不应读取 Alice 会话")
    finally:
        engine.dispose()


def tool_case() -> None:
    auth = AuthService(InMemoryUserAccountRepository(), TokenManager(store=InMemoryTokenStore()))
    history = ChatHistoryService(InMemoryChatHistoryRepository())
    store = InMemoryAgentSessionStore()

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

    executor = ChatAgentExecutor(history, store, pipeline_factory=pipeline_factory)
    tool_schemas = []
    captured = []
    original_build_info = executor._build_info_agent

    def build_info(request, state=None):
        captured.append(request)
        return original_build_info(request, state)

    executor._build_info_agent = build_info

    class ForgedMasterModel(_FallbackMockModel):
        """只替换供应商输出，让 AgentScope 处理带假身份的真实工具调用。"""

        async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
            self.call_count += 1
            tool_schemas.extend(tools or [])
            if self.call_count == 1:
                return ChatResponse(content=[ToolCallBlock(
                    id="demo-028-tool", name="info_agent",
                    input='{"question":"请介绍一处景点","userId":"u002","sessionId":"bob-028","planReference":"foreign-plan"}',
                )], is_last=True)
            return ChatResponse(content=[TextBlock(text="已处理本轮请求")], is_last=True)

    master = ForgedMasterModel()
    executor._build_model = lambda stream=False, role="master": master if role == "master" else InfoModel()
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_auth_service] = lambda: auth
    app.dependency_overrides[get_chat_executor] = lambda: executor
    app.dependency_overrides[get_chat_history_service] = lambda: history
    try:
        client = TestClient(app)
        alice = {"Authorization": auth.login("alice", "123456"), "Accept": "application/json"}
        bob = {"Authorization": auth.login("bob", "123456"), "Accept": "application/json"}
        response = client.post("/api/chat/alice-028", headers=alice, json={"message": "请介绍一处景点"})
        response.raise_for_status()
        properties = tool_schemas[0]["function"]["parameters"]["properties"]
        assert set(properties) == {"question"}
        assert all(item.user_id == "u001" and item.session_id == "alice-028" for item in captured)
        denied = client.get("/api/chat/alice-028/messages", headers=bob)
        assert denied.status_code == 403
        print("  模型可见工具参数：", sorted(properties))
        print("  子 Agent 收到的可信用户：", [item.user_id for item in captured] or "非法参数被工具校验拒绝")
        print("  Bob 读取 Alice 会话：HTTP", denied.status_code)
        print("  Bob 活跃记录：", store.load_active_agent(executor._state_session_id("alice-028", "u002")))
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def main() -> None:
    parser = argparse.ArgumentParser(description="028 工具身份和会话归属离线演示")
    parser.add_argument("--case", choices=("all", "tool", "sql"), default="all")
    case = parser.parse_args().case
    if case in ("all", "tool"):
        print("[模型伪造 userId/sessionId]")
        tool_case()
    if case in ("all", "sql"):
        print("\n[删除后复用 SQL 会话]")
        sql_case()


if __name__ == "__main__":
    main()
