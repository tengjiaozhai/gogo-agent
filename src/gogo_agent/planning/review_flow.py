"""行程方案审核的唯一权威实现：审批单校验、审核执行、结论摘要。

`review_plan` 工具与预订准备阶段的自动审核端口都调用本模块，不得各自实现一份。
"""

from collections.abc import Callable, Sequence
from typing import Any

from gogo_agent.travel.dependencies import get_travel_service
from gogo_agent.travel.models import TravelOrderStatus
from gogo_agent.travel.service import TravelBusinessService

from .models import FinalReview, PlanConditions, ReviewVerdict
from .review import ApprovedTrip, SubjectiveAssessor
from .service import PlanError, PlanService


def check_approved_order(
    travel: TravelBusinessService, user_id: str, order_id: str, conditions: PlanConditions,
):
    """差旅单必须已审批且与计划城市、日期一致；否则招 PlanError。"""
    order = travel.get_order(user_id, order_id)
    if order.status is not TravelOrderStatus.APPROVED:
        raise PlanError("APPROVAL_NOT_APPROVED", "差旅单尚未审批通过，不能作为预订依据")
    if (order.departure_city != conditions.origin or order.destination != conditions.destination
            or order.departure_date != conditions.departure_date or order.return_date != conditions.return_date):
        raise PlanError("ORDER_PLAN_MISMATCH", "差旅单城市或日期与计划不一致")
    return order


async def review_proposal(
    service: PlanService,
    user_id: str,
    plan_id: str,
    *,
    proposal_tag: str,
    assessors: Sequence[SubjectiveAssessor],
    approved: ApprovedTrip | None = None,
    travel_provider: Callable[[], TravelBusinessService] = get_travel_service,
) -> FinalReview:
    """对同一代表方案执行审核。计划关联差旅单时先校验审批，再进入评估模型，所以未审批不产生模型调用。"""
    plan = service.get(user_id, plan_id)
    current_approval = approved
    if plan.order_id:
        order = check_approved_order(travel_provider(), user_id, plan.order_id, plan.conditions)
        current_approval = ApprovedTrip(origin=order.departure_city, destination=order.destination)
    return await service.review(
        user_id, plan_id, proposal_tag=proposal_tag, assessors=assessors, approved=current_approval,
    )


def review_outcome(final: FinalReview) -> dict[str, Any]:
    """审核结论的对外摘要：工具回执与预订阶段共用，保证阻塞项与警告的定义一致。

    warnings 必须同时包含客观层与仲裁层：主观评估的体验与韧性警告只在仲裁层出现，
    只取客观层会导致结论为 warning 但列表为空。保持客观层在前的原有顺序，并去重。
    """
    warnings = [i.evidence for i in final.objective.issues if i.severity == ReviewVerdict.WARNING]
    warnings += [i.evidence for i in final.arbitration.issues if i.severity == ReviewVerdict.WARNING]
    return {
        "verdict": final.verdict.value,
        "needs_rework": final.needs_rework,
        "blocking_issues": [i.evidence for i in final.arbitration.issues if i.severity == ReviewVerdict.FAIL],
        "warnings": list(dict.fromkeys(warnings)),
    }
