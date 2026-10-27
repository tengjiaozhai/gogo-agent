"""065 预订工具：默认门禁与本人查询；仅在本轮可信网页确认后开放限定对象的模拟执行，不提供取消。

所有创建订单仍只经已认证入口调用 `BookingService.book(confirmed=True)`；模型无法传入 userId，
也没有修改审核结论或差旅单审批状态的工具。
"""

from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .chat import BookingChatTurn

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, Toolkit

from gogo_agent.planning.service import PlanError
from gogo_agent.request_context import RequestContext
from gogo_agent.travel.models import BookingStatus
from gogo_agent.travel.service import BusinessError

from .service import BookingError, BookingService


def _fail(code: str, message: str, data: dict[str, Any] | None = None) -> str:
    body: dict[str, Any] = {"success": False, "errorCode": code, "message": message}
    if data is not None:
        body["data"] = data
    return json.dumps(body, ensure_ascii=False)


def _plan_summary(plan) -> dict[str, Any]:
    """计划对模型与用户的可见摘要；只含引用与审核状态，不含金额或候选全文。"""
    review = plan.last_review
    return {
        "plan_id": plan.plan_id, "order_id": plan.order_id,
        "origin": plan.conditions.origin, "destination": plan.conditions.destination,
        "departure_date": plan.conditions.departure_date.isoformat(),
        "return_date": plan.conditions.return_date.isoformat(),
        "review_current": plan.review_is_current(),
        "review_verdict": review.verdict.value if review else None,
        "reviewed_proposal_tag": review.proposal_tag if review else None,
    }


def build_booking_toolkit(service: BookingService, request: RequestContext, *, turn: BookingChatTurn | None = None) -> Toolkit:
    """注册预订 Agent 的工具；身份来自服务端 RequestContext，模型参数中的用户标识无效。"""
    user_id = request.user_id

    def resolve_plan(reference: str = "") -> str:
        """把用户给出的编号统一解析为本人的计划标识；以 to_ 开头的是差旅单号，不是计划标识。

        用户通常只记得差旅单号，必须先经本工具换算出 plan_id 再调用 prepare_booking。
        """
        text = (reference or "").strip()
        if not text:
            return _fail("NEEDS_INPUT", "请提供差旅单号或计划标识；也可先调用 list_booking_plans 查看当前会话的计划")
        plans = []
        if text.startswith("to_"):
            plans = service.plans.list_for_order(user_id, text)
            if not plans:
                return _fail(
                    "ORDER_HAS_NO_PLAN",
                    f"差旅单 {text} 还没有关联任何计划：请先创建计划并绑定该差旅单，再重新计算与审核",
                )
            source = "order_id"
        else:
            try:
                plans = [service.plans.get(user_id, text)]
            except BookingError as exc:
                return _fail(exc.code, str(exc))
            except PlanError as exc:
                return _fail(exc.code, str(exc))
            source = "plan_id"
        return json.dumps({"success": True, "data": {
            "resolved_by": source, "reference": text,
            "plans": [_plan_summary(plan) for plan in plans],
            "next": "选定 plan_id 后调用 prepare_booking；审核非当前版或未通过时必须重新审核",
        }}, ensure_ascii=False)

    def prepare_booking(plan_id: str, proposal_tag: str, leg: str) -> str:
        """检查审核与差旅审批是否满足预订条件，返回待用户确认的摘要；本工具不会下单。

        plan_id 必须是 list_booking_plans 或 resolve_plan 返回的标识，不是 to_ 开头的差旅单号。
        """
        try:
            if turn is not None:
                return json.dumps(turn.prepare(plan_id, proposal_tag, leg), ensure_ascii=False)
            summary = service.check_ready(user_id, plan_id, proposal_tag=proposal_tag, leg=leg)
        except BookingError as exc:
            if turn is not None:
                turn.denials.append(str(exc))
            return _fail(exc.code, str(exc))
        return _fail(
            "CONFIRMATION_REQUIRED",
            "请用户在已认证入口确认本次预订摘要；本对话未下单",
            data=summary,
        )

    def list_booking_plans() -> str:
        """列出本人在当前聊天创建的计划引用、审批关联和审核状态，用于明确用户说的这个方案。"""
        plans = service.plans.list_for_session(user_id, request.session_id)
        return json.dumps({"success": True, "data": {"plans": [_plan_summary(plan) for plan in plans]}},
                          ensure_ascii=False)

    def query_bookings(status: str = "") -> str:
        """列出本人的已有预订记录；需要详情时再用返回的 booking_id 查询，不重新下单。"""
        try:
            parsed = BookingStatus(status) if status else None
        except ValueError:
            return _fail("INVALID_PARAM", "预订状态不合法")
        rows = service.travel.list_bookings(user_id, status=parsed)
        if turn is not None:
            turn.query_results.extend(row.model_dump(mode="json", exclude={"user_id"}) for row in rows)
            if not rows:
                turn.denials.append("本人暂无符合条件的预订记录")
        return json.dumps({"success": True, "data": {"bookings": [row.model_dump(mode="json", exclude={"user_id"}) for row in rows]}}, ensure_ascii=False)

    def query_booking(booking_id: str) -> str:
        """读取本人预订记录的当前本地状态与供应商订单号；不查询也不修改供应商。"""
        try:
            view = service.travel.get_booking(user_id, booking_id)
        except BusinessError as exc:
            if turn is not None:
                turn.denials.append(str(exc))
            return _fail(exc.code, str(exc))
        if turn is not None:
            turn.query_results.append(view.model_dump(mode="json", exclude={"user_id"}))
        return json.dumps({
            "success": True, "message": "查询成功",
            "data": view.model_dump(mode="json", exclude={"user_id"}),
        }, ensure_ascii=False)

    def check_booking_result(booking_id: str) -> str:
        """核实本人已有模拟预订的供应商结果，只读对账；不重选方案、不重新下单。"""
        try:
            outcome = service.check_result(user_id, booking_id)
            view = service.travel.get_booking(user_id, booking_id).model_dump(mode="json", exclude={"user_id"})
            view.update(resolution=outcome.resolution, external_order_no=outcome.external_order_no)
            if turn is not None:
                turn.query_results.append(view)
            return json.dumps({"success": True, "data": view, "message": outcome.message}, ensure_ascii=False)
        except (BookingError, BusinessError) as exc:
            if turn is not None:
                turn.denials.append(str(exc))
            return _fail(exc.code, str(exc))

    def execute_confirmed_booking() -> str:
        """执行本轮网页已经明确确认的唯一预订项；无需也不接受计划、金额或确认参数。"""
        assert turn is not None
        return json.dumps(turn.execute(), ensure_ascii=False)

    allowed = PermissionDecision(behavior=PermissionBehavior.ALLOW, message="允许本人预订查询与门禁；模拟执行仅限本轮已验证的网页确认")
    functions = [resolve_plan, prepare_booking, query_booking, query_bookings, list_booking_plans, check_booking_result]
    if turn is not None and turn.confirmed is not None:
        functions.append(execute_confirmed_booking)
    return Toolkit(tools=[
        FunctionTool(function, name=function.__name__,
                     is_read_only=function in (resolve_plan, prepare_booking, query_booking, query_bookings,
                                               list_booking_plans, check_booking_result),
                     is_concurrency_safe=False, permission=allowed)
        for function in functions
    ])

