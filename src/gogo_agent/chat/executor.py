"""对话执行层：集成 AgentScope 2.x Agent 与 L1 业务历史、L2 AgentState 会话记忆持久化。"""

import os
from typing import AsyncGenerator, Optional
from urllib.parse import urlparse

from agentscope.agent import Agent
from agentscope.credential import DeepSeekCredential, OpenAICredential
from agentscope.event import TextBlockDeltaEvent
from agentscope.message import TextBlock, UserMsg
from agentscope.model import ChatResponse, DeepSeekChatModel, OpenAIChatModel
from agentscope.state import AgentState

from .repository import (
    AgentSessionStoreProtocol,
    create_agent_session_store,
)
from .service import ChatHistoryService


class _FallbackMockModel(OpenAIChatModel):
    """离线回退模拟模型：在无 API Key 等凭证时提供离线响应与流式分块。"""

    def __init__(self, stream: bool = False):
        super().__init__(
            credential=OpenAICredential(api_key="mock-key"),
            model="mock-gogo-agent",
            stream=stream,
        )
        self.call_count = 0

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.call_count += 1
        # 提取最后一条用户输入文本
        last_user_text = ""
        for m in reversed(messages):
            if m.role == "user":
                last_user_text = m.get_text_content() or ""
                break

        reply = (
            f"你好！我是 GoGo 差旅助手（模拟模式）。"
            f"已收到你的消息：'{last_user_text}'。"
        )

        if not self.stream:
            return ChatResponse(
                id=f"mock_res_{self.call_count}",
                content=[TextBlock(text=reply)],
                is_last=True,
            )

        async def _stream_chunks():
            chunks = [
                "你好！",
                "我是 GoGo 差旅助手（模拟模式）。",
                f"已收到你的消息：'{last_user_text}'。",
            ]
            for chunk in chunks:
                yield ChatResponse(
                    id=f"mock_res_{self.call_count}",
                    content=[TextBlock(text=chunk, id="mock_text_block")],
                    is_last=False,
                )

        return _stream_chunks()


class ChatAgentExecutor:
    """协调消息持久化、AgentState 生命周期及 AgentScope 智能体执行。"""

    def __init__(
        self,
        chat_history_service: Optional[ChatHistoryService] = None,
        agent_session_store: Optional[AgentSessionStoreProtocol] = None,
    ):
        self._history_service = chat_history_service or ChatHistoryService()
        self._session_store = agent_session_store or create_agent_session_store()

    @property
    def history_service(self) -> ChatHistoryService:
        return self._history_service

    @property
    def session_store(self) -> AgentSessionStoreProtocol:
        return self._session_store

    def _build_model(self, stream: bool = False):
        """构建 ChatModel：若存在环境变量凭证则构建真实大模型，否则回退到模拟模型。"""
        required = ("GOGO_MODEL_API_KEY", "GOGO_MODEL_NAME", "GOGO_MODEL_BASE_URL")
        has_env = all(bool(os.environ.get(k, "").strip()) for k in required)
        if not has_env:
            return _FallbackMockModel(stream=stream)

        base_url = os.environ["GOGO_MODEL_BASE_URL"].strip().rstrip("/")
        parsed = urlparse(base_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return _FallbackMockModel(stream=stream)

        return DeepSeekChatModel(
            credential=DeepSeekCredential(
                api_key=os.environ["GOGO_MODEL_API_KEY"].strip(),
                base_url=f"{base_url}/v1",
            ),
            model=os.environ["GOGO_MODEL_NAME"].strip(),
            stream=stream,
        )

    def _build_agent(self, state: AgentState, stream: bool = False) -> Agent:
        """使用给定的 AgentState 实例化 AgentScope Agent。"""
        try:
            model = self._build_model(stream=stream)
        except TypeError:
            model = self._build_model()
        return Agent(
            name="GoGo",
            system_prompt=(
                "你是 GoGo 差旅助手的核心智能体。你负责协助用户办理差旅申请、"
                "行程规划、差旅政策咨询与预订服务。请保持专业、简洁和友善。"
            ),
            model=model,
            state=state,
        )

    async def execute_turn(
        self,
        session_id: str,
        user_id: str,
        message: str,
    ) -> tuple[str, str]:
        """执行单轮完整对话。

        1. L1 保存：持久化用户消息到 chat_message 表。
        2. L2 加载：从 agentscope_session 恢复多轮上下文 AgentState。
        3. Agent 执行：调用 Agent.reply() 推理并更新内部 AgentState。
        4. L2 保存：将更新后的 AgentState 持久化回 agentscope_session。
        5. L1 保存：持久化 AI 助手回复到 chat_message 表。
        """
        # 步骤 1：在 L1 保存用户消息（同时校验会话归属权）
        self._history_service.save_user_message(session_id, user_id, message)

        # 步骤 2：在 L2 读取历史 AgentState（若为新会话则初始化全新状态）
        agent_state = self._session_store.load_agent_state(session_id, agent_name="GoGo")
        if agent_state is None:
            agent_state = AgentState(session_id=session_id)

        # 步骤 3：运行 AgentScope Agent 执行推理
        agent = self._build_agent(state=agent_state, stream=False)
        reply_msg = await agent.reply(UserMsg(name="user", content=message))
        reply_text = reply_msg.get_text_content() or ""

        # 步骤 4：在 L2 保存更新后的 AgentState
        self._session_store.save_agent_state(session_id, agent.state, agent_name="GoGo")

        # 步骤 5：在 L1 保存助手回复文本
        msg_id = self._history_service.save_assistant_message(
            conversation_id=session_id,
            user_id=user_id,
            content=reply_text,
            agent_name="GoGo",
        )

        return reply_text, msg_id

    async def stream_turn_sse(
        self,
        session_id: str,
        user_id: str,
        message: str,
    ) -> AsyncGenerator[str, None]:
        """基于标准 Server-Sent Events (SSE) 逐 Token 流式执行单轮对话。

        1. L1 保存：持久化用户消息到 chat_message 表。
        2. L2 加载：从 agentscope_session 恢复多轮上下文 AgentState。
        3. Agent 流式推理：执行 Agent.reply_stream()，实时推送 TextBlockDeltaEvent。
        4. L2 保存：流式结束后将最新 AgentState 存回 agentscope_session。
        5. L1 保存：将助手完整回复文本持久化到 chat_message 表。
        6. SSE 完成：推送 event: message_id 通知前端接收完毕。
        """
        # 步骤 1：在 L1 保存用户消息（同时校验会话归属权）
        self._history_service.save_user_message(session_id, user_id, message)

        # 步骤 2：在 L2 读取历史 AgentState（若为新会话则初始化全新状态）
        agent_state = self._session_store.load_agent_state(session_id, agent_name="GoGo")
        if agent_state is None:
            agent_state = AgentState(session_id=session_id)

        # 步骤 3：流式运行 AgentScope Agent 并逐块产出
        agent = self._build_agent(state=agent_state, stream=True)
        accumulated_chunks: list[str] = []

        async for event in agent.reply_stream(
            UserMsg(name="user", content=message),
            yield_final_msg=True,
        ):
            if isinstance(event, TextBlockDeltaEvent) and event.delta:
                accumulated_chunks.append(event.delta)
                lines = event.delta.split("\n")
                data_lines = "\n".join(f"data: {line}" for line in lines)
                yield f"event: message\n{data_lines}\n\n"

        full_reply_text = "".join(accumulated_chunks)
        # 保底容错：若未捕获到 delta 事件但状态中已生成消息，输出完整回复
        if not full_reply_text and agent.state.context:
            last_msg = agent.state.context[-1]
            if getattr(last_msg, "role", "") == "assistant":
                fallback_text = last_msg.get_text_content() or ""
                if fallback_text:
                    full_reply_text = fallback_text
                    lines = full_reply_text.split("\n")
                    data_lines = "\n".join(f"data: {line}" for line in lines)
                    yield f"event: message\n{data_lines}\n\n"

        # 步骤 4：在 L2 保存更新后的 AgentState
        self._session_store.save_agent_state(session_id, agent.state, agent_name="GoGo")

        # 步骤 5：在 L1 保存助手完整回复
        msg_id = self._history_service.save_assistant_message(
            conversation_id=session_id,
            user_id=user_id,
            content=full_reply_text,
            agent_name="GoGo",
        )

        # 步骤 6：发送消息 ID 的 SSE 完成事件
        yield f"event: message_id\ndata: {msg_id}\n\n"
