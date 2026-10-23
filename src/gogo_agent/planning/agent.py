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
    "条件不足时先请求补充，不要臆造城市或日期。"
    "调用 compute_plan 时按对象结构传参，不要传 JSON 字符串，也不要自创字段名："
    "scores 是候选偏好分，形如 "
    "{\"transport_scores\": {\"F1\": {\"score\": 85, \"basis\": [\"时间最早\"]}},"
    "\"hotel_scores\": {\"H1\": {\"score\": 70, \"basis\": [\"离会场近\"]}}}；"
    "policy 只接受数值或枚举字段，形如 "
    "{\"flight_class\": \"经济舱\", \"train_seat_class\": \"二等座\", \"hotel_star_limit\": 3}，"
    "不要把自然语言政策原文放进 policy；"
    "weights 是四个维度权重，形如 "
    "{\"time\": 0.5, \"price\": 0.3, \"preference\": 0.1, \"experience\": 0.1}，"
    "用户说「时间优先、价格其次」这类诉求应折算成 weights，而不是写进 policy。"
    "工具报错时会用 field 指出不合法的是哪个参数，请按提示改正后重试。"
    "任何工具返回 PLAN_NOT_FOUND 都表示会话里残留的 plan_id 已失效：不要重复用它，"
    "也不要向用户复述这个错误，直接根据已知出行条件重新调用 create_plan 取得新 plan_id，"
    "再依次搜索候选、计算方案与审核；条件已不全时先向用户确认。"
    "一个 plan_id 只对应一次规划，不要假设旧 plan_id 仍然可用。"
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
