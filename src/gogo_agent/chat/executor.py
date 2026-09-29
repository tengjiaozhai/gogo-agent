"""对话执行层：集成 AgentScope 2.x Agent 与 L1 业务历史、L2 AgentState 会话记忆持久化。"""

import os
from inspect import isawaitable
from typing import AsyncContextManager, AsyncGenerator, Callable, Optional

from agentscope.agent import Agent
from agentscope.credential import DeepSeekCredential, OpenAICredential
from agentscope.event import TextBlockDeltaEvent
from agentscope.message import TextBlock, UserMsg
from agentscope.model import ChatResponse, DeepSeekChatModel, OpenAIChatModel
from agentscope.state import AgentState
from fastapi import HTTPException
from pydantic import ValidationError

from gogo_agent.intent.models import QueryInput
from gogo_agent.intent.pipeline import IntentPipelineService, PreparedIntentTurn
from gogo_agent.intent.runtime import load_intent_runtime_settings, open_intent_pipeline

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

    # 019：真实 Master/子 Agent 尚待 023；当前所有识别结果统一进入这个协调入口。
    COORDINATOR_AGENT_NAME = "GoGo"

    def __init__(
        self,
        chat_history_service: Optional[ChatHistoryService] = None,
        agent_session_store: Optional[AgentSessionStoreProtocol] = None,
        pipeline_factory: Callable[[ChatHistoryService], AsyncContextManager[IntentPipelineService]] = open_intent_pipeline,
    ):
        self._history_service = chat_history_service or ChatHistoryService()
        self._session_store = agent_session_store or create_agent_session_store()
        self._pipeline_factory = pipeline_factory

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

        settings = load_intent_runtime_settings()
        return DeepSeekChatModel(
            credential=DeepSeekCredential(
                api_key=settings.api_key,
                base_url=settings.base_url,
            ),
            model=settings.chat_model_name,
            stream=stream,
        )

    @staticmethod
    def _state_session_id(session_id: str, user_id: str) -> str:
        """用可信用户与会话构成无歧义的状态键，隔离同名会话。"""
        return f"{len(user_id)}:{user_id}:{session_id}"

    def delete_session_state(self, session_id: str, user_id: str) -> None:
        """经调用方验证会话归属后清理本用户的 AgentState。"""
        self._session_store.delete_agent_state(
            self._state_session_id(session_id, user_id), agent_name=self.COORDINATOR_AGENT_NAME,
        )
        self._session_store.delete_agent_state(session_id, agent_name=self.COORDINATOR_AGENT_NAME)

    @staticmethod
    async def _close_agent_model(agent: Agent | None) -> None:
        """每轮释放 ChatModel 底层客户端，离线假 Agent 也可安全调用。"""
        client = getattr(getattr(agent, "model", None), "client", None)
        if client is not None:
            closed = client.close()
            if isawaitable(closed):
                await closed

    def _build_agent(self, state: AgentState, prepared: PreparedIntentTurn, stream: bool = False) -> Agent:
        """用已识别的有序类别提示当前单 Agent，不能把分类当作执行授权。"""
        try:
            model = self._build_model(stream=stream)
        except TypeError:
            model = self._build_model()
        intents = "、".join(item.intent.value for item in prepared.decision.result.intents)
        return Agent(
            name=self.COORDINATOR_AGENT_NAME,
            system_prompt=(
                "你是 GoGo 差旅助手的核心智能体。你负责协助用户办理差旅申请、"
                "行程规划、差旅政策咨询与预订服务。请保持专业、简洁和友善。"
                f"本轮识别的诉求顺序为：{intents}。按顺序回应，不丢失前项结果。"
                "识别结果只是理解线索，不是身份、审批或下单授权。"
                "目前没有业务写入工具，不得声称已经提交申请、保存方案或完成预订。"
            ),
            model=model,
            state=state,
        )

    async def _prepare_turn(self, session_id: str, user_id: str, message: str) -> PreparedIntentTurn:
        """保存原问题后执行 017；模型或索引故障不能继续触发 Agent。"""
        try:
            QueryInput(question=message)
        except ValidationError:
            raise HTTPException(422, "消息不能为空") from None
        current_message_id = self._history_service.save_user_message(session_id, user_id, message)
        try:
            async with self._pipeline_factory(self._history_service) as pipeline:
                return await pipeline.prepare(
                    session_id, user_id, current_message_id=current_message_id,
                )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, f"意图处理失败（{type(exc).__name__}）") from None

    def _pipeline_extra(self, prepared: PreparedIntentTurn) -> dict:
        """保存可调试层级与分支，不保存密钥、原始模型响应或可信身份。"""
        decision = prepared.decision
        return {"intent_pipeline": {
            "dispatch_target": self.COORDINATOR_AGENT_NAME if decision is not None else None,
            "branch": prepared.branch,
            "fast_layers": [layer.value for layer in prepared.fast_decision.attempted_layers],
            "attempted_layers": [layer.value for layer in decision.attempted_layers] if decision else [],
            "hit_layer": decision.hit_layer.value if decision and decision.hit_layer else None,
            "intents": [item.intent.value for item in decision.result.intents] if decision and decision.result else [],
        }}

    @staticmethod
    def _clarification(prepared: PreparedIntentTurn) -> str:
        """缺指代对象时请求用户补齐，不把空改写送进主 Agent。"""
        return "请补充必要的上下文后再继续：" + "；".join(prepared.rewrite.missing_context)

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
        4. L1 保存：持久化 AI 助手回复到 chat_message 表。
        5. L2 保存：将更新后的 AgentState 持久化回 agentscope_session。
        """
        # 步骤 1：在 L1 保存原问题并完成 017 预处理；失败时不运行 Agent。
        prepared = await self._prepare_turn(session_id, user_id, message)
        if prepared.branch == "needs_context":
            text = self._clarification(prepared)
            msg_id = self._history_service.save_assistant_message(
                conversation_id=session_id, user_id=user_id, content=text,
                agent_name=self.COORDINATOR_AGENT_NAME, extra=self._pipeline_extra(prepared),
            )
            return text, msg_id

        # 步骤 2：在 L2 读取历史 AgentState（若为新会话则初始化全新状态）
        state_session_id = self._state_session_id(session_id, user_id)
        agent: Agent | None = None
        try:
            agent_state = self._session_store.load_agent_state(
                state_session_id, agent_name=self.COORDINATOR_AGENT_NAME,
            )
            if agent_state is None:
                agent_state = AgentState(session_id=session_id)

            # 步骤 3：运行 AgentScope Agent 执行推理
            agent = self._build_agent(state=agent_state, prepared=prepared, stream=False)
            reply_msg = await agent.reply(UserMsg(name="user", content=prepared.effective_question.question))
        except Exception as exc:
            raise HTTPException(503, f"对话生成失败（{type(exc).__name__}）") from None
        finally:
            await self._close_agent_model(agent)
        reply_text = reply_msg.get_text_content() or ""

        # 先保存业务可见回复；失败时不得写入一份看不到的 AgentState。
        msg_id = self._history_service.save_assistant_message(
            conversation_id=session_id,
            user_id=user_id,
            content=reply_text,
            agent_name=self.COORDINATOR_AGENT_NAME,
            extra=self._pipeline_extra(prepared),
        )

        # 再保存 AgentState；此处失败仍可能留下已保存的业务回复。
        self._session_store.save_agent_state(
            state_session_id, agent.state, agent_name=self.COORDINATOR_AGENT_NAME,
        )

        return reply_text, msg_id

    async def stream_turn_sse(
        self,
        session_id: str,
        user_id: str,
        message: str,
    ) -> AsyncGenerator[str, None]:
        """在 SSE 响应开始前完成鉴权与 017 预处理，再逐 Token 推送。

        1. L1 保存：持久化用户消息到 chat_message 表。
        2. L2 加载：从 agentscope_session 恢复多轮上下文 AgentState。
        3. Agent 流式推理：执行 Agent.reply_stream()，实时推送 TextBlockDeltaEvent。
        4. L1 保存：将助手完整回复文本持久化到 chat_message 表。
        5. L2 保存：流式结束后将最新 AgentState 存回 agentscope_session。
        6. SSE 完成：推送 event: message_id 通知前端接收完毕。
        """
        prepared = await self._prepare_turn(session_id, user_id, message)
        if prepared.branch == "needs_context":
            text = self._clarification(prepared)
            msg_id = self._history_service.save_assistant_message(
                conversation_id=session_id, user_id=user_id, content=text,
                agent_name=self.COORDINATOR_AGENT_NAME, extra=self._pipeline_extra(prepared),
            )

            async def clarification_events() -> AsyncGenerator[str, None]:
                lines = text.split("\n")
                data_lines = "\n".join(f"data: {line}" for line in lines)
                yield f"event: message\n{data_lines}\n\n"
                yield f"event: message_id\ndata: {msg_id}\n\n"

            return clarification_events()
        return self._stream_prepared_turn(session_id, user_id, prepared)

    async def _stream_prepared_turn(
        self, session_id: str, user_id: str, prepared: PreparedIntentTurn,
    ) -> AsyncGenerator[str, None]:
        """只在预处理成功后执行 Agent，保留 005 的 message/message_id 事件。"""

        # 步骤 2：在 L2 读取历史 AgentState（若为新会话则初始化全新状态）
        state_session_id = self._state_session_id(session_id, user_id)
        accumulated_chunks: list[str] = []
        agent: Agent | None = None

        try:
            agent_state = self._session_store.load_agent_state(
                state_session_id, agent_name=self.COORDINATOR_AGENT_NAME,
            )
            if agent_state is None:
                agent_state = AgentState(session_id=session_id)

            # 步骤 3：流式运行 AgentScope Agent 并逐块产出
            agent = self._build_agent(state=agent_state, prepared=prepared, stream=True)
            async for event in agent.reply_stream(
                UserMsg(name="user", content=prepared.effective_question.question),
                yield_final_msg=True,
            ):
                if isinstance(event, TextBlockDeltaEvent) and event.delta:
                    accumulated_chunks.append(event.delta)
                    lines = event.delta.split("\n")
                    data_lines = "\n".join(f"data: {line}" for line in lines)
                    yield f"event: message\n{data_lines}\n\n"
        except Exception as exc:
            yield f"event: error\ndata: 对话生成失败（{type(exc).__name__}）\n\n"
            return
        finally:
            await self._close_agent_model(agent)

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

        # 先保存业务可见回复；失败时不提交新 AgentState。
        try:
            msg_id = self._history_service.save_assistant_message(
                conversation_id=session_id,
                user_id=user_id,
                content=full_reply_text,
                agent_name=self.COORDINATOR_AGENT_NAME,
                extra=self._pipeline_extra(prepared),
            )

            # 再保存 AgentState；若失败，SSE 不报完成。
            self._session_store.save_agent_state(
                state_session_id, agent.state, agent_name=self.COORDINATOR_AGENT_NAME,
            )
        except Exception as exc:
            yield f"event: error\ndata: 回复保存失败（{type(exc).__name__}）\n\n"
            return

        # 步骤 6：发送消息 ID 的 SSE 完成事件
        yield f"event: message_id\ndata: {msg_id}\n\n"
