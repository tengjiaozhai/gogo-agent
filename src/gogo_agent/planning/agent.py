"""043 行程规划智能体装配：只接收规划工具，不包含差旅单写入或供应商下单能力。"""

from __future__ import annotations

from agentscope.agent import Agent, ModelConfig, ReActConfig
from agentscope.model import ChatModelBase
from agentscope.middleware import MiddlewareBase
from agentscope.state import AgentState
from agentscope.tool import Toolkit

PLAN_AGENT_NAME = "ItineraryPlanAgent"
PLAN_SYSTEM_PROMPT = (
    "你是 GoGo 的行程规划智能体，负责收集出行条件、搜索候选、计算代表方案并请求审核。"
    "方案金额、时间和政策数字只能来自规划工具的返回结果，不得自行估算。"
    "偏好判断由你通过 scores_json 给出，工具不解读偏好文本。"
    "条件不足时先请求补充，不要臆造城市或日期。"
    "你没有提交差旅单、取消或供应商下单的工具，不得声称已经预订或提交；"
    "确认与执行须由用户在已认证入口完成。"
)


def build_plan_agent(*, model: ChatModelBase, toolkit: Toolkit, state: AgentState,
                     middlewares: list[MiddlewareBase] | None = None) -> Agent:
    """组装规划 Agent；调用方负责按认证用户与会话构造 state 与 toolkit。"""
    return Agent(
        name=PLAN_AGENT_NAME,
        system_prompt=PLAN_SYSTEM_PROMPT,
        model=model,
        toolkit=toolkit,
        state=state,
        middlewares=middlewares,
        react_config=ReActConfig(max_iters=10, interruption_raise_cancelled_error=True),
        model_config=ModelConfig(max_retries=0),
    )
