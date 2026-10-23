"""031/029 管理 Agent 的模型可见工具；身份只从闭包 RequestContext 注入。"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, Toolkit
from pydantic import ValidationError

from gogo_agent.request_context import RequestContext

from . import confirmation
from .models import (
    BookingStatus, BookingType, PolicyCheckInput, SubmitTravelRequest, ToolResponse,
    TravelOrderDraft, TravelOrderPatch, TravelOrderStatus, UserProfilePatch,
)
from .service import BusinessError, TravelBusinessService


def _success(data: dict[str, Any], message: str = "查询成功") -> str:
    return ToolResponse(success=True, message=message, data=data).model_dump_json()


def _failure(code: str, message: str, data: dict[str, Any] | None = None) -> str:
    return ToolResponse(success=False, errorCode=code, message=message, data=data).model_dump_json()


def _submit_idempotency_key(user_id: str, digest: str) -> str:
    """由用户与行程摘要确定性派生，模型重复调用或重试都命中同一条审计记录。"""
    return "submit:" + hashlib.sha256(f"{user_id}:{digest}".encode("utf-8")).hexdigest()[:48]


def build_manage_toolkit(service: TravelBusinessService, request: RequestContext) -> Toolkit:
    """只注册管理职责工具；差旅申请在用户确认令牌通过后真实提交，其余写工具仍只返回待确认结果。"""
    user_id = request.user_id

    def get_travel_order(order_id: str) -> str:
        """读取当前登录用户的差旅单；不存在或非本人返回固定错误。"""
        try:
            order = service.get_order(user_id, order_id)
            return _success(order.model_dump(mode="json", exclude={"user_id"}))
        except BusinessError as exc:
            return _failure(exc.code, str(exc))

    def query_travel_orders(status: str = "") -> str:
        """按可选状态列出当前登录用户的差旅单。"""
        try:
            selected = TravelOrderStatus(status.upper()) if status else None
            orders = [item.model_dump(mode="json", exclude={"user_id"}) for item in service.list_orders(user_id, selected)]
            return _success({"orders": orders})
        except ValueError:
            return _failure("INVALID_PARAM", "差旅单状态无效")

    def query_approval_status(approval_id: str) -> str:
        """只读取当前登录用户拥有的审批实例状态。"""
        try:
            approval = service.get_approval(user_id, approval_id)
            return _success(approval.model_dump(mode="json", exclude={"user_id"}))
        except BusinessError as exc:
            return _failure(exc.code, str(exc))

    def query_travel_policy(city: str) -> str:
        """按当前登录用户职级与目的地查询结构化精确政策。"""
        try:
            return _success(service.query_policy(user_id, city).model_dump(mode="json"))
        except BusinessError as exc:
            return _failure(exc.code, str(exc))

    def check_travel_policy(city: str, order_summary_json: str) -> str:
        """确定性检查订单摘要；不让模型自行判断额度合规。"""
        try:
            order = PolicyCheckInput.model_validate_json(order_summary_json)
        except (ValidationError, ValueError):
            return _failure("INVALID_PARAM", "订单摘要字段无效，无法判断合规")
        try:
            return _success(service.check_policy(user_id, city, order).model_dump(mode="json"))
        except BusinessError as exc:
            return _failure(exc.code, str(exc))

    def search_policy_knowledge(question: str) -> str:
        """检索带来源和版本的政策片段；空结果不能推断政策内容。"""
        snippets = service.search_policy_knowledge(question)
        if not snippets:
            return _failure("NO_EVIDENCE", "没有找到可引用的政策片段")
        return _success({"snippets": [item.model_dump(mode="json") for item in snippets]})

    def query_user_contact_info() -> str:
        """报告本人预订资料是否齐全，仅返回脱敏末尾信息。"""
        profile = service.get_profile(user_id)
        return _success({
            "base_city": profile.base_city,
            "level": profile.level,
            "phone_suffix": profile.phone[-4:] if profile.phone else None,
            "id_number_suffix": profile.id_number[-4:] if profile.id_number else None,
            "flight": service.profile_completeness(user_id, BookingType.FLIGHT).model_dump(mode="json"),
            "hotel": service.profile_completeness(user_id, BookingType.HOTEL).model_dump(mode="json"),
        })

    def update_user_contact_info(profile_json: str) -> str:
        """校验允许的资料字段并提出待确认更新；不会执行数据库写入。"""
        try:
            UserProfilePatch.model_validate_json(profile_json)
        except (ValidationError, ValueError):
            return _failure("INVALID_PARAM", "资料字段或格式无效")
        return _failure("CONFIRMATION_REQUIRED", "需要用户在已认证的资料更新入口明确确认；本对话未修改资料")

    def submit_travel_approval(
        destination: str, departure_city: str, departure_date: str,
        return_date: str, purpose: str,
        confirmation_token: str = "", acknowledge_conflicts: bool = False,
    ) -> str:
        """提交差旅申请（两段式）。

        第一次调用（不带 confirmation_token）：校验字段与行程冲突，返回待确认摘要和确认令牌，不写库。
        第二次调用（带第一次拿到的 confirmation_token）：校验令牌与摘要一致后真正创建差旅单并提交审批。

        Args:
            destination: 目的地城市
            departure_city: 出发城市
            departure_date: 出发日期，YYYY-MM-DD
            return_date: 返回日期，YYYY-MM-DD
            purpose: 出差事由
            confirmation_token: 上一次调用返回的确认令牌；为空表示只获取摘要
            acknowledge_conflicts: 用户已知情并同意忽略高等级冲突时置 true，需用户明确表态
        """
        try:
            draft = TravelOrderDraft(
                destination=destination, departure_city=departure_city,
                departure_date=date.fromisoformat(departure_date),
                return_date=date.fromisoformat(return_date), purpose=purpose,
            )
        except (ValidationError, ValueError):
            return _failure("INVALID_PARAM", "差旅申请字段或日期范围无效")

        digest = confirmation.draft_digest(user_id, draft.model_dump(mode="json"))
        draft_payload = draft.model_dump(mode="json")
        try:
            report = service.check_conflicts(user_id, draft)
        except BusinessError as exc:
            return _failure(exc.code, str(exc))
        conflicts = [item.model_dump(mode="json") for item in report.conflicts]
        high_conflicts = [item for item in report.conflicts if item.severity == "HIGH"]

        # 第一段：只签发摘要与令牌，绝不写库。
        if not confirmation_token.strip():
            token = confirmation.issue(
                user_id, digest, acknowledge_conflicts=acknowledge_conflicts,
            )
            message = (
                "请把下面的差旅摘要完整复述给用户并征求明确确认；"
                "用户确认后带上 confirmation_token 再次调用本工具即可完成提交"
            )
            if high_conflicts and not acknowledge_conflicts:
                message += "。存在高等级行程冲突，须用户明确同意后才能提交"
            return _failure("CONFIRMATION_REQUIRED", message, data={
                "draft": draft_payload,
                "conflicts": conflicts,
                "high_conflict": bool(high_conflicts),
                "confirmation_token": token,
            })

        # 第二段：令牌必须与当前摘要一致，且用户已知情冲突。
        try:
            payload = confirmation.verify(confirmation_token.strip(), user_id, digest)
        except confirmation.ConfirmationError as exc:
            return _failure(exc.code, exc.message, data={"draft": draft_payload, "conflicts": conflicts})
        if high_conflicts and not payload.get("ack"):
            token = confirmation.issue(user_id, digest, acknowledge_conflicts=False)
            return _failure(
                "CONFLICT_BLOCKING",
                "存在高等级行程冲突；用户明确同意忽略冲突后，请带 acknowledge_conflicts=true "
                "重新获取摘要与令牌，再提交",
                data={
                    "draft": draft_payload,
                    "conflicts": conflicts,
                    "high_conflict": True,
                    "confirmation_token": token,
                },
            )

        try:
            order = service.submit_order(
                user_id,
                SubmitTravelRequest(
                    **draft.model_dump(),
                    confirmed=True,
                    idempotency_key=_submit_idempotency_key(user_id, digest),
                ),
            )
        except BusinessError as exc:
            return _failure(exc.code, str(exc))
        return _success(
            order.model_dump(mode="json", exclude={"user_id"}),
            "差旅申请已提交，等待审批",
        )

    def modify_travel_order(order_id: str, changes_json: str) -> str:
        """提出本人差旅单修改请求；未获用户确认前不写入。"""
        try:
            service.get_order(user_id, order_id)
        except BusinessError as exc:
            return _failure(exc.code, str(exc))
        try:
            TravelOrderPatch.model_validate_json(changes_json)
        except (ValidationError, ValueError):
            return _failure("INVALID_PARAM", "差旅单修改字段无效")
        return _failure("CONFIRMATION_REQUIRED", "需要用户确认差旅单修改；本对话未写入")

    def cancel_travel_order(order_id: str) -> str:
        """提出本人差旅单取消请求；未获用户确认前不写入。"""
        try:
            service.get_order(user_id, order_id)
        except BusinessError as exc:
            return _failure(exc.code, str(exc))
        return _failure("CONFIRMATION_REQUIRED", "需要用户确认差旅单取消；本对话未写入")

    def check_travel_order_conflicts(
        destination: str, departure_city: str, departure_date: str,
        return_date: str, purpose: str, exclude_order_id: str = "",
    ) -> str:
        """对本人已有行程做日期重叠和跨城衔接检查。"""
        try:
            draft = TravelOrderDraft(
                destination=destination, departure_city=departure_city,
                departure_date=date.fromisoformat(departure_date),
                return_date=date.fromisoformat(return_date), purpose=purpose,
            )
            return _success(service.check_conflicts(
                user_id, draft, exclude_order_id=exclude_order_id or None,
            ).model_dump(mode="json"))
        except (ValidationError, ValueError):
            return _failure("INVALID_PARAM", "行程字段或日期范围无效")

    def query_booking_record(booking_id: str) -> str:
        """只读本人预订记录；管理 Agent 没有供应商下单或取消工具。"""
        try:
            booking = service.get_booking(user_id, booking_id)
            return _success(booking.model_dump(mode="json", exclude={"user_id"}))
        except BusinessError as exc:
            return _failure(exc.code, str(exc))

    def query_booking_records(status: str = "", biz_type: str = "") -> str:
        """按可选状态和业务类型列出当前登录用户的预订记录。"""
        try:
            selected_status = BookingStatus(status.upper()) if status else None
            selected_type = BookingType(biz_type.upper()) if biz_type else None
        except ValueError:
            return _failure("INVALID_PARAM", "预订状态或业务类型无效")
        rows = service.list_bookings(user_id, status=selected_status, biz_type=selected_type)
        return _success({"bookings": [row.model_dump(mode="json", exclude={"user_id"}) for row in rows]})

    def request_booking_cancellation(booking_id: str) -> str:
        """核验本人预订并提示独立确认；不执行本地或供应商取消。"""
        try:
            service.get_booking(user_id, booking_id)
        except BusinessError as exc:
            return _failure(exc.code, str(exc))
        return _failure("CONFIRMATION_REQUIRED", "需在已认证入口确认取消；本对话未改变预订或供应商状态")

    allowed = PermissionDecision(behavior=PermissionBehavior.ALLOW, message="允许受控差旅查询或待确认建议")
    # 提交工具在用户确认令牌通过后会真实写库，因此不标记为只读。
    write_tools = {submit_travel_approval}
    return Toolkit(tools=[
        FunctionTool(function, name=function.__name__, is_read_only=function not in write_tools,
                     is_concurrency_safe=False, permission=allowed)
        for function in (
            get_travel_order, query_travel_orders, query_approval_status,
            query_travel_policy, check_travel_policy, search_policy_knowledge,
            query_user_contact_info, update_user_contact_info,
            submit_travel_approval, modify_travel_order, cancel_travel_order,
            check_travel_order_conflicts, query_booking_record,
            query_booking_records, request_booking_cancellation,
        )
    ])
