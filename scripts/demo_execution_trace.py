"""执行时间轴离线调试入口：真实 AgentScope 主/子 Agent 与工具，固定模型和候选。

在仓库根目录准备依赖：uv sync --frozen；解释器为 .venv/bin/python。
零网络运行：PYTHONPATH=src .venv/bin/python scripts/demo_execution_trace.py
浏览器后端：PYTHONPATH=src .venv/bin/python scripts/demo_execution_trace.py --serve
另开终端：cd frontend && GOGO_API_PROXY_TARGET=http://127.0.0.1:8002 npm run dev -- --port 5174
浏览器打开 http://localhost:5174，以 alice / 123456 登录，发送“帮我规划北京到上海的行程”。
--fail-search 用于查看真实工具返回 success=false 时的失败展示；不会访问途牛或业务数据库。
断点顺序、输出和 Java 对照见 docs/契约样例/执行过程时间轴-调试指南.md。
"""

import argparse
import asyncio
import json
from contextlib import asynccontextmanager

from agentscope.message import ThinkingBlock, TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatResponse
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gogo_agent.auth.dependencies import get_auth_service
from gogo_agent.auth.repository import InMemoryUserAccountRepository
from gogo_agent.auth.router import router as auth_router
from gogo_agent.auth.security import InMemoryTokenStore, TokenManager
from gogo_agent.auth.service import AuthService
from gogo_agent.chat.config import ChatAgentSettings
from gogo_agent.chat.dependencies import get_chat_executor, get_chat_history_service
from gogo_agent.chat.executor import ChatAgentExecutor, _FallbackMockModel
from gogo_agent.chat.repository import InMemoryAgentSessionStore, InMemoryChatHistoryRepository
from gogo_agent.chat.router import router as chat_router
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.intent import IntentPipelineService, IntentRecognizer, RewriteContextBuilder
from gogo_agent.intent.rules import IntentRuleMatcher
from gogo_agent.planning.search import PlanSearchError
from gogo_agent.planning.simulated import simulated_provider


QUESTION = "帮我规划北京到上海的行程，2026-11-10出发，2026-11-12返回"


def result_text(result: ToolResultBlock) -> str:
    """SDK 工具结果可为字符串或内容块列表，这里只读取文本块。"""
    return result.output if isinstance(result.output, str) else "".join(
        block.text for block in result.output if isinstance(block, TextBlock)
    )


class DemoModel(_FallbackMockModel):
    """固定供应商输出；真实 SDK 仍负责工具执行、事件、上下文和权限。"""

    def __init__(self, role: str, stream: bool = False, delay: float = 0):
        super().__init__(stream=stream)
        self.role = role
        self.delay = delay

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.call_count += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        results = [block for message in messages for block in message.content if isinstance(block, ToolResultBlock)]
        content = []
        if self.role == "master":
            if self.call_count == 1:
                content = [ThinkingBlock(thinking="用户希望规划行程，本轮开放 itinerary_plan_agent；将完整条件交给行程规划智能体。"),
                           ToolCallBlock(id="demo-delegate", name="itinerary_plan_agent",
                                         input=json.dumps({"question": QUESTION}, ensure_ascii=False))]
            else:
                child_answer = result_text(results[-1]) if results else "无子 Agent 返回"
                content = [ThinkingBlock(thinking="子 Agent 已返回，依据工具结果整理最终回复；不声称已经预订。"),
                           TextBlock(text="GoGo 汇总（离线演示）：" + child_answer)]
        elif self.role == "plan":
            if self.call_count == 1:
                content = [ThinkingBlock(thinking="条件齐全，先创建计划，再使用 plan_id 搜索候选。"),
                           ToolCallBlock(id="demo-create", name="create_plan", input=json.dumps({
                               "origin": "北京", "destination": "上海", "departure_date": "2026-11-10",
                               "return_date": "2026-11-12",
                           }, ensure_ascii=False))]
            elif self.call_count == 2:
                plan_id = json.loads(result_text(results[-1]))["data"]["plan_id"]
                content = [ToolCallBlock(id="demo-search", name="search_candidates",
                                         input=json.dumps({"plan_id": plan_id}))]
            else:
                payload = json.loads(result_text(results[-1]))
                text = ("模拟交通和酒店候选已保存。这里只验证搜索链路，尚未计算或审核方案；未预订、未提交差旅单。"
                        if payload.get("success") else "候选搜索失败：" + payload.get("message", "未知原因"))
                content = [ThinkingBlock(thinking="按搜索工具的实际返回汇报当前状态，不补造候选或价格。"), TextBlock(text=text)]
        else:
            content = [TextBlock(text="离线固定回复")]
        response = ChatResponse(content=content, is_last=not self.stream)
        if not self.stream:
            return response

        async def chunks():
            yield response

        return chunks()


class NoRewrite:
    """演示句由真实 L1 规则命中，不应调用外部改写或向量库。"""

    async def rewrite(self, context):
        raise ValueError("本离线入口只接受明确的行程规划句，例如：" + QUESTION)


@asynccontextmanager
async def demo_pipeline(history):
    model = _FallbackMockModel()
    try:
        yield IntentPipelineService(RewriteContextBuilder(history), NoRewrite(),
                                    IntentRecognizer(model, rule_matcher=IntentRuleMatcher()))
    finally:
        await model.client.close()


class DemoExecutor(ChatAgentExecutor):
    """仅替换模型与供应商；业务工具、鉴权、SSE 和状态保存沿用正式代码。"""

    def __init__(self, history, *, fail_search=False, delay=0):
        super().__init__(history, InMemoryAgentSessionStore(), pipeline_factory=demo_pipeline,
                         settings=ChatAgentSettings())
        self.fail_search = fail_search
        self.delay = delay

    def _build_model(self, stream=False, *, role="master"):
        return DemoModel(role, stream=stream, delay=self.delay)

    def _plan_search_call(self, user_id, conditions):
        if self.fail_search:
            raise PlanSearchError("DEMO_SEARCH_FAILED", "离线演示：候选服务不可用")
        return simulated_provider(user_id, conditions)

    async def recommend_questions(self, question, answer):
        return []


def create_demo_app(*, fail_search=False, delay=0):
    """独立内存应用，不读取 .env 数据库，不挂载业务写接口。"""
    application = FastAPI(title="GoGo 执行时间轴离线演示")
    auth = AuthService(InMemoryUserAccountRepository(load_default_seeds=True),
                       TokenManager(store=InMemoryTokenStore()))
    history = ChatHistoryService(InMemoryChatHistoryRepository())
    executor = DemoExecutor(history, fail_search=fail_search, delay=delay)
    application.include_router(auth_router)
    application.include_router(chat_router)
    application.dependency_overrides[get_auth_service] = lambda: auth
    application.dependency_overrides[get_chat_executor] = lambda: executor
    application.dependency_overrides[get_chat_history_service] = lambda: history
    return application, auth, history, executor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--fail-search", action="store_true")
    args = parser.parse_args()
    application, auth, history, _ = create_demo_app(fail_search=args.fail_search, delay=0.15 if args.serve else 0)
    if args.serve:
        import uvicorn
        uvicorn.run(application, host="127.0.0.1", port=args.port)
        return
    with TestClient(application) as client:
        token = auth.login("alice", "123456")
        response = client.post("/api/chat/demo-trace", headers={"Authorization": token, "Accept": "text/event-stream"},
                               json={"message": QUESTION})
        assert "event: message_id" in response.text, response.text
        timeline = history.list_messages("demo-trace", "u001")[-1].extra["timeline"]
        for item in timeline:
            print(item["kind"], item["agentName"], item.get("status", ""), item.get("title", ""))
        print("历史时间轴条目：", len(timeline))
        print("模拟边界：固定模型、内存会话与模拟候选；未访问真实网关/途牛/数据库。")


if __name__ == "__main__":
    main()
