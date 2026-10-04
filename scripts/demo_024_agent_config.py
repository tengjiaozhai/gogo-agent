"""024 离线断点：启动校验、主/子 Agent 轮次与只读工具超时。"""

import asyncio
import os
from contextlib import asynccontextmanager
from unittest.mock import patch

from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatResponse
from fastapi import HTTPException
from fastapi.testclient import TestClient

from demo_023_master_agent import FixedRule, InfoModel, MasterModel, UnusedRewriter
from gogo_agent.api import app
from gogo_agent.chat.config import ChatAgentSettings, load_chat_agent_settings
from gogo_agent.chat.executor import ChatAgentExecutor, _FallbackMockModel
from gogo_agent.chat.repository import InMemoryAgentSessionStore, InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.intent import IntentPipelineService, IntentRecognizer, RewriteContextBuilder


class SlowInfoModel(_FallbackMockModel):
    """让只读子 Agent 等待超过应用设置的工具超时。"""

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        await asyncio.sleep(1)
        return ChatResponse(content=[TextBlock(text="迟到的回答")], is_last=True)


class RepeatingMasterModel(_FallbackMockModel):
    """持续提议同一工具调用，让 AgentScope 达到轮次上限。"""

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.call_count += 1
        print(f"  主 Agent 推理第 {self.call_count} 次")
        return ChatResponse(content=[ToolCallBlock(
            id=f"repeat-{self.call_count}", name="info_agent",
            input='{"question":"请介绍一处景点"}',
        )], is_last=True)


async def run_turn(case: str) -> None:
    """保留真实执行器、AgentScope 工具循环和内存状态，只替换供应商模型。"""
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

    settings = ChatAgentSettings(
        master_max_iters=1 if case == "limit" else 15,
        info_tool_timeout_seconds=0.02 if case == "timeout" else 60,
    )
    executor = ChatAgentExecutor(history, store, pipeline_factory=pipeline_factory, settings=settings)
    master = RepeatingMasterModel() if case == "limit" else MasterModel("info_agent")
    child = InfoModel() if case == "limit" else SlowInfoModel()
    built = 0

    def build_model(stream=False):
        nonlocal built
        built += 1
        return master if built == 1 else child

    executor._build_model = build_model
    session_id = f"demo-024-{case}"
    try:
        await executor.execute_turn(session_id, "u001", "请介绍一处景点")
    except HTTPException as exc:
        print(f"  HTTP {exc.status_code}：{exc.detail}")
    else:
        raise AssertionError("本例应走 024 错误出口")
    roles = [message.role for message in history.list_messages(session_id, "u001")]
    state_key = executor._state_session_id(session_id, "u001")
    assert roles == ["user"]
    assert store.load_agent_state(state_key, agent_name="GoGo") is None
    print("  已保存角色：", roles, "；成功状态：无")


def startup_example() -> None:
    """缺少模型配置时，由真实 FastAPI lifespan 在接受请求前拒绝启动。"""
    with patch.dict(os.environ, {
        "GOGO_MODEL_API_KEY": "",
        "GOGO_MODEL_NAME": "",
        "GOGO_MODEL_BASE_URL": "",
    }):
        try:
            with TestClient(app):
                pass
        except ValueError as exc:
            print("  API 启动失败：", exc)
        else:
            raise AssertionError("缺少模型配置时不应启动 API")


async def main() -> None:
    """依次展示配置输入、启动失败、工具超时与轮次耗尽。"""
    print("[配置读取]")
    with patch.dict(os.environ, {
        "GOGO_MASTER_MAX_ITERS": "2",
        "GOGO_INFO_MAX_ITERS": "3",
    }):
        settings = load_chat_agent_settings()
        assert settings.master_max_iters == 2 and settings.info_max_iters == 3
        print("  环境变量调整后：Master max_iters=2，Info max_iters=3")

    print("\n[缺模型配置]")
    startup_example()
    print("\n[InfoAgent 工具超时：在 ask_info_agent 的 timeout 处打断点]")
    await run_turn("timeout")
    print("\n[GoGo 轮次耗尽：在 _completion_error 处打断点]")
    await run_turn("limit")
    print("\n本脚本的模型和信息内容是本地替身；正式 API 启动校验与 AgentScope 工具循环使用实际实现。")


if __name__ == "__main__":
    asyncio.run(main())
