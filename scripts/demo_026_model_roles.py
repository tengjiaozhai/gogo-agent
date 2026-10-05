"""026：从 Java ModelConfig 对照 Python 模型分工与逐次调用记录。

Java 入口：agent/config/ModelConfig.java 构造 strongModel(qwen3.7-max)、
stableModel(glm-5.2)，MasterAgent.build() 用前者，InfoAgent.build()、
QueryRewritingAgent 和 IntentRecognitionAgent 的 L3 用后者。
Python 入口：ChatAgentExecutor._build_model() 构造 GoGo/InfoAgent 模型；
open_intent_pipeline() 把稳定模型交给 QueryRewriter 和 IntentRecognizer；
create_chat_model() 统一超时/重试/记录，ObservedOpenAIChatModel.__call__
在一次模型响应完成时写日志。这里的 main/stable 是调用角色，不是权限来源。

从仓库根目录运行，无需密钥、Qdrant、数据库或网络：
    PYTHONPATH=src .venv/bin/python scripts/demo_026_model_roles.py --case roles
    PYTHONPATH=src .venv/bin/python scripts/demo_026_model_roles.py --case switch
    PYTHONPATH=src .venv/bin/python scripts/demo_026_model_roles.py --case all
默认 --case all。roles 预期显示主模型 qwen3.7-max、意图/Info glm-5.2，
L3 命中层 llm；日志分别含 role、model、duration_ms 与 token 数，
Info 替身没有 usage，应显示 unavailable。switch 预期改成 main-026-alt 后，
可信 user_id、AgentState 键和仓储对象都保持原值。

建议按顺序打断点：
1. main() 的 patch.dict：看四个 GOGO_MODEL_* 变量；只有 demo-key，没读真实密钥。
2. load_intent_runtime_settings()：看 chat_model_name 与 stable_model_name 不同，
   两者共用 base_url/api_key；对应 Java ModelConfig 的两个 bean。
3. ChatAgentExecutor._build_model() 与 create_text_model()：看 role、model_name、
   stream、max_retries；像 Java DI 注入不同 ChatModel，这里工厂每次显式构造。
4. IntentRecognizer.recognize() 的 L3 调用与 ObservedOpenAIChatModel.__call__：
   看 response.usage 有 12/7 token；Info 替身的 usage 为 None，日志不能写成 0。
5. switch_roles()：看 request.user_id、state_key、history/store 的对象身份；
   改 GOGO_MODEL_NAME 后只改变新模型的 model 字段。

先预测：--case switch 中更换模型名后，state_key 会不会改变？roles 中哪条
模型调用的 token 是 unavailable？再分别运行核对。

asyncio.run() 启动 Python 事件循环，await 等模型调用完成；Java Reactor 的 Mono
在订阅时执行，二者都处理异步步骤，但执行时机与返回形状不同。生产
open_intent_pipeline() 用 async with 管理请求级 Qdrant 和模型生命周期；
本脚本直接创建模型，在 finally 显式关闭客户端。真实的配置读取、模型工厂、
IntentRecognizer L3 判定、会话归属和日志代码都参与运行；仅供应商 _call_api
响应由固定替身替换，也没有执行完整 HTTP/Agent 工具循环或计算真实费用。
"""

import argparse
import asyncio
import logging
import os
import sys
from unittest.mock import patch

from agentscope.credential import OpenAICredential
from agentscope.message import TextBlock, UserMsg
from agentscope.model import ChatResponse, ChatUsage

from gogo_agent.chat.executor import ChatAgentExecutor
from gogo_agent.chat.repository import InMemoryAgentSessionStore, InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.intent.models import ConfidenceLevel, IntentCategory, IntentItem, IntentResult, QueryInput
from gogo_agent.intent.runtime import load_intent_runtime_settings
from gogo_agent.intent.service import IntentRecognizer, create_text_model


def fixed_api(model, response: ChatResponse) -> None:
    """只替换供应商边界，保留真实 AgentScope __call__ 与调用观测。"""
    async def call(model_name, messages, tools=None, tool_choice=None, **kwargs):
        assert model_name == model.model
        return response

    model._call_api = call


async def show_roles(executor: ChatAgentExecutor) -> None:
    """离线运行 L3 与两种 Agent 模型的一次调用。"""
    settings = load_intent_runtime_settings()
    intent = create_text_model(
        OpenAICredential(api_key=settings.api_key, base_url=settings.base_url),
        settings.stable_model_name,
    )
    master = executor._build_model()
    info = executor._build_model(role="info")
    print(f"[模型分工] GoGo={master.model}，L3={intent.model}，InfoAgent={info.model}")
    result = IntentResult(
        intents=[IntentItem(
            intent=IntentCategory.GENERAL_INFO,
            confidence=ConfidenceLevel.HIGH,
            reason="026 固定分类",
            evidence=["请介绍上海"],
        )],
        primary_intent=IntentCategory.GENERAL_INFO,
        multi_intent=False,
        overall_reason="026 固定分类",
    )
    fixed_api(intent, ChatResponse(
        content=[TextBlock(text=result.model_dump_json())], is_last=True,
        usage=ChatUsage(input_tokens=12, output_tokens=7, time=0.01),
    ))
    fixed_api(master, ChatResponse(
        content=[TextBlock(text="主 Agent 的固定回答")], is_last=True,
        usage=ChatUsage(input_tokens=20, output_tokens=8, time=0.01),
    ))
    fixed_api(info, ChatResponse(content=[TextBlock(text="Info 的固定回答")], is_last=True))
    try:
        decision = await IntentRecognizer(intent).recognize(QueryInput(question="请介绍上海"))
        assert decision.hit_layer == "llm"
        print(f"[意图兜底] hit_layer={decision.hit_layer}，primary={decision.result.primary_intent}")
        await master([UserMsg(name="user", content="请介绍上海")])
        await info([UserMsg(name="GoGo", content="请介绍上海")])
    finally:
        await intent.client.close()
        await master.client.close()
        await info.client.close()


async def switch_roles(executor: ChatAgentExecutor, history: ChatHistoryService, sessions: InMemoryAgentSessionStore) -> None:
    """只切换模型名，核对认证身份与仓储引用。"""
    request = executor._save_user_turn("session-026-demo", "u001", "请介绍上海")
    before = executor._state_session_id(request.session_id, request.user_id)
    initial = executor._build_model()
    try:
        print(f"[切换前] model={initial.model}，user_id={request.user_id}，state_key={before}")
    finally:
        await initial.client.close()
    with patch.dict(os.environ, {"GOGO_MODEL_NAME": "main-026-alt"}):
        changed = executor._build_model()
        try:
            assert changed.model == "main-026-alt"
            assert executor._state_session_id(request.session_id, request.user_id) == before
            assert executor.history_service is history and executor.session_store is sessions
            assert history.list_messages(request.session_id, request.user_id)[0].content == "请介绍上海"
            print("[切换后] model=main-026-alt，user_id=u001，state_key 不变，历史/状态仓储对象不变")
        finally:
            await changed.client.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description="026 模型分工和调用观测离线演示")
    parser.add_argument("--case", choices=("roles", "switch", "all"), default="all")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    with patch.dict(os.environ, {
        "GOGO_MODEL_API_KEY": "demo-key",
        "GOGO_MODEL_BASE_URL": "https://example.invalid/v1",
        "GOGO_MODEL_NAME": "qwen3.7-max",
        "GOGO_STABLE_MODEL_NAME": "glm-5.2",
    }):
        history = ChatHistoryService(InMemoryChatHistoryRepository())
        sessions = InMemoryAgentSessionStore()
        executor = ChatAgentExecutor(history, sessions)
        if args.case in ("roles", "all"):
            await show_roles(executor)
        if args.case in ("switch", "all"):
            await switch_roles(executor, history, sessions)


if __name__ == "__main__":
    asyncio.run(main())
