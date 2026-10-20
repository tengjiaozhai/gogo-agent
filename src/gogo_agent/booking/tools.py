"""065 预订 Agent 的模型可见工具：只做门禁检查与待确认摘要，不执行下单或取消。

真正的下单只能由已认证入口调用 `BookingService.book(confirmed=True)`；模型无法传入 userId，
也没有修改审核结论或差旅单审批状态的工具。
"""

from __future__ import annotations

import json
from typing import Any

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, Toolkit

from gogo_agent.request_context import RequestContext
from gogo_agent.travel.service import BusinessError

from .service import BookingError, BookingService


def _fail(code: str, message: str, data: dict[str, Any] | None = None) -> str:
    body: dict[str, Any] = {"success": False, "errorCode": code, "message": message}
    if data is not None:
        body["data"] = data
    return json.dumps(body, ensure_ascii=False)


def build_booking_toolkit(service: BookingService, request: RequestContext) -> Toolkit:
    """注册预订 Agent 的工具；身份来自服务端 RequestContext，模型参数中的用户标识无效。"""
    user_id = request.user_id

    def prepare_booking(plan_id: str, proposal_tag: str, leg: str) -> str:
        """检查审核与差旅审批是否满足预订条件，返回待用户确认的摘要；本工具不会下单。"""
        try:
            summary = service.check_ready(user_id, plan_id, proposal_tag=proposal_tag, leg=leg)
        except BookingError as exc:
            return _fail(exc.code, str(exc))
        return _fail(
            "CONFIRMATION_REQUIRED",
            "请用户在已认证入口确认本次预订摘要；本对话未下单",
            data=summary,
        )

    def query_booking(booking_id: str) -> str:
        """读取本人预订记录的当前本地状态与供应商订单号；不查询也不修改供应商。"""
        try:
            view = service.travel.get_booking(user_id, booking_id)
        except BusinessError as exc:
            return _fail(exc.code, str(exc))
        return json.dumps({
            "success": True, "message": "查询成功",
            "data": view.model_dump(mode="json", exclude={"user_id"}),
        }, ensure_ascii=False)

    allowed = PermissionDecision(behavior=PermissionBehavior.ALLOW, message="允许门禁检查与只读查询")
    return Toolkit(tools=[
        FunctionTool(function, name=function.__name__, is_read_only=function in (prepare_booking, query_booking),
                     is_concurrency_safe=False, permission=allowed)
        for function in (prepare_booking, query_booking)
    ])

