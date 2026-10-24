"""确认提交离线调试：真实规则 → GoGo → Manage → 隔离 SQLite，两轮确认与幂等。

准备：仓库根目录执行 uv sync --frozen，解释器选 .venv/bin/python，PYTHONPATH=src。
命令：PYTHONPATH=src .venv/bin/python scripts/demo_confirmation_routing.py
重试：加 --retry-scenario recovered 验证超时恢复；unresolved 验证其他任务成功不覆盖原失败。
页面：同命令加 --serve（后端8004）；另在frontend执行：
    GOGO_API_PROXY_TARGET=http://127.0.0.1:8004 npm run dev -- --host 127.0.0.1 --port 5176 --strictPort
访问 http://localhost:5176，以 alice / 123456 登录，打开预置会话，连续两次发送“确认提交”。
模型改写和回答是固定替身；真实 SDK、管理工具、摘要令牌、事务均实际执行，不连接外部服务。
每次运行先预置一轮规划历史（没有模型调用）；具体断点与边界见 docs/契约样例/018-确认提交与方案选择调试.md。
"""

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import patch

from agentscope.message import TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatResponse
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gogo_agent.auth.dependencies import get_auth_service
from gogo_agent.auth.repository import InMemoryUserAccountRepository
from gogo_agent.auth.router import router as auth_router
from gogo_agent.auth.security import InMemoryTokenStore, TokenManager
from gogo_agent.auth.service import AuthService
from gogo_agent.chat.config import ChatAgentSettings
from gogo_agent.chat.dependencies import get_chat_executor, get_chat_history_service
from gogo_agent.chat.executor import ChatAgentExecutor, _FallbackMockModel
from gogo_agent.chat.repository import SQLAgentSessionStore, SQLChatHistoryRepository
from gogo_agent.chat.router import router as chat_router
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.db.base import Base
from gogo_agent.intent import IntentPipelineService, IntentRecognizer, IntentRuleMatcher, QueryRewriter, RewriteContextBuilder
from gogo_agent.travel.repository import BusinessRepository
from gogo_agent.travel.service import TravelBusinessService


SESSION_ID = "confirmation-demo"
DRAFT = dict(destination="上海", departure_city="北京", departure_date="2026-11-10",
             return_date="2026-11-12", purpose="客户拜访")
APPLICATION = "确认提交北京到上海的差旅申请，2026-11-10出发，2026-11-12返回，事由为客户拜访，酒店H2两晚"
SELECTION = "选择北京到上海的综合最佳行程方案，2026-11-10出发，2026-11-12返回，酒店H2两晚"


def tool_text(block: ToolResultBlock) -> str:
    """提取真实 SDK 工具结果的文本，不把内容块列表当 JSON 字符串。"""
    return block.output if isinstance(block.output, str) else "".join(
        item.text for item in block.output if isinstance(item, TextBlock)
    )


class ReplayModel(_FallbackMockModel):
    """固定供应商响应；真实 AgentScope 仍负责工具执行、事件和状态恢复。"""

    def __init__(self, role: str, *, stream=False):
        super().__init__(stream=stream)
        self.max_retries = 0
        self.client.max_retries = 0
        self.role = role
        self.tool_names: list[list[str]] = []

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.call_count += 1
        self.tool_names.append([item["function"]["name"] for item in tools or []])
        if self.role == "intent":
            payload = json.loads(messages[-1].get_text_content())
            if "query" not in payload:
                raise AssertionError("本例完整问题应由真实 L1 规则命中，不调用 L3")
            question = payload["query"]["question"]
            missing = not payload["history"]
            text = json.dumps({
                "related": not missing, "rewritten_question": None if missing else
                SELECTION if "选" in question else APPLICATION,
                "reason": "离线固定：从已给出的差旅申请历史补全对象，不用酒店名词代替动作",
                "evidence": [question, "历史明确讨论北京到上海的差旅申请，事由为客户拜访"],
                "missing_context": ["要确认的具体事项"] if missing else [],
            }, ensure_ascii=False)
            return ChatResponse(content=[TextBlock(text=text)], is_last=True)

        results = [block for message in messages for block in message.content if isinstance(block, ToolResultBlock)]
        if self.role == "master":
            if self.call_count == 1:
                question = next(message.get_text_content() for message in reversed(messages) if message.role == "user")
                content = [ToolCallBlock(id="confirm-manage", name="itinerary_manage_agent",
                                         input=json.dumps({"question": question}, ensure_ascii=False))]
            else:
                content = [TextBlock(text=tool_text(results[-1]))]
        else:
            if self.call_count == 1:
                # 只在新一轮用户确认后，才从已恢复的 Manage state 读取先前工具签发的令牌。
                tokens = [json.loads(tool_text(result)).get("data", {}).get("confirmation_token") for result in results]
                token = next((value for value in reversed(tokens) if value), "")
                content = [ToolCallBlock(id="confirm-submit", name="submit_travel_approval",
                                         input=json.dumps(dict(DRAFT, confirmation_token=token), ensure_ascii=False))]
            else:
                result = json.loads(tool_text(results[-1]))
                if result.get("success"):
                    text = "隔离演示：差旅申请已提交，等待审批。差旅单：" + result["data"]["order_id"]
                elif result.get("errorCode") == "CONFIRMATION_REQUIRED":
                    text = "隔离演示：请确认差旅申请摘要：北京→上海，2026-11-10至2026-11-12，事由为客户拜访。本轮仅生成摘要，尚未提交；核对后请回复“确认提交”。"
                else:
                    text = "尚未提交：" + result.get("message", "请核对摘要")
                content = [TextBlock(text=text)]
        response = ChatResponse(content=content, is_last=not self.stream)
        if not self.stream:
            return response

        async def chunks():
            yield response

        return chunks()


class TimeoutManageModel(ReplayModel):
    """固定制造一次子 Agent 超时，不等待真实供应商。"""

    async def _call_api(self, *args, **kwargs):
        raise TimeoutError("隔离演示的首次委派超时")


class RetryMasterModel(ReplayModel):
    """重放失败后再次委派；unresolved 模式故意把另一任务当成重试。"""

    def __init__(self, scenario, *, stream=False):
        super().__init__("master", stream=stream)
        self.scenario = scenario

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.call_count += 1
        results = [block for message in messages for block in message.content if isinstance(block, ToolResultBlock)]
        if self.call_count <= 2:
            question = next(message.get_text_content() for message in reversed(messages) if message.role == "user")
            if self.call_count == 1:
                arguments = {"question": question}
            elif self.scenario == "recovered":
                arguments = {"retry_of": json.loads(tool_text(results[-1]))["retry_of"]}
            else:
                arguments = {"question": "另一个任务：查询我的审批状态"}
            content = [ToolCallBlock(id=f"manage-attempt-{self.call_count}", name="itinerary_manage_agent",
                                     input=json.dumps(arguments, ensure_ascii=False))]
        else:
            content = [TextBlock(text=tool_text(results[-1]))]
        response = ChatResponse(content=content, is_last=not self.stream)
        if not self.stream:
            return response

        async def chunks():
            yield response

        return chunks()


@asynccontextmanager
async def replay_pipeline(history):
    model = ReplayModel("intent")
    try:
        yield IntentPipelineService(RewriteContextBuilder(history), QueryRewriter(model),
                                    IntentRecognizer(model, rule_matcher=IntentRuleMatcher()))
    finally:
        await model.client.close()


class ConfirmationExecutor(ChatAgentExecutor):
    """只替换外部模型与业务依赖的装配，复用正式主子 Agent builder。"""

    def __init__(self, history, sessions, business, retry_scenario=""):
        super().__init__(history, sessions, pipeline_factory=replay_pipeline, settings=ChatAgentSettings())
        self.business = business
        self.models: list[ReplayModel] = []
        self.retry_scenario = retry_scenario

    def _build_model(self, stream=False, *, role="master"):
        if self.retry_scenario and not any(item.role == role for item in self.models):
            model = (RetryMasterModel(self.retry_scenario, stream=stream) if role == "master"
                     else TimeoutManageModel(role, stream=stream))
        else:
            model = ReplayModel(role, stream=stream)
        self.models.append(model)
        return model

    def _build_manage_agent(self, request, state=None, *, trace=None):
        # 只在同步构造期间注入隔离仓储；工具闭包之后持有该仓储，不读全局数据库。
        with patch("gogo_agent.chat.executor.get_travel_service", return_value=self.business):
            return super()._build_manage_agent(request, state, trace=trace)

    async def recommend_questions(self, question, answer):
        return []


def create_demo_app(*, retry_scenario=""):
    """每次创建独立 SQLite、账号、业务历史和 AgentState，不读取 .env 数据库。"""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    history = ChatHistoryService(SQLChatHistoryRepository(factory))
    business = TravelBusinessService(BusinessRepository(factory))
    executor = ConfirmationExecutor(history, SQLAgentSessionStore(factory), business, retry_scenario)
    auth = AuthService(InMemoryUserAccountRepository(load_default_seeds=True), TokenManager(store=InMemoryTokenStore()))
    history.save_user_message(SESSION_ID, "u001", "为客户拜访规划北京到上海2026-11-10至11-12的行程，并准备提交差旅申请")
    history.save_assistant_message(SESSION_ID, "u001", "综合最佳方案为往返交通F12/F2和酒店H2两晚；差旅申请尚未提交。")
    application = FastAPI(title="确认提交路由离线验证")
    application.include_router(auth_router)
    application.include_router(chat_router)
    application.dependency_overrides[get_auth_service] = lambda: auth
    application.dependency_overrides[get_chat_executor] = lambda: executor
    application.dependency_overrides[get_chat_history_service] = lambda: history
    return application, auth, history, executor, business, engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8004)
    parser.add_argument("--retry-scenario", choices=("recovered", "unresolved"), default="")
    args = parser.parse_args()
    application, auth, history, _, business, engine = create_demo_app(retry_scenario=args.retry_scenario)
    try:
        if args.serve:
            import uvicorn
            uvicorn.run(application, host="127.0.0.1", port=args.port)
            return
        with TestClient(application) as client:
            token = auth.login("alice", "123456")
            if args.retry_scenario == "unresolved":
                response = client.post(f"/api/chat/{SESSION_ID}", headers={"Authorization": token}, json={"message": "确认提交"})
                assert "请确认差旅申请摘要" in response.text
                assert "event: error" in response.text and "event: message_id" not in response.text
                assert history.list_messages(SESSION_ID, "u001")[-1].role == "user"
                assert business.list_orders("u001") == []
                print("通过：另一任务成功未覆盖原失败；已流出的摘要没有保存为成功回复。")
                return
            for index, expected_count in enumerate([0, 1, 1], 1):
                response = client.post(f"/api/chat/{SESSION_ID}", headers={"Authorization": token, "Accept": "application/json"},
                                       json={"message": "确认提交"})
                assert response.status_code == 200, response.text
                message = history.list_messages(SESSION_ID, "u001")[-1]
                assert message.extra["intent_pipeline"]["intents"] == ["travel_application"]
                assert message.extra["tool_calls"][0]["agent_name"] == "ItineraryManageAgent"
                orders = business.list_orders("u001")
                assert len(orders) == expected_count
                print(f"第{index}轮：travel_application → GoGo → ItineraryManageAgent；差旅单数量={len(orders)}")
            print("通过：首次只生成摘要，后续确认提交一次，重复确认保持幂等。")
            print("边界：模型是固定替身；真实SDK、令牌与SQLite事务已执行；未访问外部模型或共享数据库。")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
