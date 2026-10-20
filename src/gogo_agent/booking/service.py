"""061–064 预订服务：唯一的下单入口，并统一处理门禁、幂等、未知结果与审计。

取消复用阶段 D 的 `TravelBusinessService.cancel_booking`，本模块只在酒店取消前补做供应商侧取消，
避免本地记录显示已取消而供应商订单仍然有效。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from gogo_agent.db.models import BookingRecordModel
from gogo_agent.planning.models import PlanView, Proposal, ReviewVerdict
from gogo_agent.planning.service import PlanError, PlanService
from gogo_agent.travel.models import (
    BookingCancelRequest, BookingCancelResult, BookingStatus, BookingType, TravelOrderStatus,
)
from gogo_agent.travel.service import BusinessError, TravelBusinessService

from .reliability import RetryPolicy, call_read_with_retry
from .supplier import SupplierGateway, SupplierOrder, SupplierRejected, SupplierUnknownOutcome

LEGS = ("outbound", "inbound", "hotel")
PLATFORM = "simulated-supplier"
CURRENCY = "CNY"
LOOKUP_POLICY = RetryPolicy()


class BookingError(ValueError):
    """预订或取消被门禁、权限或供应商明确拒绝时的错误，带稳定错误码。"""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class BookingOutcome(BaseModel):
    """一次预订请求的最终可验证结果；UNKNOWN 不等于成功，也不等于失败。"""

    model_config = ConfigDict(extra="forbid")

    booking_id: str = Field(..., description="本地预订记录标识，同时作为供应商请求 ID")
    leg: str = Field(..., description="预订的行程段：outbound、inbound 或 hotel")
    resolution: str = Field(..., description="最终结果：CONFIRMED、FAILED 或 UNKNOWN")
    external_order_no: str | None = Field(default=None, description="供应商订单号；未确认时为空")
    amount: Decimal = Field(..., description="本段金额，元，保留两位小数")
    currency: str = Field(..., description="金额币种")
    message: str = Field(..., description="面向用户和调用方的结果说明")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def booking_id_for(user_id: str, plan_id: str, content_version: int, proposal_tag: str, leg: str) -> str:
    """业务幂等键派生的预订标识：同一用户、计划内容版本、方案与行程段只对应一条本地记录。"""
    raw = "|".join([user_id, plan_id, str(content_version), proposal_tag, leg])
    return "bk_" + sha256(raw.encode("utf-8")).hexdigest()[:32]


def _leg_spec(proposal: Proposal, leg: str) -> dict[str, Any]:
    """从已审核的代表方案展开一段预订；金额只来自方案，不接受模型自报。"""
    if leg == "hotel":
        hotel = proposal.hotel
        amount = Decimal(f"{hotel.price_per_night * hotel.nights:.2f}")
        return {
            "biz_type": BookingType.HOTEL, "amount": amount, "code": hotel.id,
            "title": f"酒店 {hotel.name} {hotel.nights} 晚",
        }
    candidate = proposal.outbound if leg == "outbound" else proposal.inbound
    biz_type = BookingType.FLIGHT if candidate.type.value == "flight" else BookingType.TRAIN
    return {
        "biz_type": biz_type, "amount": Decimal(f"{candidate.price:.2f}"), "code": candidate.code or candidate.id,
        "title": f"{'去程' if leg == 'outbound' else '返程'} {candidate.code or candidate.id} {candidate.origin}→{candidate.destination}",
    }


class BookingService:
    """预订门禁与执行：先查本地审核和差旅审批，再记录待处理状态，最后调用供应商。"""

    def __init__(
        self, travel: TravelBusinessService, plans: PlanService, supplier: SupplierGateway,
        *, sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.travel = travel
        self.plans = plans
        self.supplier = supplier
        self._sleep = sleep

    def _gate(self, user_id: str, plan_id: str, proposal_tag: str, leg: str) -> tuple[PlanView, Proposal]:
        if leg not in LEGS:
            raise BookingError("INVALID_LEG", "预订行程段必须是 outbound、inbound 或 hotel", 400)
        try:
            plan = self.plans.get(user_id, plan_id)
        except PlanError as exc:
            raise BookingError(exc.code, str(exc), exc.status) from None
        if plan.result is None:
            raise BookingError("PLAN_NOT_COMPUTED", "计划尚未计算方案，不能预订")
        proposal = next((p for p in plan.result.proposals if proposal_tag in p.tags), None)
        if proposal is None:
            raise BookingError("PROPOSAL_NOT_FOUND", f"没有标签为「{proposal_tag}」的代表方案", 404)
        review = plan.last_review
        if review is None or not plan.review_is_current() or review.proposal_tag != proposal_tag:
            raise BookingError("REVIEW_REQUIRED", "需先对同一代表方案完成当前内容版本的审核")
        if review.verdict is not ReviewVerdict.PASS:
            raise BookingError("REVIEW_NOT_PASSED", "审核结论不是通过，不能预订")
        if plan.order_id is None:
            raise BookingError("APPROVAL_REQUIRED", "计划未关联差旅单，无法核对审批")
        try:
            order = self.travel.get_order(user_id, plan.order_id)
        except BusinessError as exc:
            raise BookingError(exc.code, str(exc), exc.status_code) from None
        if order.status is not TravelOrderStatus.APPROVED:
            raise BookingError("APPROVAL_NOT_APPROVED", f"差旅单当前为 {order.status.value}，尚未审批通过")
        return plan, proposal

    def check_ready(self, user_id: str, plan_id: str, *, proposal_tag: str, leg: str) -> dict[str, Any]:
        """只检查门禁并返回待确认摘要；不写本地记录，也不调用供应商。"""
        plan, proposal = self._gate(user_id, plan_id, proposal_tag, leg)
        spec = _leg_spec(proposal, leg)
        return {
            "plan_id": plan.plan_id, "content_version": plan.content_version, "proposal_tag": proposal_tag,
            "leg": leg, "biz_type": spec["biz_type"].value, "title": spec["title"],
            "amount": f"{spec['amount']:.2f}", "currency": CURRENCY,
        }

    def book(self, user_id: str, plan_id: str, *, proposal_tag: str, leg: str, confirmed: bool) -> BookingOutcome:
        """执行一段预订；同一业务键重复调用不会再次创建供应商订单。"""
        if not confirmed:
            raise BookingError("CONFIRMATION_REQUIRED", "请由已认证用户明确确认本次预订", 409)
        plan, proposal = self._gate(user_id, plan_id, proposal_tag, leg)
        spec = _leg_spec(proposal, leg)
        payload = {
            "leg": leg, "code": spec["code"], "amount": f"{spec['amount']:.2f}", "currency": CURRENCY,
            "title": spec["title"], "plan_id": plan.plan_id, "content_version": plan.content_version,
            "proposal_tag": proposal_tag,
        }
        digest = _digest(payload)
        booking_id = booking_id_for(user_id, plan.plan_id, plan.content_version, proposal_tag, leg)
        existing = self._load(user_id, booking_id)
        if existing is not None:
            return self._replay(user_id, booking_id, existing, digest, leg)
        if plan.order_id is not None and self._confirmed_leg_exists(user_id, plan.order_id, leg):
            raise BookingError("BOOKING_ALREADY_CONFIRMED", "该差旅单的此行程段已有确认预订，请先取消旧预订")
        self._insert_pending(user_id, booking_id, plan, spec, payload, digest)
        return self._execute(user_id, booking_id, payload, leg, spec["amount"])

    def _load(self, user_id: str, booking_id: str) -> dict[str, Any] | None:
        """在事务内取出快照，避免事务结束后访问 ORM 对象。"""
        with self.travel.repository.transaction() as session:
            row = self.travel.repository.booking(session, user_id, booking_id)
            if row is None:
                return None
            return {
                "status": row.status, "external_order_no": row.external_order_no,
                "detail": row.detail, "total_amount": row.total_amount,
            }

    def _confirmed_leg_exists(self, user_id: str, order_id: str, leg: str) -> bool:
        """同一差旅单的同一行程段只允许一个确认预订；重新规划产生的新业务标识也要经过此检查。"""
        with self.travel.repository.transaction() as session:
            for row in self.travel.repository.bookings(session, user_id, order_id=order_id):
                if row.status != BookingStatus.CONFIRMED.value:
                    continue
                stored = json.loads(row.detail or "{}").get("payload", {})
                if stored.get("leg") == leg:
                    return True
        return False

    def _insert_pending(self, user_id: str, booking_id: str, plan: PlanView, spec: dict[str, Any],
                        payload: dict[str, Any], digest: str) -> None:
        """先落本地待处理记录，使供应商结果未知时仍有证据可对账。"""
        try:
            with self.travel.repository.transaction() as session:
                session.add(BookingRecordModel(
                    booking_id=booking_id, user_id=user_id, conversation_id=None,
                    travel_order_id=plan.order_id, biz_type=spec["biz_type"].value, platform=PLATFORM,
                    external_order_no=None, status=BookingStatus.CREATED.value,
                    external_status="PENDING_SUPPLIER", payment_status="UNPAID", title=spec["title"],
                    total_amount=spec["amount"], currency=CURRENCY,
                    detail=json.dumps({"payload_hash": digest, "payload": payload}, ensure_ascii=False, sort_keys=True),
                    remark=None, booked_at=None, created_at=_utc_now(), updated_at=_utc_now(), deleted=0,
                ))
        except IntegrityError:
            raise BookingError("CONCURRENT_WRITE", "同一预订正在处理，请稍后查询结果", 409) from None

    def _execute(self, user_id: str, booking_id: str, payload: dict[str, Any], leg: str, amount: Decimal) -> BookingOutcome:
        """写入类调用不重试：拒绝视为失败，超时或异常视为未知并转入只读对账。"""
        try:
            order = self.supplier.create_order(booking_id, payload)
        except SupplierRejected as exc:
            self._finish(user_id, booking_id, status=BookingStatus.FAILED, external_status=exc.code, remark=exc.message)
            return self._outcome(booking_id, leg, "FAILED", None, amount, f"供应商明确拒绝，未生成订单：{exc.code}")
        except Exception:  # 包括 SupplierUnknownOutcome：订单可能已生成，不能宣称失败
            self._mark_unknown(user_id, booking_id)
            return self._reconcile(user_id, booking_id, leg, amount)
        self._finish_confirmed(user_id, booking_id, order)
        return self._outcome(booking_id, leg, "CONFIRMED", order.order_no, amount, "供应商已确认订单，本地记录与供应商一致")

    def _replay(self, user_id: str, booking_id: str, row: dict[str, Any], digest: str, leg: str) -> BookingOutcome:
        stored = json.loads(row["detail"] or "{}")
        if stored.get("payload_hash") != digest:
            raise BookingError("IDEMPOTENCY_CONFLICT", "同一预订标识对应不同请求内容", 409)
        amount = Decimal(row["total_amount"] or 0)
        if row["status"] == BookingStatus.CONFIRMED.value:
            return self._outcome(booking_id, leg, "CONFIRMED", row["external_order_no"], amount,
                                 "已存在确认的预订，未重复下单")
        if row["status"] == BookingStatus.FAILED.value:
            return self._outcome(booking_id, leg, "FAILED", None, amount, "此前预订已被供应商拒绝，未重复下单")
        return self._reconcile(user_id, booking_id, leg, amount)

    def _reconcile(self, user_id: str, booking_id: str, leg: str, amount: Decimal) -> BookingOutcome:
        """只读查询供应商结果；查不到时保持未知，绝不再次调用创建。"""
        try:
            order = call_read_with_retry(lambda: self.supplier.lookup(booking_id), LOOKUP_POLICY, sleep=self._sleep)
        except Exception:
            order = None
        if order is None:
            return self._outcome(booking_id, leg, "UNKNOWN", None, amount,
                                 "供应商结果未知：未宣称成功，已保留待对账记录，请勿用新的业务键重复下单")
        self._finish_confirmed(user_id, booking_id, order)
        return self._outcome(booking_id, leg, "CONFIRMED", order.order_no, amount, "对账确认：供应商已有该订单")

    def _mark_unknown(self, user_id: str, booking_id: str) -> None:
        with self.travel.repository.transaction() as session:
            row = self.travel.repository.booking(session, user_id, booking_id)
            if row is None:
                return
            row.external_status = "UNKNOWN"
            row.remark = "供应商结果未知，等待只读对账"
            row.updated_at = _utc_now()

    def _finish_confirmed(self, user_id: str, booking_id: str, order: SupplierOrder) -> None:
        with self.travel.repository.transaction() as session:
            row = self.travel.repository.booking(session, user_id, booking_id)
            if row is None:
                raise BookingError("BOOKING_NOT_FOUND", "预订记录不存在", 404)
            row.status = BookingStatus.CONFIRMED.value
            row.external_order_no = order.order_no
            row.external_status = order.status
            row.booked_at = _utc_now()
            row.remark = None
            row.updated_at = _utc_now()

    def _finish(self, user_id: str, booking_id: str, *, status: BookingStatus, external_status: str, remark: str) -> None:
        with self.travel.repository.transaction() as session:
            row = self.travel.repository.booking(session, user_id, booking_id)
            if row is None:
                raise BookingError("BOOKING_NOT_FOUND", "预订记录不存在", 404)
            row.status = status.value
            row.external_status = external_status
            row.remark = remark
            row.updated_at = _utc_now()

    def _outcome(self, booking_id: str, leg: str, resolution: str, order_no: str | None,
                 amount: Decimal, message: str) -> BookingOutcome:
        return BookingOutcome(
            booking_id=booking_id, leg=leg, resolution=resolution, external_order_no=order_no,
            amount=amount, currency=CURRENCY, message=message,
        )

    def cancel(
        self, user_id: str, booking_id: str, *, idempotency_key: str, confirmed: bool, reason: str | None = None,
    ) -> BookingCancelResult:
        """取消本人预订。酒店已由供应商确认时先取消供应商侧，再交给阶段 D 更新本地状态。"""
        if not confirmed:
            raise BookingError("CONFIRMATION_REQUIRED", "请由已认证用户明确确认本次取消", 409)
        try:
            booking = self.travel.get_booking(user_id, booking_id)
        except BusinessError as exc:
            raise BookingError(exc.code, str(exc), exc.status_code) from None
        if (booking.biz_type is BookingType.HOTEL and booking.external_order_no
                and booking.status is BookingStatus.CONFIRMED):
            self._cancel_hotel_at_supplier(booking.external_order_no, reason)
        try:
            result = self.travel.cancel_booking(
                user_id, booking_id,
                BookingCancelRequest(confirmed=True, idempotency_key=idempotency_key, reason=reason),
            )
        except BusinessError as exc:
            raise BookingError(exc.code, str(exc), exc.status_code) from None
        if (booking.biz_type is BookingType.HOTEL and booking.external_order_no
                and booking.status is BookingStatus.CONFIRMED):
            # 阶段 D 的酒店分支只改本地状态；供应商侧已在上面取消，这里以供应商结果为准返回。
            return result.model_copy(update={
                "supplier_cancelled": True, "message": "供应商已确认取消，内部记录已更新",
            })
        return result

    def _cancel_hotel_at_supplier(self, order_no: str, reason: str | None) -> None:
        try:
            cancelled = self.supplier.cancel(order_no, reason)
        except SupplierRejected as exc:
            raise BookingError("SUPPLIER_CANCEL_FAILED", f"供应商未确认取消：{exc.message}", 503) from None
        except SupplierUnknownOutcome:
            raise BookingError("SUPPLIER_CANCEL_UNKNOWN", "供应商取消结果未知，本地状态保持不变", 503) from None
        if not cancelled:
            raise BookingError("SUPPLIER_CANCEL_FAILED", "供应商未确认取消，本地状态保持不变", 503)

