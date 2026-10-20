"""065 预订 Agent 装配：只接收门禁与只读工具，不包含下单、取消或审核修改能力。"""

from __future__ import annotations

from agentscope.agent import Agent, ModelConfig, ReActConfig
from agentscope.model import ChatModelBase
from agentscope.state import AgentState
from agentscope.tool import Toolkit

BOOKING_AGENT_NAME = "BookingAgent"
BOOKING_SYSTEM_PROMPT = (
    "你是 GoGo 的预订智能体。只处理已审核通过且差旅单已审批通过的方案。"
    "执行预订前必须先调用门禁工具并把摘要交给用户确认；你不能自行下单或取消，"
    "也不能修改审核结论或差旅单审批状态。供应商结果未知时只能说明未确认，不得宣称已预订成功。"
)


def build_booking_agent(*, model: ChatModelBase, toolkit: Toolkit, state: AgentState) -> Agent:
    """组装预订 Agent；调用方负责按认证用户与会话构造 toolkit 与 state。"""
    return Agent(
        name=BOOKING_AGENT_NAME,
        system_prompt=BOOKING_SYSTEM_PROMPT,
        model=model,
        toolkit=toolkit,
        state=state,
        react_config=ReActConfig(max_iters=8, interruption_raise_cancelled_error=True),
        model_config=ModelConfig(max_retries=0),
    )
