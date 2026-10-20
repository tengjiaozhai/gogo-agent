"""把真实 AgentScope 事件转换为沿用 Java 前端协议的执行时间轴。"""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import suppress

from agentscope.event import (
    ReplyStartEvent, ReplyEndEvent, ThinkingBlockStartEvent,
    ThinkingBlockDeltaEvent, ThinkingBlockEndEvent, ToolCallStartEvent,
    ToolCallDeltaEvent, ToolCallEndEvent, ToolResultTextDeltaEvent,
    ToolResultEndEvent,
)
from agentscope.middleware import MiddlewareBase

from gogo_agent.security.masking import SensitiveMasker


AGENT_LABELS = {
    "GoGo": "GoGo 主智能体", "InfoAgent": "信息查询智能体",
    "ItineraryManageAgent": "差旅管理智能体", "ItineraryPlanAgent": "行程规划智能体",
}
CHILD_TOOLS = {
    "info_agent": "InfoAgent", "itinerary_manage_agent": "ItineraryManageAgent",
    "itinerary_plan_agent": "ItineraryPlanAgent",
}


def sse_event(name: str, data: str | dict) -> str:
    """统一编码多行文本和 JSON 事件，避免内容中的换行破坏 SSE。"""
    raw = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else data
    return f"event: {name}\n" + "\n".join(f"data: {line}" for line in raw.split("\n")) + "\n\n"


def mask_trace_text(text: str) -> str:
    """资料 JSON（含 profile_json 内嵌字符串）按字段遮盖姓名拼音，其他沿用既有脱敏。"""
    def scrub(value):
        if isinstance(value, dict):
            return {key: "****" if key.lower() == "name_pinyin" else scrub(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            try:
                return json.dumps(scrub(json.loads(value)), ensure_ascii=False)
            except ValueError:
                pass
        return value

    try:
        text = json.dumps(scrub(json.loads(text)), ensure_ascii=False)
    except ValueError:
        pass
    return SensitiveMasker.mask(text) or ""


class ExecutionTrace(MiddlewareBase):
    """每轮独立的观察器；原样转交 SDK 事件，不改变工具、权限或推理结果。"""

    def __init__(self, *, root_agent: str = "GoGo", streaming: bool = False, intent: dict | None = None):
        self.root_agent = root_agent
        self.timeline: list[dict] = []
        self.intent = intent
        self._items: dict[str, dict] = {}
        self._raw: dict[str, str] = {}
        self._queue: asyncio.Queue[str | None] | None = asyncio.Queue(maxsize=128) if streaming else None

    async def emit(self, name: str, data: str | dict) -> None:
        if self._queue is not None:
            await self._queue.put(sse_event(name, data))

    def _add(self, item: dict) -> dict:
        self.timeline.append(item)
        self._items[item["id"]] = item
        return item

    async def _progress(self, item: dict, event_type: str) -> None:
        await self.emit("progress", {
            "type": event_type, "stepId": item["id"], "agentName": item["agentName"],
            "message": item["title"], **{key: item[key] for key in (
                "status", "arguments", "result", "parentAgentName", "thinkingAvailable", "detail",
            ) if key in item},
        })

    async def on_reply(self, agent, input_kwargs, next_handler):
        """主/子 Agent 的 reply 与 reply_stream 共用一个事件观察点。"""
        try:
            async for event in next_handler(**input_kwargs):
                await self._record(agent, event)
                yield event
        except BaseException:
            # 不展示异常正文（供应商异常可能含凭证）；也不把失败进度画成成功。
            for item in self.timeline:
                if item.get("agentName") == agent.name and item.get("status") == "in-progress":
                    item.update(status="error", result="执行未完成")
                    # 取消时队列可能没有消费者，清理由外层 stream 的 finally 完成。
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        continue
                    await self._progress(item, "tool_done" if item["kind"] == "tool" else "agent_done")
            raise

    async def _record(self, agent, event) -> None:
        name = agent.name
        reply_id = getattr(event, "reply_id", "")
        agent_id = f"agent:{name}:{reply_id}"
        if isinstance(event, ReplyStartEvent):
            item = self._add({
                "kind": "agent", "id": agent_id, "agentName": name,
                "title": AGENT_LABELS.get(name, name), "status": "in-progress",
                "thinkingAvailable": False,
            })
            if name != self.root_agent:
                item["parentAgentName"] = self.root_agent
            elif self.intent:
                schemas = await agent.toolkit.get_tool_schemas()
                tools = [schema["function"]["name"] for schema in schemas]
                item["detail"] = (
                    "识别诉求：" + "、".join(self.intent.get("intents", []))
                    + "；本轮可用工具：" + ("、".join(tools) or "无")
                    + "。实际委派见下方工具调用。"
                )
            await self._progress(item, "agent_start")
        elif isinstance(event, ReplyEndEvent):
            item = self._items[agent_id]
            item["status"] = "done" if event.finished_reason == "completed" else "error"
            await self._progress(item, "agent_done")
        elif isinstance(event, ThinkingBlockStartEvent):
            key = f"thinking:{name}:{reply_id}:{event.block_id}"
            self._raw[key] = ""
        elif isinstance(event, ThinkingBlockDeltaEvent):
            key = f"thinking:{name}:{reply_id}:{event.block_id}"
            self._raw[key] = self._raw.get(key, "") + event.delta
        elif isinstance(event, ThinkingBlockEndEvent):
            key = f"thinking:{name}:{reply_id}:{event.block_id}"
            # 完整块再脱敏，避免凭证跨 delta 分块时被逐片透传。
            text = SensitiveMasker.mask(self._raw.pop(key, "")) or ""
            if text.strip():
                self._items[agent_id]["thinkingAvailable"] = True
                self._add({"kind": "thinking", "id": key, "agentName": name, "text": text})
                await self.emit("thinking", {"agentName": name, "roundStart": "true"})
                await self.emit("thinking", {"agentName": name, "text": text})
        elif isinstance(event, ToolCallStartEvent):
            key = f"tool:{name}:{reply_id}:{event.tool_call_id}"
            child = CHILD_TOOLS.get(event.tool_call_name)
            title = f"{AGENT_LABELS.get(name, name)} → {AGENT_LABELS[child]}" if child else event.tool_call_name
            self._items[key] = {"kind": "tool", "id": key, "agentName": name,
                                "title": title, "status": "in-progress", "arguments": ""}
        elif isinstance(event, ToolCallDeltaEvent):
            key = f"tool:{name}:{reply_id}:{event.tool_call_id}"
            self._raw[key + ":args"] = self._raw.get(key + ":args", "") + event.delta
        elif isinstance(event, ToolCallEndEvent):
            key = f"tool:{name}:{reply_id}:{event.tool_call_id}"
            item = self._items[key]
            item["arguments"] = mask_trace_text(self._raw.pop(key + ":args", ""))
            self.timeline.append(item)
            await self._progress(item, "tool_call")
        elif isinstance(event, ToolResultTextDeltaEvent):
            key = f"tool:{name}:{reply_id}:{event.tool_call_id}"
            self._raw[key] = self._raw.get(key, "") + event.delta
        elif isinstance(event, ToolResultEndEvent):
            key = f"tool:{name}:{reply_id}:{event.tool_call_id}"
            item = self._items.get(key)
            if item is None:
                return
            result = self._raw.pop(key, "")
            failed = event.state != "success"
            waiting = False
            try:
                payload = json.loads(result)
                waiting = (event.state == "success" and isinstance(payload, dict)
                           and payload.get("errorCode") in {"CONFIRMATION_REQUIRED", "NEEDS_INPUT"})
                failed = failed or (isinstance(payload, dict) and payload.get("success") is False)
            except (ValueError, TypeError):
                pass
            item.update(status="pending" if waiting else "error" if failed else "done", result=mask_trace_text(result))
            await self._progress(item, "tool_done")

    async def stream(self, source: AsyncGenerator[str, None]) -> AsyncGenerator[str, None]:
        """合并原有正文流与嵌套子 Agent 事件；断开连接时取消并等待整个生产任务。"""
        if self._queue is None:
            raise ValueError("流式执行需要 streaming=True")

        async def produce():
            try:
                async for chunk in source:
                    await self._queue.put(chunk)
            finally:
                await source.aclose()
                if not asyncio.current_task().cancelling():
                    await self._queue.put(None)

        producer = asyncio.create_task(produce())
        try:
            while (chunk := await self._queue.get()) is not None:
                yield chunk
            await producer
        finally:
            producer.cancel()
            with suppress(asyncio.CancelledError):
                await producer
