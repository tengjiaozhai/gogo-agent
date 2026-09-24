"""One-shot terminal agent for the 001 startup exercise."""

import argparse
import asyncio
import os
import sys
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlparse

from agentscope.agent import Agent
from agentscope.credential import DeepSeekCredential
from agentscope.event import (
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import Msg, ToolResultState, UserMsg
from agentscope.model import DeepSeekChatModel
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, Toolkit
from dotenv import load_dotenv

from gogo_agent.tools import get_current_date


def build_agent() -> Agent:
    """Create an Agent whose only callable tool reads the local date."""
    required = ("GOGO_MODEL_API_KEY", "GOGO_MODEL_NAME", "GOGO_MODEL_BASE_URL")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise ValueError("缺少模型配置：" + ", ".join(missing) + "；参见 .env.example")

    base_url = os.environ["GOGO_MODEL_BASE_URL"].strip().rstrip("/")
    parsed = urlparse(base_url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("GOGO_MODEL_BASE_URL 应是网关根地址，程序会追加 /v1")

    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=os.environ["GOGO_MODEL_API_KEY"].strip(),
            base_url=f"{base_url}/v1",
        ),
        model=os.environ["GOGO_MODEL_NAME"].strip(),
        stream=False,
    )
    tool = FunctionTool(
        get_current_date,
        is_read_only=True,
        permission=PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Read the local date",
        ),
    )
    return Agent(
        name="GoGo",
        system_prompt=(
            "你是简洁的中文助手。回答今天的日期时，必须调用 "
            "get_current_date 工具，并根据工具结果回答。"
        ),
        model=model,
        toolkit=Toolkit(tools=[tool]),
    )


async def ask(prompt: str) -> tuple[str, bool]:
    agent = build_agent()
    answer = ""
    tool_name = None
    tool_used = False
    async for event in agent.reply_stream(
        UserMsg(name="user", content=prompt),
        yield_final_msg=True,
    ):
        if isinstance(event, ToolResultStartEvent):
            tool_name = event.tool_call_name
        elif isinstance(event, ToolResultTextDeltaEvent) and tool_name == "get_current_date":
            print(f"工具结果：{event.delta}")
        elif isinstance(event, ToolResultEndEvent) and tool_name == "get_current_date":
            tool_used = tool_used or event.state == ToolResultState.SUCCESS
            tool_name = None
        elif isinstance(event, Msg):
            answer = event.get_text_content() or ""
    return answer, tool_used


def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    parser = argparse.ArgumentParser(description="GoGo Agent 001：查询当前日期的最小终端 Agent")
    parser.add_argument("prompt", nargs="?", default="今天是几月几日？请调用日期工具后回答。")
    parser.add_argument("--health", action="store_true", help="只检查本地安装，不调用模型")
    args = parser.parse_args()

    print(f"AgentScope {version('agentscope')}")
    if args.health:
        print("本地启动检查通过")
        return 0
    try:
        answer, tool_used = asyncio.run(ask(args.prompt))
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    if not answer:
        print("模型未返回文本回答", file=sys.stderr)
        return 1
    print(f"模型回答：{answer}")
    if not tool_used:
        print("模型未成功调用 get_current_date", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
