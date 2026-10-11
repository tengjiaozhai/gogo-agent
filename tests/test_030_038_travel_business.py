"""阶段 D 业务模型、政策、联系人、差旅单、冲突、预订和审批的隔离验收。"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from gogo_agent.auth.models import UserAccount
from gogo_agent.db.base import Base
from gogo_agent.db.models import BookingRecordModel, TravelOrderModel, UserProfileModel
from gogo_agent.travel.conflict import TripSegment, detect_conflicts
from gogo_agent.travel.models import (
    AdminDecisionRequest, ApprovalStatus, BookingCancelRequest, BookingStatus,
    BookingType, CancelTravelRequest, ModifyTravelRequest, PolicyCheckInput,
    SubmitTravelRequest, TravelOrderDraft, TravelOrderPatch, TravelOrderStatus,
    UserProfilePatch,
)
from gogo_agent.travel.policy import DEFAULT_POLICY_PATH, PolicyCatalog, PolicyUnavailable, check_policy
from gogo_agent.travel.repository import BusinessRepository
from gogo_agent.travel.service import BusinessError, TravelBusinessService


@pytest.fixture
def business():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    service = TravelBusinessService(BusinessRepository(sessionmaker(bind=engine, expire_on_commit=False)))
    with service.repository.transaction() as session:
        session.add_all([UserProfileModel(user_id="u001", level="P6"), UserProfileModel(user_id="u002", level="P5")])
    service.update_profile("u001", UserProfilePatch(base_city="北京", phone="13800000001"))
    service.update_profile("u002", UserProfilePatch(base_city="上海"))
    yield service
    engine.dispose()


def draft(**changes):
    return {
        "destination": "上海", "departure_city": "北京",
        "departure_date": "2026-11-10", "return_date": "2026-11-12",
        "purpose": "客户拜访", **changes,
    }


def submit_request(key="submit-1", **changes):
    return SubmitTravelRequest(**draft(**changes), confirmed=True, idempotency_key=key)


def admin(role="ADMIN"):
    return UserAccount(user_id="u_admin", username="admin", password_hash="test", role=role)


def test_travel_model_rejects_invalid_date_and_missing_business_fields():
    assert TravelOrderDraft(**draft()).departure_date == date(2026, 11, 10)
    with pytest.raises(ValidationError):
        TravelOrderDraft(**draft(return_date="2026-11-09"))
    with pytest.raises(ValidationError):
        TravelOrderDraft(**draft(destination=""))
    with pytest.raises(ValidationError):
        TravelOrderPatch()


def test_policy_matches_level_city_and_rejects_missing_amount_or_expired_rule(business, tmp_path):
    catalog = PolicyCatalog()
    assert len(catalog.rules) == 12
    p6_shanghai = business.query_policy("u001", "上海", on_date=date(2026, 10, 7))
    p5_shanghai = business.query_policy("u002", "上海", on_date=date(2026, 10, 7))
    assert p6_shanghai.rule.hotel_limit == Decimal("500")
    assert p5_shanghai.rule.hotel_limit == Decimal("400")
    assert business.query_policy("u001", "济南", on_date=date(2026, 10, 7)).rule.hotel_limit == Decimal("350")
    exact = check_policy(PolicyCheckInput(type="HOTEL", amount=Decimal("500")), p6_shanghai)
    over = check_policy(PolicyCheckInput(type="HOTEL", amount=Decimal("500.01")), p6_shanghai)
    missing = check_policy(PolicyCheckInput(type="HOTEL"), p6_shanghai)
    assert exact.compliant and not over.compliant and not missing.compliant
    assert not check_policy(PolicyCheckInput(type="FLIGHT", amount=100), p6_shanghai).compliant
    assert not check_policy(PolicyCheckInput(type="TRAIN", amount=100), p6_shanghai).compliant
    with pytest.raises(PolicyUnavailable):
        catalog.select("P0", "上海", on_date=date(2026, 10, 7))
    old_rules = json.loads(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    old_rules["effective_until"] = "2026-01-02"
    expired_path = tmp_path / "expired.json"
    expired_path.write_text(json.dumps(old_rules, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PolicyUnavailable):
        PolicyCatalog(expired_path).select("P6", "上海", on_date=date(2026, 10, 7))


def test_profile_update_is_scoped_to_authenticated_user_and_reports_missing_fields(business):
    profile = business.update_profile("u001", UserProfilePatch(chinese_name="张三", id_type=0, id_number="TEST12345"))
    assert profile.user_id == "u001" and profile.chinese_name == "张三"
    assert business.get_profile("u002").chinese_name is None
    assert business.profile_completeness("u001", BookingType.FLIGHT).missing_fields == ["gender"]
    assert business.profile_completeness("u001", BookingType.HOTEL).missing_fields == ["name_pinyin", "email"]
    with pytest.raises(ValidationError):
        UserProfilePatch(userId="u002")
    with pytest.raises(ValidationError):
        UserProfilePatch(level="P9")
    with pytest.raises(ValidationError):
        UserProfilePatch(phone="not-a-phone")


def test_submit_modify_approve_cancel_transaction_and_idempotency(business):
    with pytest.raises(BusinessError) as error:
        business.submit_order("u001", SubmitTravelRequest(**draft(), confirmed=False, idempotency_key="no"))
    assert error.value.code == "CONFIRMATION_REQUIRED"

    order = business.submit_order("u001", submit_request())
    assert order.status is TravelOrderStatus.SUBMITTED and order.approval_id
    assert business.submit_order("u001", submit_request()).order_id == order.order_id
    assert business.submit_order("u001", submit_request("another-key")).order_id == order.order_id
    with pytest.raises(BusinessError) as error:
        business.submit_order("u001", submit_request("submit-1", purpose="不同事由"))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert business.get_approval("u001", order.approval_id).status is ApprovalStatus.PENDING
    with pytest.raises(BusinessError):
        business.get_order("u002", order.order_id)

    with pytest.raises(BusinessError) as error:
        business.decide_approval(admin("USER"), order.approval_id, AdminDecisionRequest(decision="agree"))
    assert error.value.status_code == 403
    approval = business.decide_approval(admin(), order.approval_id, AdminDecisionRequest(decision="agree"))
    assert approval.status is ApprovalStatus.APPROVED
    assert business.get_order("u001", order.order_id).status is TravelOrderStatus.APPROVED
    assert business.decide_approval(admin(), order.approval_id, AdminDecisionRequest(decision="agree")).status is ApprovalStatus.APPROVED
    with pytest.raises(BusinessError):
        business.decide_approval(admin(), order.approval_id, AdminDecisionRequest(decision="refuse"))

    with pytest.raises(BusinessError):
        business.modify_order("u001", order.order_id, ModifyTravelRequest(
            confirmed=True, idempotency_key="modify-no-force", changes=TravelOrderPatch(destination="杭州"),
        ))
    changed = business.modify_order("u001", order.order_id, ModifyTravelRequest(
        confirmed=True, idempotency_key="modify-1", force=True,
        changes=TravelOrderPatch(destination="杭州"),
    ))
    assert changed.status is TravelOrderStatus.SUBMITTED and changed.approval_id != order.approval_id
    history = business.list_approvals_for_order("u001", order.order_id)
    assert [record.status for record in history] == [ApprovalStatus.PENDING, ApprovalStatus.CANCELLED]
    with pytest.raises(BusinessError):
        business.cancel_order("u002", order.order_id, CancelTravelRequest(confirmed=True, idempotency_key="cross"))
    cancelled = business.cancel_order("u001", order.order_id, CancelTravelRequest(confirmed=True, idempotency_key="cancel-1"))
    assert cancelled.cancelled and cancelled.approval_cancelled
    assert business.get_order("u001", order.order_id).status is TravelOrderStatus.CANCELLED
    assert business.cancel_order("u001", order.order_id, CancelTravelRequest(confirmed=True, idempotency_key="cancel-1")) == cancelled


def test_active_order_fingerprint_is_database_unique_and_rejection_releases_it(business):
    order = business.submit_order("u001", submit_request("unique-1"))
    with business.repository.transaction() as session:
        fingerprint = business.repository.order(session, "u001", order.order_id).active_fingerprint
        assert fingerprint
    with pytest.raises(IntegrityError):
        with business.repository.transaction() as session:
            session.add(TravelOrderModel(
                order_id="duplicate", user_id="u001", **draft(),
                status="SUBMITTED", approval_id=None, active_fingerprint=fingerprint,
                plan_html_url=None, created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            ))
    business.decide_approval(admin(), order.approval_id, AdminDecisionRequest(decision="refuse"))
    with business.repository.transaction() as session:
        assert business.repository.order(session, "u001", order.order_id).active_fingerprint is None
    renewed = business.submit_order("u001", submit_request("unique-2"))
    assert renewed.order_id != order.order_id


def test_conflict_detector_covers_overlap_exact_connection_and_missing_transport():
    start = datetime(2026, 11, 10, 9)
    candidate = TripSegment(order_id="new", departure_city="北京", destination="上海", start_at=start, end_at=start + timedelta(hours=3))
    same = TripSegment(order_id="same", departure_city="北京", destination="上海", start_at=start + timedelta(hours=1), end_at=start + timedelta(hours=4))
    cross = TripSegment(order_id="cross", departure_city="广州", destination="深圳", start_at=start + timedelta(hours=1), end_at=start + timedelta(hours=4))
    too_short = TripSegment(order_id="short", departure_city="杭州", destination="苏州", start_at=start + timedelta(hours=5), end_at=start + timedelta(hours=7))
    unknown = TripSegment(order_id="unknown", departure_city="拉萨", destination="成都", start_at=start + timedelta(hours=4), end_at=start + timedelta(hours=5))
    travel = lambda a, b: 180 if frozenset((a, b)) == frozenset(("上海", "杭州")) else None
    report = detect_conflicts(candidate, [same, cross, too_short, unknown], travel)
    assert {item.order_id: item.severity for item in report.conflicts} == {"same": "LOW", "cross": "HIGH", "short": "HIGH"}
    assert report.uncertain and "unknown" in report.uncertain[0]
    exactly_enough = TripSegment(order_id="exact", departure_city="杭州", destination="苏州", start_at=start + timedelta(hours=6), end_at=start + timedelta(hours=7))
    assert detect_conflicts(candidate, [exactly_enough], travel).conflicts == []
    with pytest.raises(ValidationError):
        TripSegment(
            order_id="aware", departure_city="北京", destination="上海",
            start_at=start.replace(tzinfo=timezone.utc),
            end_at=(start + timedelta(hours=1)).replace(tzinfo=timezone.utc),
        )


class FixedBookingGateway:
    def __init__(self, accepted: bool):
        self.accepted = accepted
        self.calls = 0

    def cancel(self, booking, reason):
        self.calls += 1
        return self.accepted


def test_booking_is_user_scoped_and_supplier_failure_does_not_change_local_status(business):
    with business.repository.transaction() as session:
        session.add(BookingRecordModel(
            booking_id="bk-1", user_id="u001", conversation_id=None,
            travel_order_id=None, biz_type="FLIGHT", platform="simulated",
            external_order_no="external-1", status="CONFIRMED", external_status=None,
            payment_status="PAID", title="北京到上海", total_amount=Decimal("1200"),
            currency="CNY", contact_name=None, contact_phone=None, start_time=None,
            end_time=None, detail=None, remark=None, booked_at=None,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            updated_at=datetime.now(timezone.utc).replace(tzinfo=None), deleted=0,
        ))
    assert business.list_bookings("u002") == []
    assert len(business.list_bookings("u001", status=BookingStatus.CONFIRMED)) == 1
    with pytest.raises(BusinessError):
        business.get_booking("u002", "bk-1")
    business.booking_gateway = FixedBookingGateway(False)
    request = BookingCancelRequest(confirmed=True, idempotency_key="bk-cancel", reason="行程调整")
    with pytest.raises(BusinessError) as error:
        business.cancel_booking("u001", "bk-1", request)
    assert error.value.code == "SUPPLIER_CANCEL_FAILED"
    assert business.get_booking("u001", "bk-1").status is BookingStatus.CONFIRMED
    business.booking_gateway = FixedBookingGateway(True)
    result = business.cancel_booking("u001", "bk-1", request)
    assert result.cancelled and result.supplier_cancelled
    assert business.cancel_booking("u001", "bk-1", request) == result
    assert business.get_booking("u001", "bk-1").external_status == "CANCELLED"
    repeated_new_key = business.cancel_booking(
        "u001", "bk-1", BookingCancelRequest(confirmed=True, idempotency_key="bk-cancel-new", reason="复查"),
    )
    assert repeated_new_key.supplier_cancelled
    with pytest.raises(BusinessError) as error:
        business.cancel_booking(
            "u001", "bk-1", BookingCancelRequest(confirmed=True, idempotency_key="bk-cancel-new", reason="另一原因"),
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert business.booking_gateway.calls == 1

    with business.repository.transaction() as session:
        session.add(BookingRecordModel(
            booking_id="bk-local-cancelled", user_id="u001", conversation_id=None,
            travel_order_id=None, biz_type="FLIGHT", platform="simulated",
            external_order_no="external-unconfirmed", status="CANCELLED", external_status=None,
            payment_status="PAID", title="仅本地已取消", total_amount=Decimal("800"),
            currency="CNY", contact_name=None, contact_phone=None, start_time=None,
            end_time=None, detail=None, remark=None, booked_at=None,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            updated_at=datetime.now(timezone.utc).replace(tzinfo=None), deleted=0,
        ))
    unknown_supplier = business.cancel_booking(
        "u001", "bk-local-cancelled",
        BookingCancelRequest(confirmed=True, idempotency_key="local-cancelled"),
    )
    assert unknown_supplier.cancelled and not unknown_supplier.supplier_cancelled
