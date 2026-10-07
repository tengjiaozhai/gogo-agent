"""027 离线断点入口：登录 → GoGo 委派 Info → 记录活跃状态 → 精确续聊。

从仓库根目录运行（不用数据库、Qdrant 或模型密钥）：
    PYTHONPATH=src .venv/bin/python scripts/demo_027_active_info.py --case all
场景还可单独选 continue、sse、switch、expired、cross_user。
逐步准备、IDE 参数、断点表和预测练习见 docs/契约样例/027-活跃InfoAgent续跑.md。

Java ChatController.chat() 读 ActiveAgentSessionStore，再用 ContinuationSignals.ALL
整句匹配并交给 ChatAgentExecutor；Java 当前 InfoAgent 没挂活跃记录 Hook。
Python ChatAgentExecutor.execute_turn()/stream_turn_sse() 在已鉴权消息保存后读取
InfoAgentContinuation；命中时不进入 IntentPipelineService.prepare()。
这里的 asynccontextmanager 像 Java 对请求资源设定作用域，yield 前装配意图流水线，
退出时关闭固定模型；asyncio 的 await 是等待异步结果，不等同 Reactor Mono 的订阅时机。

真实代码：FastAPI 登录与会话归属、执行器、AgentScope GoGo/InfoAgent、AgentState
和活跃记录。替身：L1 固定分类、GoGo/Info 模型响应、内存 Token/仓储；无真实网关。
续跑的 SSE 目前在子 Agent 完成后发一整条 message，再发 message_id，不逐 token。
"""

import argparse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from demo_023_master_agent import FixedRule, InfoModel, MasterModel, UnusedRewriter
from gogo_agent.api import app
from gogo_agent.auth.dependencies import get_auth_service
from gogo_agent.auth.repository import InMemoryUserAccountRepository
from gogo_agent.auth.security import InMemoryTokenStore, TokenManager
from gogo_agent.auth.service import AuthService
from gogo_agent.chat.dependencies import get_chat_executor, get_chat_history_service
from gogo_agent.chat.executor import ChatAgentExecutor, _FallbackMockModel
from gogo_agent.chat.repository import ActiveAgentRecord, InMemoryAgentSessionStore, InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.intent import IntentPipelineService, IntentRecognizer, RewriteContextBuilder


def run_case(case: str) -> None:
    auth = AuthService(InMemoryUserAccountRepository(), TokenManager(store=InMemoryTokenStore()))
    history = ChatHistoryService(InMemoryChatHistoryRepository())
    store = InMemoryAgentSessionStore()
    pipeline_calls = []
    model_roles = []

    @asynccontextmanager
    async def pipeline_factory(history_service):
        pipeline_calls.append("intent_pipeline")
        model = _FallbackMockModel()
        try:
            yield IntentPipelineService(
                RewriteContextBuilder(history_service), UnusedRewriter(),
                IntentRecognizer(model, rule_matcher=FixedRule()),
            )
        finally:
            await model.client.close()

    executor = ChatAgentExecutor(history, store, pipeline_factory=pipeline_factory)

    def build_model(stream=False, *, role="master"):
        model_roles.append(role)
        return MasterModel("info_agent") if role == "master" else InfoModel()

    executor._build_model = build_model
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_auth_service] = lambda: auth
    app.dependency_overrides[get_chat_executor] = lambda: executor
    app.dependency_overrides[get_chat_history_service] = lambda: history
    session_id = f"demo-027-{case}"
    alice = {"Authorization": auth.login("alice", "123456"), "Accept": "application/json"}
    try:
        client = TestClient(app)
        first = client.post(f"/api/chat/{session_id}", headers=alice, json={"message": "请介绍一处景点"})
        first.raise_for_status()
        key = executor._state_session_id(session_id, "u001")
        active = store.load_active_agent(key)
        assert active and active.agent_name == "InfoAgent"
        print(f"  首轮：pipeline={len(pipeline_calls)}，models={model_roles}，active={active.agent_name}")

        if case == "expired":
            store.save_active_agent(key, ActiveAgentRecord(
                agent_name="InfoAgent", expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            ))
        if case == "cross_user":
            bob = {"Authorization": auth.login("bob", "123456"), "Accept": "application/json"}
            response = client.post(f"/api/chat/{session_id}", headers=bob, json={"message": "继续"})
            assert response.status_code == 403
            print(f"  Bob 复用 Alice session：HTTP {response.status_code}；Bob 活跃记录：{store.load_active_agent(executor._state_session_id(session_id, 'u002'))}")
            return

        message = "另外一件事" if case == "switch" else "继续"
        headers = {**alice, "Accept": "text/event-stream"} if case == "sse" else alice
        response = client.post(f"/api/chat/{session_id}", headers=headers, json={"message": message})
        response.raise_for_status()
        last = history.list_messages(session_id, "u001")[-1]
        if case == "sse":
            print("  续跑 SSE 事件：", [line for line in response.text.splitlines() if line.startswith("event:")])
        print(f"  第二轮：pipeline={len(pipeline_calls)}，models={model_roles}，reply_agent={last.agentName}")
        if case in ("continue", "sse"):
            assert len(pipeline_calls) == 1 and model_roles.count("master") == 1
            assert last.extra["turn_entry"] == "continue_active"
        else:
            assert len(pipeline_calls) == 2 and model_roles.count("master") == 2
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def main() -> None:
    parser = argparse.ArgumentParser(description="027 活跃 InfoAgent 续聊离线演示")
    parser.add_argument("--case", choices=("all", "continue", "sse", "switch", "expired", "cross_user"), default="all")
    case = parser.parse_args().case
    for selected in (("continue", "sse", "switch", "expired", "cross_user") if case == "all" else (case,)):
        print(f"\n[{selected}]")
        run_case(selected)


if __name__ == "__main__":
    main()
