"""065 预订工具：默认门禁与本人查询；仅在本轮可信网页确认后开放限定对象的模拟执行，不提供取消。

所有创建订单仍只经已认证入口调用 `BookingService.book(confirmed=True)`；模型无法传入 userId，
也没有修改审核结论或差旅单审批状态的工具。
"""

from __future__ import annotations

import json
from typing import Any, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from .chat import BookingChatTurn

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, Toolkit

from gogo_agent.planning.review_flow import review_outcome
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


class ReviewPort(Protocol):
    """预订准备阶段的自动审核端口：由装配层注入，预订模块不直接依赖规划模块。

    返回的摘要字典至少包含 verdict、blocking_issues、warnings。
    """

    async def review(self, user_id: str, plan_id: str, proposal_tag: str) -> dict[str, Any]:
        ...


def _review_denial(outcome: dict[str, Any]) -> str:
    """把审核阻塞项与警告压成一条可读的拒绝说明，供 reply_text 拼接。"""
    parts = [f"审核结论不是通过（{outcome['verdict']}）"]
    if outcome["blocking_issues"]:
        parts.append("阻塞项：" + "、".join(outcome["blocking_issues"]))
    if outcome["warnings"]:
        parts.append("警告：" + "、".join(outcome["warnings"]))
    return "；".join(parts)


def build_booking_toolkit(
    service: BookingService, request: RequestContext, *,
    turn: BookingChatTurn | None = None, reviewer: ReviewPort | None = None,
) -> Toolkit:
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

    async def prepare_booking(plan_id: str, proposal_tag: str, leg: str) -> str:
        """检查审核与差旅审批是否满足预订条件，返回待用户确认的摘要；本工具不会下单。

        plan_id 必须是 list_booking_plans 或 resolve_plan 返回的标识，不是 to_ 开头的差旅单号。
        计划未审核时自动对同一代表方案审核；结论为 fail 时不出卡，warning 只作为建议随卡片展示。
        """
        try:
            return await _prepare_with_review(plan_id, proposal_tag, leg)
        except BookingError as exc:
            stored = _stored_review(plan_id) if exc.code == "REVIEW_NOT_PASSED" else None
            if turn is not None:
                # 已有审核为 fail 时，阻塞项与警告也必须进入 denials，与自动审核分支一致。
                turn.denials.append(str(exc) if stored is None else f"{exc}；{_review_denial(stored)}")
            return _fail(exc.code, str(exc), data=stored)

    def _stored_review(plan_id: str) -> dict[str, Any] | None:
        """读取计划已保存的审核结论与警告；只读，不重新审核，不产生模型调用。"""
        try:
            plan = service.plans.get(user_id, plan_id)
        except PlanError:
            return None
        if plan.last_review is None:
            return None
        return review_outcome(plan.last_review)

    async def _prepare_with_review(plan_id: str, proposal_tag: str, leg: str) -> str:
        """门禁报 REVIEW_REQUIRED 且有端口时先审核；只有 fail 阻止出卡，warning 放行并作为建议保留，只重试一次。"""
        try:
            return _prepare_once(plan_id, proposal_tag, leg)
        except BookingError as exc:
            if exc.code != "REVIEW_REQUIRED" or reviewer is None:
                raise
        outcome = await reviewer.review(user_id, plan_id, proposal_tag)
        if outcome["verdict"] == "fail":
            # 审核 fail 走的是返回值而非异常，必须显式写入 denials，否则用户回复只会是固定的「本轮未下单」。
            if turn is not None:
                turn.denials.append(_review_denial(outcome))
            return _fail(
                "REVIEW_NOT_PASSED",
                "行程方案审核未通过，未生成预订确认卡",
                data={"verdict": outcome["verdict"], "blocking_issues": outcome["blocking_issues"],
                      "warnings": outcome["warnings"]},
            )
        return _prepare_once(plan_id, proposal_tag, leg)

    def _prepare_once(plan_id: str, proposal_tag: str, leg: str) -> str:
        """一次门禁检查并返回摘要；通过后把审核警告记为建议，失败以 BookingError 抬出，交由调用方处理。"""
        if turn is not None:
            prepared = turn.prepare(plan_id, proposal_tag, leg)
            stored = _stored_review(plan_id)
            if stored is not None:
                for warning in stored["warnings"]:
                    if warning not in turn.advisories:
                        turn.advisories.append(warning)
            return json.dumps(prepared, ensure_ascii=False)
        summary = service.check_ready(user_id, plan_id, proposal_tag=proposal_tag, leg=leg)
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

