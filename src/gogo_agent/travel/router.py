"""只以认证用户身份开放差旅读写与管理员本地审批。"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from gogo_agent.auth.dependencies import get_current_user, require_admin
from gogo_agent.auth.models import UserAccount

from .dependencies import get_travel_service
from .knowledge import PolicySnippet
from .models import (
    AdminDecisionRequest, ApprovalStatus, ApprovalView, BookingCancelRequest,
    BookingCancelResult, BookingStatus, BookingType, BookingView, CancelResult,
    CancelTravelRequest, ConflictReport, ModifyTravelRequest, MyTravelOrder,
    PolicyCheckInput, PolicyCheckResult, PolicyView, ProfileCompleteness,
    SubmitTravelRequest, TravelOrderDraft, TravelOrderStatus, TravelOrderView,
    UserProfilePatch, UserProfileView,
)
from .service import TravelBusinessService


travel_router = APIRouter(prefix="/api/my-travel", tags=["travel"])
admin_router = APIRouter(prefix="/api/admin/approvals", tags=["approval"])
callback_router = APIRouter(prefix="/api/callback/dingtalk", tags=["approval callback"])


@travel_router.get("/orders", response_model=list[MyTravelOrder], summary="查看本人差旅单及审批和预订摘要")
def my_orders(
    current_user: UserAccount = Depends(get_current_user),
    service: TravelBusinessService = Depends(get_travel_service),
) -> list[MyTravelOrder]:
    return service.my_travel_orders(current_user.user_id)


@travel_router.post("/orders", response_model=TravelOrderView, summary="本人确认后创建并提交本地差旅审批")
def submit_order(
    request: SubmitTravelRequest,
    current_user: UserAccount = Depends(get_current_user),
    service: TravelBusinessService = Depends(get_travel_service),
) -> TravelOrderView:
    return service.submit_order(current_user.user_id, request)


@travel_router.get("/orders/{order_id}", response_model=TravelOrderView, summary="读取本人指定差旅单")
def get_order(
    order_id: str,
    current_user: UserAccount = Depends(get_current_user),
    service: TravelBusinessService = Depends(get_travel_service),
) -> TravelOrderView:
    return service.get_order(current_user.user_id, order_id)


@travel_router.put("/orders/{order_id}", response_model=TravelOrderView, summary="本人确认后修改差旅单并重提审批")
def modify_order(
    order_id: str,
    request: ModifyTravelRequest,
    current_user: UserAccount = Depends(get_current_user),
    service: TravelBusinessService = Depends(get_travel_service),
) -> TravelOrderView:
    return service.modify_order(current_user.user_id, order_id, request)


@travel_router.post("/orders/{order_id}/cancel", response_model=CancelResult, summary="本人确认后取消差旅单")
def cancel_order(
    order_id: str,
    request: CancelTravelRequest,
    current_user: UserAccount = Depends(get_current_user),
    service: TravelBusinessService = Depends(get_travel_service),
) -> CancelResult:
    return service.cancel_order(current_user.user_id, order_id, request)


@travel_router.get("/orders/{order_id}/approvals", response_model=list[ApprovalView], summary="读取本人差旅单的审批历史")
def order_approvals(
    order_id: str,
    current_user: UserAccount = Depends(get_current_user),
    service: TravelBusinessService = Depends(get_travel_service),
) -> list[ApprovalView]:
    return service.list_approvals_for_order(current_user.user_id, order_id)


@travel_router.get("/approvals/{approval_id}", response_model=ApprovalView, summary="读取本人审批状态")
def my_approval(
    approval_id: str,
    current_user: UserAccount = Depends(get_current_user),
    service: TravelBusinessService = Depends(get_travel_service),
) -> ApprovalView:
    return service.get_approval(current_user.user_id, approval_id)


@travel_router.post("/conflicts", response_model=ConflictReport, summary="检查本人现有行程与候选行程的日期和跨城衔接")
def check_conflicts(
    draft: TravelOrderDraft,
    exclude_order_id: str | None = None,
    current_user: UserAccount = Depends(get_current_user),
    service: TravelBusinessService = Depends(get_travel_service),
) -> ConflictReport:
    return service.check_conflicts(current_user.user_id, draft, exclude_order_id=exclude_order_id)


@travel_router.get("/profile", response_model=UserProfileView, summary="查看本人联系资料")
def my_profile(
    current_user: UserAccount = Depends(get_current_user),
    service: TravelBusinessService = Depends(get_travel_service),
) -> UserProfileView:
    return service.get_profile(current_user.user_id)


@travel_router.patch("/profile", response_model=UserProfileView, summary="局部更新本人允许的联系资料")
def update_profile(
    patch: UserProfilePatch,
    current_user: UserAccount = Depends(get_current_user),
    service: TravelBusinessService = Depends(get_travel_service),
) -> UserProfileView:
    return service.update_profile(current_user.user_id, patch)


@travel_router.get("/profile/completeness", response_model=ProfileCompleteness, summary="按预订类型检查本人资料缺项")
def profile_completeness(
    biz_type: BookingType,
    current_user: UserAccount = Depends(get_current_user),
    service: TravelBusinessService = Depends(get_travel_service),
) -> ProfileCompleteness:
    return service.profile_completeness(current_user.user_id, biz_type)


@travel_router.get("/policy", response_model=PolicyView, summary="查询本人职级与目的地适用的精确政策")
def my_policy(
    city: str,
    on_date: date | None = None,
    current_user: UserAccount = Depends(get_current_user),
    service: TravelBusinessService = Depends(get_travel_service),
) -> PolicyView:
    return service.query_policy(current_user.user_id, city, on_date=on_date)


@travel_router.post("/policy/check", response_model=PolicyCheckResult, summary="按本人政策检查候选订单是否合规")
def policy_check(
    city: str,
    order: PolicyCheckInput,
    current_user: UserAccount = Depends(get_current_user),
    service: TravelBusinessService = Depends(get_travel_service),
) -> PolicyCheckResult:
    return service.check_policy(current_user.user_id, city, order)


@travel_router.get("/policy/knowledge", response_model=list[PolicySnippet], summary="检索带来源与版本的政策文本片段")
def policy_knowledge(
    question: str,
    current_user: UserAccount = Depends(get_current_user),
    service: TravelBusinessService = Depends(get_travel_service),
) -> list[PolicySnippet]:
    return service.search_policy_knowledge(question)


@travel_router.get("/bookings", response_model=list[BookingView], summary="按类型或状态筛选本人预订记录")
def my_bookings(
    status: BookingStatus | None = None,
    biz_type: BookingType | None = None,
    order_id: str | None = None,
    current_user: UserAccount = Depends(get_current_user),
    service: TravelBusinessService = Depends(get_travel_service),
) -> list[BookingView]:
    return service.list_bookings(current_user.user_id, status=status, biz_type=biz_type, order_id=order_id)


@travel_router.get("/bookings/{booking_id}", response_model=BookingView, summary="读取本人指定预订记录")
def my_booking(
    booking_id: str,
    current_user: UserAccount = Depends(get_current_user),
    service: TravelBusinessService = Depends(get_travel_service),
) -> BookingView:
    return service.get_booking(current_user.user_id, booking_id)


@travel_router.post("/bookings/{booking_id}/cancel", response_model=BookingCancelResult, summary="本人确认后模拟取消预订")
def cancel_booking(
    booking_id: str,
    request: BookingCancelRequest,
    current_user: UserAccount = Depends(get_current_user),
    service: TravelBusinessService = Depends(get_travel_service),
) -> BookingCancelResult:
    return service.cancel_booking(current_user.user_id, booking_id, request)


@travel_router.delete("/bookings/{booking_id}", summary="软删除本人的本地预订记录")
def delete_booking(
    booking_id: str,
    current_user: UserAccount = Depends(get_current_user),
    service: TravelBusinessService = Depends(get_travel_service),
) -> dict[str, bool]:
    return {"deleted": service.delete_booking_record(current_user.user_id, booking_id)}


@admin_router.get("", response_model=list[ApprovalView], summary="管理员查看本地审批列表")
def admin_approvals(
    status: ApprovalStatus | None = None,
    admin: UserAccount = Depends(require_admin),
    service: TravelBusinessService = Depends(get_travel_service),
) -> list[ApprovalView]:
    return service.list_admin_approvals(admin, status)


@admin_router.post("/{approval_id}/decision", response_model=ApprovalView, summary="管理员审批并同步差旅单状态")
def admin_decision(
    approval_id: str,
    request: AdminDecisionRequest,
    admin: UserAccount = Depends(require_admin),
    service: TravelBusinessService = Depends(get_travel_service),
) -> ApprovalView:
    return service.decide_approval(admin, approval_id, request)


@callback_router.post("/approval", summary="钉钉审批回调暂未开放")
def disabled_approval_callback() -> None:
    """源端未配置可信来源签名，拒绝一切外部事件且不触碰业务状态。"""
    raise HTTPException(503, "审批回调来源认证未接入，当前入口禁用")
