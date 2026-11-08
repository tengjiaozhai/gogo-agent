"""规划智能体回复用户时只能使用代表方案标签与真实班次、酒店；不得使用内部编号。"""

import json

from gogo_agent.planning.agent import PLAN_SYSTEM_PROMPT
from gogo_agent.planning.dependencies import get_plan_service
from gogo_agent.planning.simulated import simulated_assessors, simulated_provider
from gogo_agent.planning.tools import build_plan_toolkit
from gogo_agent.request_context import RequestContext

PLAN_TAGS = ("综合最佳", "时间最短", "价格最低", "最符合偏好")


def test_prompt_requires_tags_and_forbids_internal_codes():
    for tag in PLAN_TAGS:
        assert tag in PLAN_SYSTEM_PROMPT
    assert "不得使用" in PLAN_SYSTEM_PROMPT and "编号" in PLAN_SYSTEM_PROMPT


def test_compute_result_exposes_flight_hotel_and_leg_prices_for_the_reply():
    ctx = RequestContext(user_id="u001", session_id="reply-labels", request_id="r")
    toolkit = build_plan_toolkit(
        get_plan_service(), ctx, search_provider=simulated_provider, assessors=simulated_assessors(),
    )

    def call(name: str, **kwargs):
        import asyncio
        tool = asyncio.run(toolkit.get_tool(name))
        return json.loads(asyncio.run(tool.call(**kwargs)).content[0].text)

    created = call("create_plan", origin="北京", destination="上海", departure_date="2026-10-20",
                   return_date="2026-10-22")
    plan_id = created["data"]["plan_id"]
    call("search_candidates", plan_id=plan_id)
    # policy 是对象结构；不传即不做政策检查。传字符串会被拒绝（INVALID_PARAM）。
    computed = call("compute_plan", plan_id=plan_id)
    proposal = computed["data"]["proposals"][0]
    for key in ("outbound", "hotel", "inbound"):
        assert key in proposal, f"方案缺少 {key}，模型无法说出真实班次或酒店"
    assert proposal["outbound"]["code"] and proposal["outbound"]["price"] > 0
    assert proposal["inbound"]["code"] and proposal["inbound"]["price"] > 0
    assert proposal["hotel"]["name"] and proposal["hotel"]["price_per_night"] > 0
    assert proposal["hotel"]["nights"] >= 1
