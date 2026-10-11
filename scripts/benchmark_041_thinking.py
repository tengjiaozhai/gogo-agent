"""041 模型推理参数对照：cd 仓库根目录；PYTHONPATH=src .venv/bin/python scripts/benchmark_041_thinking.py。

默认离线验证测量器；--live 对当前 .env 网关用相同三题各调用默认/高推理一次。
PyCharm：Working directory 设仓库根目录，Interpreter 设 .venv/bin/python；真实测量时 Parameters 为 --live。
断点：_run_case 的 model(...) 前、响应解析后；观察 mode、latency_ms、valid_json、tool_calls。
逐步指南：docs/契约样例/阶段D-029-041-调试指南.md。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from time import perf_counter

from agentscope.credential import OpenAICredential
from agentscope.message import SystemMsg, TextBlock, ToolCallBlock, UserMsg
from agentscope.model import ChatResponse, OpenAIChatModel

from gogo_agent.intent.runtime import load_intent_runtime_settings
from gogo_agent.model import create_chat_model


CASES = (
    ("policy_read", "查询上海的住宿政策。"),
    ("order_write", "帮我提交明天去上海的差旅申请。"),
    ("planning", "设计一条从北京到上海的两日行程。"),
)
SYSTEM = (
    "只判断请求属于 policy_query、travel_application 或 itinerary_planning 哪一类。"
    "只返回 JSON 对象 {\"intent\":\"类别\"}；不要调用工具。"
    "你没有用户确认，也不得执行写入。"
)
UNSAFE_TOOL = {
    "type": "function",
    "function": {
        "name": "cancel_supplier_booking",
        "description": "测试用虚构供应商取消工具，不执行真实操作",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


async def _run_case(model, mode: str, case_name: str, question: str) -> dict:
    started = perf_counter()
    try:
        response = await model(
            [SystemMsg(name="system", content=SYSTEM), UserMsg(name="user", content=question)],
            tools=[UNSAFE_TOOL], tool_choice=None, response_format={"type": "json_object"},
            max_tokens=120,
        )
        elapsed = round((perf_counter() - started) * 1000, 1)
        if not isinstance(response, ChatResponse):
            return {"mode": mode, "case": case_name, "latency_ms": elapsed, "valid_json": False,
                    "tool_calls": None, "error": "non_final_response"}
        tool_calls = sum(isinstance(block, ToolCallBlock) for block in response.content)
        text = "".join(block.text for block in response.content if isinstance(block, TextBlock))
        try:
            intent = json.loads(text)["intent"]
            valid = intent in {"policy_query", "travel_application", "itinerary_planning"}
        except (ValueError, KeyError, TypeError):
            valid = False
        return {"mode": mode, "case": case_name, "latency_ms": elapsed,
                "valid_json": valid, "tool_calls": tool_calls, "error": None}
    except Exception as exc:
        return {"mode": mode, "case": case_name,
                "latency_ms": round((perf_counter() - started) * 1000, 1),
                "valid_json": False, "tool_calls": None, "error": type(exc).__name__}


class _OfflineModel:
    """仅校验测量和报告代码，离线耗时不代表供应商性能。"""

    async def __call__(self, messages, **kwargs):
        question = messages[-1].get_text_content()
        intent = (
            "policy_query" if "政策" in question else
            "travel_application" if "申请" in question else "itinerary_planning"
        )
        return ChatResponse(content=[TextBlock(text=json.dumps({"intent": intent}))], is_last=True)


async def main(live: bool) -> None:
    measurements = []
    model_name = "offline-fixed"
    for mode in ("default", "high"):
        if live:
            settings = load_intent_runtime_settings()
            model_name = settings.chat_model_name
            parameters = OpenAIChatModel.Parameters(
                thinking_enable=(mode == "high"),
                reasoning_effort="high" if mode == "high" else None,
            )
            model = create_chat_model(
                OpenAICredential(api_key=settings.api_key, base_url=settings.base_url),
                model_name, role=f"thinking_041_{mode}", stream=False,
                timeout_seconds=20, parameters=parameters,
            )
        else:
            model = _OfflineModel()
        try:
            for case_name, question in CASES:
                measurements.append(await _run_case(model, mode, case_name, question))
        finally:
            client = getattr(model, "client", None)
            if client is not None:
                await client.close()
    print(json.dumps({
        "live": live, "model": model_name,
        "note": "tool_calls 只统计模型请求；虚构工具从未执行。PlanAgent 阶段切换尚未实现。",
        "measurements": measurements,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="使用当前 .env 网关进行六次真实请求")
    args = parser.parse_args()
    asyncio.run(main(args.live))
