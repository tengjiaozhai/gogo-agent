"""对话执行层：集成 AgentScope 2.x Agent 与 L1 业务历史、L2 AgentState 会话记忆持久化。"""

import asyncio
import logging
from inspect import isawaitable
from typing import AsyncContextManager, AsyncGenerator, Callable, Optional

from agentscope.agent import Agent, ModelConfig, ReActConfig
from agentscope.credential import OpenAICredential
from agentscope.event import ReplyFinishedReason, TextBlockDeltaEvent
from agentscope.message import Msg, TextBlock, UserMsg
from agentscope.model import ChatResponse, OpenAIChatModel
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, Toolkit
from fastapi import HTTPException
from pydantic import ValidationError

from gogo_agent.intent.models import IntentCategory, QueryInput
from gogo_agent.intent.pipeline import IntentPipelineService, PreparedIntentTurn
from gogo_agent.intent.runtime import open_intent_pipeline
from gogo_agent.model import create_chat_model
from gogo_agent.request_context import RequestContext, current_trace_id, trace_scope

from .continuation import ActiveAgentContinuation, TurnEntry, choose_turn_entry
from .config import (
    INFO_SYSTEM_PROMPT, MODEL_MAX_RETRIES, ChatAgentSettings,
    load_chat_agent_settings, master_system_prompt, require_model_configuration,
)
from .repository import (
    AgentSessionStoreProtocol,
    create_agent_session_store,
)
from .service import ChatHistoryService

logger = logging.getLogger(__name__)


class _FallbackMockModel(OpenAIChatModel):
    """测试和学习脚本显式注入的离线模型替身。"""

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
    """协调消息、AgentState 和主 Agent 的只读信息子 Agent 调用。"""

    # 019：所有识别结果仍进入同一个协调入口，高置信不直跳子 Agent。
    COORDINATOR_AGENT_NAME = "GoGo"
    INFO_AGENT_NAME = "InfoAgent"
    INFO_TOOL_NAME = "info_agent"

    def __init__(
        self,
        chat_history_service: Optional[ChatHistoryService] = None,
        agent_session_store: Optional[AgentSessionStoreProtocol] = None,
        pipeline_factory: Callable[[ChatHistoryService], AsyncContextManager[IntentPipelineService]] = open_intent_pipeline,
        active_continuation: ActiveAgentContinuation | None = None,
        settings: ChatAgentSettings | None = None,
    ):
        self._history_service = chat_history_service or ChatHistoryService()
        self._session_store = agent_session_store or create_agent_session_store()
        self._pipeline_factory = pipeline_factory
        self._active_continuation = active_continuation
        self._settings = settings or load_chat_agent_settings()

    @property
    def history_service(self) -> ChatHistoryService:
        return self._history_service

    @property
    def session_store(self) -> AgentSessionStoreProtocol:
        return self._session_store

    def _build_model(self, stream: bool = False, *, role: str = "master"):
        """正式模型按角色选主/稳定模型；离线测试显式注入替身。"""
        settings = require_model_configuration()
        if role not in ("master", "info"):
            raise ValueError("未知聊天模型角色")
        return create_chat_model(
            OpenAICredential(
                api_key=settings.api_key,
                base_url=settings.base_url,
            ),
            settings.chat_model_name if role == "master" else settings.stable_model_name,
            role=role,
            stream=stream,
            timeout_seconds=self._settings.model_timeout_seconds,
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

    def _build_info_agent(self, request: RequestContext) -> Agent:
        """每次调用创建无业务工具的只读子 Agent，不保存独立会话状态。"""
        return Agent(
            name=self.INFO_AGENT_NAME,
            system_prompt=INFO_SYSTEM_PROMPT,
            model=self._build_model(stream=False, role="info"),
            state=AgentState(session_id=self._state_session_id(request.session_id, request.user_id)),
            react_config=ReActConfig(
                max_iters=self._settings.info_max_iters,
                interruption_raise_cancelled_error=True,
            ),
            model_config=ModelConfig(max_retries=MODEL_MAX_RETRIES),
        )

    def _build_agent(
        self,
        state: AgentState,
        prepared: PreparedIntentTurn,
        request: RequestContext,
        tool_calls: list[dict[str, str]],
        stream: bool = False,
    ) -> Agent:
        """用识别结果决定本轮可见工具；身份仍从可信请求参数传入。"""
        model = self._build_model(stream=stream)
        intents = "、".join(item.intent.value for item in prepared.decision.result.intents)
        toolkit = None
        if any(item.intent == IntentCategory.GENERAL_INFO for item in prepared.decision.result.intents):
            async def ask_info_agent(question: str) -> str:
                """委派普通公共信息问题给只读 InfoAgent，再返回其回答。"""
                child: Agent | None = None
                status = "failed"
                error_type: str | None = None
                try:
                    child = self._build_info_agent(request)
                    async with asyncio.timeout(self._settings.info_tool_timeout_seconds) as deadline:
                        reply = await child.reply(UserMsg(name=self.COORDINATOR_AGENT_NAME, content=question))
                    if deadline.expired():
                        raise TimeoutError("InfoAgent 工具调用超时")
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise asyncio.CancelledError
                    if reply.finished_reason != ReplyFinishedReason.COMPLETED:
                        raise RuntimeError(f"InfoAgent finished with {reply.finished_reason}")
                    answer = reply.get_text_content() or ""
                    if not answer.strip():
                        raise ValueError("信息子 Agent 没有返回可用回答")
                    status = "completed"
                    return answer
                except BaseException as exc:
                    error_type = type(exc).__name__
                    raise
                finally:
                    record = {
                        "tool_name": self.INFO_TOOL_NAME,
                        "agent_name": self.INFO_AGENT_NAME,
                        "status": status,
                        "trace_id": request.trace_id,
                    }
                    if error_type:
                        record["error_type"] = error_type
                    tool_calls.append(record)
                    await self._close_agent_model(child)

            toolkit = Toolkit(tools=[FunctionTool(
                ask_info_agent,
                name=self.INFO_TOOL_NAME,
                is_read_only=True,
                is_concurrency_safe=False,
                permission=PermissionDecision(
                    behavior=PermissionBehavior.ALLOW,
                    message="允许只读公共信息查询",
                ),
            )])
        return Agent(
            name=self.COORDINATOR_AGENT_NAME,
            system_prompt=master_system_prompt(intents, info_tool_enabled=toolkit is not None),
            model=model,
            toolkit=toolkit,
            state=state,
            react_config=ReActConfig(
                max_iters=self._settings.master_max_iters,
                interruption_raise_cancelled_error=True,
            ),
            model_config=ModelConfig(max_retries=MODEL_MAX_RETRIES),
        )

    def _save_user_turn(self, session_id: str, user_id: str, message: str) -> RequestContext:
        """保存已鉴权消息后创建可信上下文，供两条入口共用。"""
        try:
            QueryInput(question=message)
        except ValidationError:
            raise HTTPException(422, "消息不能为空") from None
        request_id = self._history_service.save_user_message(session_id, user_id, message)
        return RequestContext(user_id=user_id, session_id=session_id, request_id=request_id)

    def _active_agent_for_turn(self, request: RequestContext, message: str) -> str | None:
        """只接受已注册名称与完整续跑词；无记录或无效记录进入完整流水线。"""
        if self._active_continuation is None:
            return None
        try:
            active_name = self._active_continuation.get_active_agent(request)
            entry = choose_turn_entry(
                active_name, message, self._active_continuation.available_agents,
            )
        except Exception as exc:
            raise HTTPException(503, f"活跃 Agent 状态读取失败（{type(exc).__name__}）") from None
        return active_name if entry is TurnEntry.CONTINUE_ACTIVE else None

    async def _continue_active_turn(
        self, request: RequestContext, agent_name: str, message: str,
    ) -> tuple[str, str]:
        """把可信会话参数交给已注册活跃 Agent，统一保存其真实回复。"""
        try:
            text = await self._active_continuation.continue_turn(
                request, agent_name, QueryInput(question=message),
            )
            if not isinstance(text, str) or not text.strip():
                raise ValueError("活跃 Agent 没有返回可保存的回复")
        except Exception as exc:
            raise HTTPException(503, f"活跃 Agent 续跑失败（{type(exc).__name__}）") from None
        msg_id = self._history_service.save_assistant_message(
            conversation_id=request.session_id, user_id=request.user_id, content=text,
            agent_name=agent_name,
            extra={
                "turn_entry": TurnEntry.CONTINUE_ACTIVE.value,
                "active_agent": agent_name,
                "request_id": request.request_id,
                "trace_id": request.trace_id,
            },
        )
        return text, msg_id

    async def _prepare_turn(self, request: RequestContext) -> PreparedIntentTurn:
        """消息已保存时执行 017；模型或索引故障不能继续触发 Agent。"""
        try:
            async with self._pipeline_factory(self._history_service) as pipeline:
                prepared = await pipeline.prepare(request)
                logger.info("chat intent prepared", extra={"trace_id": current_trace_id()})
                return prepared
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, f"意图处理失败（{type(exc).__name__}）") from None

    def _pipeline_extra(
        self, prepared: PreparedIntentTurn, request: RequestContext,
        tool_calls: list[dict[str, str]] | None = None,
    ) -> dict:
        """保存可调试层级与分支，不保存密钥、原始模型响应或可信身份。"""
        decision = prepared.decision
        extra = {"intent_pipeline": {
            "request_id": request.request_id,
            "trace_id": request.trace_id,
            "dispatch_target": self.COORDINATOR_AGENT_NAME if decision is not None else None,
            "branch": prepared.branch,
            "fast_layers": [layer.value for layer in prepared.fast_decision.attempted_layers],
            "attempted_layers": [layer.value for layer in decision.attempted_layers] if decision else [],
            "hit_layer": decision.hit_layer.value if decision and decision.hit_layer else None,
            "intents": [item.intent.value for item in decision.result.intents] if decision and decision.result else [],
        }}
        if tool_calls is not None:
            extra["tool_calls"] = tool_calls
        return extra

    @staticmethod
    def _completion_error(
        tool_calls: list[dict[str, str]], reason: ReplyFinishedReason | None,
    ) -> str | None:
        """不把工具失败、中断或轮次耗尽保存为成功回复。"""
        failed = next((call for call in tool_calls if call["status"] == "failed"), None)
        if failed:
            return f"信息子 Agent 调用失败（{failed.get('error_type', 'ToolError')}）"
        if reason == ReplyFinishedReason.EXCEED_MAX_ITERS:
            return "主 Agent 达到最大推理轮次"
        if reason == ReplyFinishedReason.INTERRUPTED:
            return "对话已中断"
        if reason == ReplyFinishedReason.ERROR:
            return "主 Agent 未能完成回复"
        return None

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
        request = self._save_user_turn(session_id, user_id, message)
        with trace_scope(request):
            logger.info("chat turn started", extra={"trace_id": current_trace_id()})
            active_name = self._active_agent_for_turn(request, message)
            if active_name is not None:
                return await self._continue_active_turn(request, active_name, message)
            prepared = await self._prepare_turn(request)
            if prepared.branch == "needs_context":
                text = self._clarification(prepared)
                msg_id = self._history_service.save_assistant_message(
                    conversation_id=request.session_id, user_id=request.user_id, content=text,
                    agent_name=self.COORDINATOR_AGENT_NAME, extra=self._pipeline_extra(prepared, request),
                )
                return text, msg_id

            # 步骤 2：在 L2 读取历史 AgentState（若为新会话则初始化全新状态）
            state_session_id = self._state_session_id(request.session_id, request.user_id)
            agent: Agent | None = None
            tool_calls: list[dict[str, str]] = []
            try:
                agent_state = self._session_store.load_agent_state(
                    state_session_id, agent_name=self.COORDINATOR_AGENT_NAME,
                )
                if agent_state is None:
                    agent_state = AgentState(session_id=request.session_id)

                # 步骤 3：运行 AgentScope Agent 执行推理
                agent = self._build_agent(
                    state=agent_state, prepared=prepared, request=request,
                    tool_calls=tool_calls, stream=False,
                )
                reply_msg = await agent.reply(UserMsg(name="user", content=prepared.effective_question.question))
            except Exception as exc:
                raise HTTPException(503, f"对话生成失败（{type(exc).__name__}）") from None
            finally:
                await self._close_agent_model(agent)
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError
            error = self._completion_error(
                tool_calls, getattr(reply_msg, "finished_reason", None),
            )
            if error:
                raise HTTPException(503, error)
            reply_text = reply_msg.get_text_content() or ""

            # 先保存业务可见回复；失败时不得写入一份看不到的 AgentState。
            msg_id = self._history_service.save_assistant_message(
                conversation_id=request.session_id,
                user_id=request.user_id,
                content=reply_text,
                agent_name=self.COORDINATOR_AGENT_NAME,
                extra=self._pipeline_extra(prepared, request, tool_calls),
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
        request = self._save_user_turn(session_id, user_id, message)
        with trace_scope(request):
            logger.info("chat stream prepared", extra={"trace_id": current_trace_id()})
            active_name = self._active_agent_for_turn(request, message)
            if active_name is not None:
                text, msg_id = await self._continue_active_turn(
                    request, active_name, message,
                )

                async def continuation_events() -> AsyncGenerator[str, None]:
                    data_lines = "\n".join(f"data: {line}" for line in text.split("\n"))
                    yield f"event: message\n{data_lines}\n\n"
                    yield f"event: message_id\ndata: {msg_id}\n\n"

                return self._traced_stream(request, continuation_events())
            prepared = await self._prepare_turn(request)
            if prepared.branch == "needs_context":
                text = self._clarification(prepared)
                msg_id = self._history_service.save_assistant_message(
                    conversation_id=request.session_id, user_id=request.user_id, content=text,
                    agent_name=self.COORDINATOR_AGENT_NAME, extra=self._pipeline_extra(prepared, request),
                )

                async def clarification_events() -> AsyncGenerator[str, None]:
                    lines = text.split("\n")
                    data_lines = "\n".join(f"data: {line}" for line in lines)
                    yield f"event: message\n{data_lines}\n\n"
                    yield f"event: message_id\ndata: {msg_id}\n\n"

                return self._traced_stream(request, clarification_events())
            return self._traced_stream(request, self._stream_prepared_turn(request, prepared))

    @staticmethod
    async def _traced_stream(
        request: RequestContext, events: AsyncGenerator[str, None],
    ) -> AsyncGenerator[str, None]:
        """每次推进 SSE 生成器时绑定追踪号，交出事件前恢复消费任务的上下文。"""
        try:
            while True:
                with trace_scope(request):
                    try:
                        event = await anext(events)
                    except StopAsyncIteration:
                        return
                yield event
        finally:
            with trace_scope(request):
                await events.aclose()

    async def _stream_prepared_turn(
        self, request: RequestContext, prepared: PreparedIntentTurn,
    ) -> AsyncGenerator[str, None]:
        """只在预处理成功后执行 Agent，保留 005 的 message/message_id 事件。"""
        logger.info("chat stream started", extra={"trace_id": current_trace_id()})

        # 步骤 2：在 L2 读取历史 AgentState（若为新会话则初始化全新状态）
        state_session_id = self._state_session_id(request.session_id, request.user_id)
        accumulated_chunks: list[str] = []
        agent: Agent | None = None
        tool_calls: list[dict[str, str]] = []
        final_reason: ReplyFinishedReason | None = None

        try:
            agent_state = self._session_store.load_agent_state(
                state_session_id, agent_name=self.COORDINATOR_AGENT_NAME,
            )
            if agent_state is None:
                agent_state = AgentState(session_id=request.session_id)

            # 步骤 3：流式运行 AgentScope Agent 并逐块产出
            agent = self._build_agent(
                state=agent_state, prepared=prepared, request=request,
                tool_calls=tool_calls, stream=True,
            )
            async for event in agent.reply_stream(
                UserMsg(name="user", content=prepared.effective_question.question),
                yield_final_msg=True,
            ):
                if isinstance(event, TextBlockDeltaEvent) and event.delta:
                    accumulated_chunks.append(event.delta)
                    lines = event.delta.split("\n")
                    data_lines = "\n".join(f"data: {line}" for line in lines)
                    yield f"event: message\n{data_lines}\n\n"
                elif isinstance(event, Msg):
                    final_reason = event.finished_reason
        except Exception as exc:
            yield f"event: error\ndata: 对话生成失败（{type(exc).__name__}）\n\n"
            return
        finally:
            await self._close_agent_model(agent)

        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError

        error = self._completion_error(tool_calls, final_reason)
        if error:
            yield f"event: error\ndata: {error}\n\n"
            return

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
                conversation_id=request.session_id,
                user_id=request.user_id,
                content=full_reply_text,
                agent_name=self.COORDINATOR_AGENT_NAME,
                extra=self._pipeline_extra(prepared, request, tool_calls),
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
