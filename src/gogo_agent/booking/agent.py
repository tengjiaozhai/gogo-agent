"""065 预订 Agent 装配：默认查询与门禁，可信网页确认后执行模拟预订，不提供取消或审核修改。"""

from __future__ import annotations

from agentscope.agent import Agent, ModelConfig, ReActConfig
from agentscope.model import ChatModelBase
from agentscope.middleware import MiddlewareBase
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from gogo_agent.context.input import PROMPT_VERSION
from gogo_agent.planning.scoring import TAG_ORDER

BOOKING_AGENT_NAME = "BookingAgent"
BOOKING_SYSTEM_PROMPT = f"[提示词版本 {PROMPT_VERSION}]\n" + (
    "你是 GoGo 的预订智能体。只处理已审核通过且差旅单已审批通过的方案。"
    "核实未知预订用 check_booking_result，传入已有 booking_id；这是只读对账，不要重新生成确认或创建新订单。"
    "查询已有预订先用 query_bookings 列出本人的记录，再按 booking_id 查询详情，不要重新创建预订。"
    "用户未给计划标识时先调用 list_booking_plans，明确当前会话的计划与方案；多个可选方案时询问用户选择。"
    "以 to_ 开头的是差旅单号，不是计划标识：必须先调用 resolve_plan 换算成 plan_id，"
    "绝不能把差旅单号当 plan_id 传给 prepare_booking。"
    "用户给的编号查不到时，先用 resolve_plan 或 list_booking_plans 找到唯一匹配的计划并继续；"
    "只有匹配到多个或完全查不到时才请用户确认，不要直接回复「计划不存在」。"
    f"代表方案只能用标签指定，可用标签为：{'、'.join(TAG_ORDER)}。"
    "用户给出的组合编号（如 F8、H7）、班次或酒店编号，以及不在可用标签中的名称，都不能对应到某个标签；"
    "门禁返回 PROPOSAL_NOT_FOUND 时，把其中列出的可用标签交给用户选择，不得自行替换或猜测标签。"
    "每个去程、返程和酒店独立生成确认卡，用户只授权卡片列出的项目。"
    "执行预订前必须先调用门禁工具并把摘要交给用户确认；没有本轮服务端网页确认时不能自行下单或取消，"
    "网页确认通过时会开放 execute_confirmed_booking，请调用它执行已确认项，不要重新索取确认或更换对象。"
    "也不能修改审核结论或差旅单审批状态。供应商结果未知时只能说明未确认，不得宣称已预订成功。"
)


def build_booking_agent(*, model: ChatModelBase, toolkit: Toolkit, state: AgentState,
                        middlewares: list[MiddlewareBase] | None = None) -> Agent:
    """组装预订 Agent；调用方负责按认证用户与会话构造 toolkit 与 state。"""
    return Agent(
        name=BOOKING_AGENT_NAME,
        system_prompt=BOOKING_SYSTEM_PROMPT,
        model=model,
        toolkit=toolkit,
        state=state,
        middlewares=middlewares,
        react_config=ReActConfig(max_iters=8, interruption_raise_cancelled_error=True),
        model_config=ModelConfig(max_retries=0),
    )
