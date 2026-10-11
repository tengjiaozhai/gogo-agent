"""阶段 D 的确定性差旅业务：身份、事务、状态与外部副作用边界。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
import re
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo
from sqlalchemy.exc import IntegrityError

from gogo_agent.auth.models import UserAccount
from gogo_agent.db.models import (
    ApprovalRecordModel, BookingRecordModel, BusinessActionAuditModel,
    TravelOrderModel, UserProfileModel,
)

from .cache import UserPolicyCache
from .conflict import TripSegment, detect_conflicts, whole_day_segment
from .knowledge import PolicyKnowledgeIndex, PolicySnippet
from .models import (
    AdminDecisionRequest, ApprovalStatus, ApprovalView, BookingCancelRequest,
    BookingCancelResult, BookingStatus, BookingType, BookingView, CancelResult,
    CancelTravelRequest, ConflictReport, ModifyTravelRequest, PolicyCheckInput,
    PolicyCheckResult, PolicyView, ProfileCompleteness, SubmitTravelRequest,
    TravelOrderDraft, TravelOrderStatus, TravelOrderView, UserProfilePatch,
    UserProfileView, MyTravelBooking, MyTravelOrder,
)
from .policy import PolicyCatalog, PolicyUnavailable, check_policy
from .repository import BusinessRepository


class BusinessError(ValueError):
    """可转换为稳定 HTTP 状态和工具错误码的业务拒绝。"""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class BookingGateway(Protocol):
    """模拟或真实供应商取消适配器，独立于本地记录事务。"""

    def cancel(self, booking: BookingView, reason: str | None) -> bool: ...


class DisabledBookingGateway:
    """正式供应商尚未接入时拒绝外部取消请求。"""

    def cancel(self, booking: BookingView, reason: str | None) -> bool:
        raise BusinessError("SUPPLIER_UNAVAILABLE", "供应商取消尚未接入", 503)


def _utc_now() -> datetime:
    """与 Java DATETIME 字段一致，持久化无时区的 UTC 时间。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _business_date() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return int(aware.timestamp() * 1000)


_ORDER_LABELS = {
    "DRAFT": "草稿", "SUBMITTED": "待审批", "APPROVED": "已通过",
    "REJECTED": "已拒绝", "COMPLETED": "已完成", "CANCELLED": "已取消",
}
_APPROVAL_LABELS = {"PENDING": "待审批", "APPROVED": "已通过", "REJECTED": "已拒绝", "CANCELLED": "已撤销"}
_BOOKING_LABELS = {"FLIGHT": "机票", "HOTEL": "酒店", "TRAIN": "火车"}
_INTERNATIONAL_KEYWORDS = ("日本", "韩国", "美国", "加拿大", "英国", "法国", "德国", "意大利", "西班牙", "新加坡", "泰国", "东京", "首尔", "纽约", "伦敦", "巴黎")


def _approval_view(row: ApprovalRecordModel) -> ApprovalView:
    return ApprovalView(
        process_instance_id=row.process_instance_id, user_id=row.user_id,
        order_id=row.order_id, title=row.title, status=row.status,
        approval_form=json.loads(row.approval_form), remark=row.remark,
        submit_time=row.submit_time, update_time=row.update_time,
    )


def _draft_fields(draft: TravelOrderDraft) -> dict[str, str]:
    return {
        "destination": draft.destination.strip(),
        "departure_city": draft.departure_city.strip(),
        "departure_date": draft.departure_date.isoformat(),
        "return_date": draft.return_date.isoformat(),
        "purpose": draft.purpose.strip(),
    }


class TravelBusinessService:
    """仅用服务端传入的用户身份操作本人业务数据和管理员审批。"""

    def __init__(
        self,
        repository: BusinessRepository,
        *,
        policy_catalog: PolicyCatalog | None = None,
        knowledge: PolicyKnowledgeIndex | None = None,
        policy_cache: UserPolicyCache | None = None,
        booking_gateway: BookingGateway | None = None,
    ):
        self.repository = repository
        self.policy_catalog = policy_catalog or PolicyCatalog()
        self.knowledge = knowledge or PolicyKnowledgeIndex()
        self.policy_cache = policy_cache or UserPolicyCache()
        self.booking_gateway = booking_gateway or DisabledBookingGateway()

    @contextmanager
    def _write_transaction(self):
        """把并发唯一键冲突转换成稳定业务拒绝，并回滚整次写入。"""
        try:
            with self.repository.transaction() as session:
                yield session
        except IntegrityError:
            raise BusinessError("CONCURRENT_WRITE", "数据已被其他请求更改，请重新查询后再试", 409) from None

    @staticmethod
    def _require_confirmed(confirmed: bool) -> None:
        if not confirmed:
            raise BusinessError("CONFIRMATION_REQUIRED", "请由已认证用户明确确认本次写入", 409)

    @staticmethod
    def _digest(payload: dict) -> str:
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def _active_fingerprint(cls, user_id: str, fields: dict[str, str]) -> str:
        """数据库唯一约束用的同用户有效申请内容指纹。"""
        return cls._digest({"user_id": user_id, "draft": fields})

    def _replay(self, session, user_id: str, action: str, key: str, payload: dict) -> dict | None:
        row = self.repository.audit(session, user_id, action, key)
        if row is None:
            return None
        if row.payload_hash != self._digest(payload):
            raise BusinessError("IDEMPOTENCY_CONFLICT", "同一幂等键对应不同请求内容", 409)
        return json.loads(row.result_json)

    def _audit(self, session, user_id: str, action: str, key: str, payload: dict, result: dict) -> None:
        session.add(BusinessActionAuditModel(
            action_id=f"act_{uuid4().hex}", user_id=user_id, action=action,
            idempotency_key=key, payload_hash=self._digest(payload),
            result_json=json.dumps(result, ensure_ascii=False, sort_keys=True), created_at=_utc_now(),
        ))

    @staticmethod
    def _need_order(row: TravelOrderModel | None) -> TravelOrderModel:
        if row is None:
            raise BusinessError("ORDER_NOT_FOUND", "差旅单不存在或不属于当前用户", 404)
        return row

    def get_profile(self, user_id: str) -> UserProfileView:
        with self.repository.transaction() as session:
            row = self.repository.profile(session, user_id)
            return UserProfileView.model_validate(row) if row else UserProfileView(user_id=user_id)

    def update_profile(self, user_id: str, patch: UserProfilePatch) -> UserProfileView:
        """按认证用户局部更新；错误和日志都不回显完整资料值。"""
        with self._write_transaction() as session:
            row = self.repository.profile(session, user_id)
            if row is None:
                row = UserProfileModel(user_id=user_id)
                session.add(row)
            for field, value in patch.model_dump(exclude_unset=True).items():
                setattr(row, field, value.strip() if isinstance(value, str) else value)
            session.flush()
            view = UserProfileView.model_validate(row)
        self.policy_cache.invalidate_user(user_id)
        return view

    def profile_completeness(self, user_id: str, biz_type: BookingType) -> ProfileCompleteness:
        profile = self.get_profile(user_id)
        required = {
            BookingType.FLIGHT: ("chinese_name", "id_type", "id_number", "phone", "gender"),
            BookingType.HOTEL: ("name_pinyin", "email", "phone"),
            BookingType.TRAIN: ("chinese_name", "id_type", "id_number", "phone"),
        }[biz_type]
        missing = [field for field in required if getattr(profile, field) is None or getattr(profile, field) == ""]
        return ProfileCompleteness(biz_type=biz_type, complete=not missing, missing_fields=missing)

    def query_policy(self, user_id: str, city: str, *, on_date: date | None = None) -> PolicyView:
        profile = self.get_profile(user_id)
        if not profile.level:
            raise BusinessError("PROFILE_LEVEL_MISSING", "用户档案缺少职级，无法确定适用政策", 409)
        today = on_date or _business_date()
        try:
            return self.policy_cache.get_or_load(
                user_id, city, profile.level, self.policy_catalog.version, today.isoformat(),
                lambda: self.policy_catalog.select(profile.level or "", city, on_date=today),
            )
        except PolicyUnavailable as exc:
            raise BusinessError("POLICY_NOT_FOUND", str(exc), 404) from None

    def check_policy(
        self, user_id: str, city: str, order: PolicyCheckInput,
        *, on_date: date | None = None,
    ) -> PolicyCheckResult:
        return check_policy(order, self.query_policy(user_id, city, on_date=on_date))

    def search_policy_knowledge(self, question: str) -> list[PolicySnippet]:
        return self.knowledge.retrieve(question)

    def get_order(self, user_id: str, order_id: str) -> TravelOrderView:
        with self.repository.transaction() as session:
            return TravelOrderView.model_validate(self._need_order(self.repository.order(session, user_id, order_id)))

    def list_orders(self, user_id: str, status: TravelOrderStatus | None = None) -> list[TravelOrderView]:
        with self.repository.transaction() as session:
            return [TravelOrderView.model_validate(row) for row in self.repository.orders(session, user_id, status)]

    def my_travel_orders(self, user_id: str) -> list[MyTravelOrder]:
        """只按当前用户聚合差旅单、当前审批与有效预订摘要。"""
        output: list[MyTravelOrder] = []
        with self.repository.transaction() as session:
            for order in self.repository.orders(session, user_id):
                approval = self.repository.approval(session, user_id, order.approval_id) if order.approval_id else None
                bookings = []
                for booking in self.repository.bookings(session, user_id, order_id=order.order_id):
                    bookings.append(MyTravelBooking(
                        bookingId=booking.booking_id, bizType=booking.biz_type,
                        bizTypeLabel=_BOOKING_LABELS.get(booking.biz_type, booking.biz_type),
                        title=booking.title, status=booking.status,
                        statusLabel=booking.status, externalStatus=booking.external_status,
                        totalAmount=format(booking.total_amount, "f") if booking.total_amount is not None else None,
                        currency=booking.currency, platform=booking.platform,
                        externalOrderNo=booking.external_order_no, paymentStatus=booking.payment_status,
                        startTime=_epoch_ms(booking.start_time), endTime=_epoch_ms(booking.end_time),
                        bookedAt=_epoch_ms(booking.booked_at), orderUrl=None,
                    ))
                output.append(MyTravelOrder(
                    orderId=order.order_id, destination=order.destination,
                    departureCity=order.departure_city, departureDate=order.departure_date,
                    returnDate=order.return_date, purpose=order.purpose,
                    status=order.status, statusLabel=_ORDER_LABELS.get(order.status, order.status),
                    createdAt=_epoch_ms(order.created_at), updatedAt=_epoch_ms(order.updated_at),
                    international=bool(re.search(r"[A-Za-z]", order.destination) or any(
                        word in order.destination for word in _INTERNATIONAL_KEYWORDS
                    )),
                    approvalId=approval.process_instance_id if approval else None,
                    approvalStatus=approval.status if approval else None,
                    approvalStatusLabel=_APPROVAL_LABELS.get(approval.status) if approval else None,
                    approvalRemark=approval.remark if approval else None,
                    approvalSubmitTime=_epoch_ms(approval.submit_time) if approval else None,
                    approvalUpdateTime=_epoch_ms(approval.update_time) if approval else None,
                    bookingCount=len(bookings), bookings=bookings,
                    planHtmlUrl=order.plan_html_url,
                ))
        return output

    def get_approval(self, user_id: str, approval_id: str) -> ApprovalView:
        with self.repository.transaction() as session:
            row = self.repository.approval(session, user_id, approval_id)
            if row is None:
                raise BusinessError("APPROVAL_NOT_FOUND", "审批记录不存在或不属于当前用户", 404)
            return _approval_view(row)

    def list_approvals_for_order(self, user_id: str, order_id: str) -> list[ApprovalView]:
        with self.repository.transaction() as session:
            self._need_order(self.repository.order(session, user_id, order_id))
            return [_approval_view(row) for row in self.repository.approvals_for_order(session, user_id, order_id)]

    def check_conflicts(
        self, user_id: str, draft: TravelOrderDraft, *, exclude_order_id: str | None = None,
    ) -> ConflictReport:
        fields = _draft_fields(draft)
        candidate = TripSegment(
            order_id=exclude_order_id or "candidate",
            departure_city=fields["departure_city"], destination=fields["destination"],
            start_at=datetime.combine(draft.departure_date, time.min),
            end_at=datetime.combine(draft.return_date + timedelta(days=1), time.min),
        )
        with self.repository.transaction() as session:
            existing = [
                whole_day_segment(TravelOrderView.model_validate(row))
                for row in self.repository.orders(session, user_id)
                if row.status in ("DRAFT", "SUBMITTED", "APPROVED")
            ]
        return detect_conflicts(candidate, existing, self.policy_catalog.travel_minutes)

    @staticmethod
    def _new_approval(order: TravelOrderModel, now: datetime) -> ApprovalRecordModel:
        snapshot = {
            "destination": order.destination,
            "departure_city": order.departure_city,
            "departure_date": order.departure_date,
            "return_date": order.return_date,
            "purpose": order.purpose,
        }
        return ApprovalRecordModel(
            process_instance_id=f"ap_{uuid4().hex}", user_id=order.user_id,
            title=f"{order.departure_city}→{order.destination} {order.departure_date}",
            status=ApprovalStatus.PENDING.value,
            approval_form=json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            remark=None, submit_time=now, update_time=now, order_id=order.order_id,
        )

    def submit_order(self, user_id: str, request: SubmitTravelRequest) -> TravelOrderView:
        self._require_confirmed(request.confirmed)
        draft = TravelOrderDraft.model_validate(request.model_dump(include=set(TravelOrderDraft.model_fields)))
        fields = _draft_fields(draft)
        payload = {"draft": fields}
        try:
            with self.repository.transaction() as session:
                replay = self._replay(session, user_id, "submit", request.idempotency_key, payload)
                if replay is not None:
                    return TravelOrderView.model_validate(replay)
                current = self.repository.matching_active_order(session, user_id, fields)
                if current is not None:
                    result = TravelOrderView.model_validate(current)
                else:
                    now = _utc_now()
                    current = TravelOrderModel(
                        order_id=f"to_{uuid4().hex}", user_id=user_id, **fields,
                        status=TravelOrderStatus.DRAFT.value, approval_id=None,
                        active_fingerprint=self._active_fingerprint(user_id, fields),
                        plan_html_url=None, created_at=now, updated_at=now,
                    )
                    session.add(current)
                    approval = self._new_approval(current, now)
                    session.add(approval)
                    current.approval_id = approval.process_instance_id
                    current.status = TravelOrderStatus.SUBMITTED.value
                    result = TravelOrderView.model_validate(current)
                self._audit(session, user_id, "submit", request.idempotency_key, payload, result.model_dump(mode="json"))
                return result
        except IntegrityError:
            raise BusinessError("CONCURRENT_SUBMIT", "相同申请正在提交，请重新查询差旅单", 409) from None

    def modify_order(self, user_id: str, order_id: str, request: ModifyTravelRequest) -> TravelOrderView:
        self._require_confirmed(request.confirmed)
        payload = {"order_id": order_id, "changes": request.changes.model_dump(mode="json", exclude_unset=True), "force": request.force}
        with self._write_transaction() as session:
            replay = self._replay(session, user_id, "modify", request.idempotency_key, payload)
            if replay is not None:
                return TravelOrderView.model_validate(replay)
            row = self._need_order(self.repository.order_for_update(session, user_id, order_id))
            if row.status in ("CANCELLED", "COMPLETED"):
                raise BusinessError("INVALID_ORDER_STATE", "已取消或已完成的差旅单不能修改", 409)
            if row.status == "APPROVED" and not request.force:
                raise BusinessError("FORCE_CONFIRMATION_REQUIRED", "已审批差旅单修改需再次确认撤审", 409)
            merged = {
                "destination": row.destination, "departure_city": row.departure_city,
                "departure_date": row.departure_date, "return_date": row.return_date,
                "purpose": row.purpose,
                **request.changes.model_dump(mode="json", exclude_unset=True),
            }
            validated = TravelOrderDraft.model_validate(merged)
            if row.approval_id:
                old = self.repository.approval_for_update(session, user_id, row.approval_id)
                if old is not None and old.status != ApprovalStatus.CANCELLED.value:
                    old.status = ApprovalStatus.CANCELLED.value
                    old.update_time = _utc_now()
            for name, value in _draft_fields(validated).items():
                setattr(row, name, value)
            row.active_fingerprint = self._active_fingerprint(user_id, _draft_fields(validated))
            now = _utc_now()
            row.status = TravelOrderStatus.DRAFT.value
            row.approval_id = None
            row.updated_at = now
            approval = self._new_approval(row, now)
            session.add(approval)
            row.approval_id = approval.process_instance_id
            row.status = TravelOrderStatus.SUBMITTED.value
            result = TravelOrderView.model_validate(row)
            self._audit(session, user_id, "modify", request.idempotency_key, payload, result.model_dump(mode="json"))
            return result

    def cancel_order(self, user_id: str, order_id: str, request: CancelTravelRequest) -> CancelResult:
        self._require_confirmed(request.confirmed)
        payload = {"order_id": order_id, "force": request.force}
        with self._write_transaction() as session:
            replay = self._replay(session, user_id, "cancel", request.idempotency_key, payload)
            if replay is not None:
                return CancelResult.model_validate(replay)
            row = self._need_order(self.repository.order_for_update(session, user_id, order_id))
            if row.status in ("CANCELLED", "COMPLETED"):
                raise BusinessError("INVALID_ORDER_STATE", "已取消或已完成的差旅单不能再次取消", 409)
            if row.status == "APPROVED" and not request.force:
                raise BusinessError("FORCE_CONFIRMATION_REQUIRED", "已审批差旅单取消需再次确认撤审", 409)
            approval_cancelled = False
            if row.approval_id:
                approval = self.repository.approval_for_update(session, user_id, row.approval_id)
                if approval is not None and approval.status != ApprovalStatus.CANCELLED.value:
                    approval.status = ApprovalStatus.CANCELLED.value
                    approval.update_time = _utc_now()
                    approval_cancelled = True
            row.status = TravelOrderStatus.CANCELLED.value
            row.active_fingerprint = None
            row.updated_at = _utc_now()
            result = CancelResult(cancelled=True, order_id=order_id, approval_cancelled=approval_cancelled)
            self._audit(session, user_id, "cancel", request.idempotency_key, payload, result.model_dump(mode="json"))
            return result

    def list_admin_approvals(self, admin: UserAccount, status: ApprovalStatus | None = None) -> list[ApprovalView]:
        if not admin.is_admin():
            raise BusinessError("ADMIN_REQUIRED", "仅管理员可查看审批列表", 403)
        with self.repository.transaction() as session:
            return [_approval_view(row) for row in self.repository.approvals(session, status)]

    def decide_approval(
        self, admin: UserAccount, approval_id: str, request: AdminDecisionRequest,
    ) -> ApprovalView:
        if not admin.is_admin():
            raise BusinessError("ADMIN_REQUIRED", "仅管理员可处理审批", 403)
        target = ApprovalStatus.APPROVED if request.decision == "agree" else ApprovalStatus.REJECTED
        with self._write_transaction() as session:
            snapshot = self.repository.approval_any_user(session, approval_id)
            if snapshot is None:
                raise BusinessError("APPROVAL_NOT_FOUND", "审批记录不存在", 404)
            order = self.repository.order_any_user_for_update(session, snapshot.order_id)
            row = self.repository.approval_for_update(session, snapshot.user_id, approval_id)
            if row is None:
                raise BusinessError("APPROVAL_NOT_FOUND", "审批记录不存在", 404)
            if order is None or order.user_id != row.user_id or order.approval_id != row.process_instance_id:
                raise BusinessError("APPROVAL_ORDER_MISMATCH", "审批与差旅单关联不一致", 409)
            if row.status == target.value:
                return _approval_view(row)
            if row.status != ApprovalStatus.PENDING.value:
                raise BusinessError("INVALID_APPROVAL_STATE", "审批已处理或已撤销", 409)
            if not self.repository.decide_if_pending(session, approval_id, target.value, request.remark):
                raise BusinessError("INVALID_APPROVAL_STATE", "审批已被其他请求处理", 409)
            session.refresh(row)
            row.update_time = _utc_now()
            order.status = TravelOrderStatus.APPROVED.value if target is ApprovalStatus.APPROVED else TravelOrderStatus.REJECTED.value
            if target is ApprovalStatus.REJECTED:
                order.active_fingerprint = None
            order.updated_at = _utc_now()
            return _approval_view(row)

    def list_bookings(
        self, user_id: str, *, status: BookingStatus | None = None,
        biz_type: BookingType | None = None, order_id: str | None = None,
    ) -> list[BookingView]:
        with self.repository.transaction() as session:
            return [BookingView.model_validate(row) for row in self.repository.bookings(
                session, user_id, status=status, biz_type=biz_type, order_id=order_id,
            )]

    def get_booking(self, user_id: str, booking_id: str) -> BookingView:
        with self.repository.transaction() as session:
            row = self.repository.booking(session, user_id, booking_id)
            if row is None:
                raise BusinessError("BOOKING_NOT_FOUND", "预订记录不存在或不属于当前用户", 404)
            return BookingView.model_validate(row)

    def cancel_booking(
        self, user_id: str, booking_id: str, request: BookingCancelRequest,
    ) -> BookingCancelResult:
        self._require_confirmed(request.confirmed)
        payload = {"booking_id": booking_id, "reason": request.reason}
        with self._write_transaction() as session:
            replay = self._replay(session, user_id, "booking_cancel", request.idempotency_key, payload)
            if replay is not None:
                return BookingCancelResult.model_validate(replay)
            row = self.repository.booking(session, user_id, booking_id)
            if row is None:
                raise BusinessError("BOOKING_NOT_FOUND", "预订记录不存在或不属于当前用户", 404)
            snapshot = BookingView.model_validate(row)
            if snapshot.status is BookingStatus.CANCELLED:
                result = BookingCancelResult(
                    cancelled=True, booking_id=booking_id,
                    supplier_cancelled=snapshot.external_status == "CANCELLED",
                    message="该预订记录此前已取消；供应商状态以外部确认记录为准",
                )
                self._audit(
                    session, user_id, "booking_cancel", request.idempotency_key,
                    payload, result.model_dump(mode="json"),
                )
                return result
        supplier_cancelled = False
        if snapshot.biz_type is not BookingType.HOTEL:
            if not self.booking_gateway.cancel(snapshot, request.reason):
                raise BusinessError("SUPPLIER_CANCEL_FAILED", "供应商未确认取消，本地状态保持不变", 503)
            supplier_cancelled = True
        with self._write_transaction() as session:
            row = self.repository.booking(session, user_id, booking_id)
            if row is None:
                raise BusinessError("BOOKING_NOT_FOUND", "预订记录不存在或不属于当前用户", 404)
            row.status = BookingStatus.CANCELLED.value
            if supplier_cancelled:
                row.external_status = "CANCELLED"
            row.remark = request.reason
            row.updated_at = _utc_now()
            message = (
                "供应商已确认取消，内部记录已更新" if supplier_cancelled
                else "仅将酒店预订标记为内部取消；请联系酒店确认外部订单"
            )
            result = BookingCancelResult(
                cancelled=True, booking_id=booking_id,
                supplier_cancelled=supplier_cancelled, message=message,
            )
            self._audit(session, user_id, "booking_cancel", request.idempotency_key, payload, result.model_dump(mode="json"))
            return result

    def delete_booking_record(self, user_id: str, booking_id: str) -> bool:
        """只软删除本人的内部记录，不声称供应商订单已取消。"""
        with self._write_transaction() as session:
            row = self.repository.booking(session, user_id, booking_id)
            if row is None:
                raise BusinessError("BOOKING_NOT_FOUND", "预订记录不存在或不属于当前用户", 404)
            row.deleted = 1
            row.updated_at = _utc_now()
            return True
