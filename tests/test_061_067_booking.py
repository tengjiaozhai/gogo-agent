"""061–067 预订与工具可靠性的针对性验收：门禁、唯一下单、未知结果、取消、重试、并行与熔断。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gogo_agent.auth.models import UserAccount
from gogo_agent.booking.agent import BOOKING_SYSTEM_PROMPT, build_booking_agent
from gogo_agent.booking.reliability import (
    CircuitBreaker, CircuitOpenError, CircuitState, RetryPolicy, ToolBreakerRegistry,
    call_read_with_retry, search_in_parallel,
)
from gogo_agent.booking.service import BookingError, BookingService, booking_id_for
from gogo_agent.booking.supplier import SimulatedSupplier, SupplierCancelGateway
from gogo_agent.booking.tools import build_booking_toolkit
from gogo_agent.db.base import Base
from gogo_agent.db.models import BookingRecordModel, UserProfileModel
from gogo_agent.planning.review import ApprovedTrip
from gogo_agent.planning.models import PlanConditions
from gogo_agent.planning.service import PlanService
from gogo_agent.planning.simulated import SIMULATED_POLICY, simulated_assessors, simulated_provider, simulated_search, StaticAssessor
from gogo_agent.planning.store import create_plan_repository
from gogo_agent.planning.tools import build_plan_toolkit
from gogo_agent.request_context import RequestContext
from gogo_agent.travel.models import AdminDecisionRequest, SubmitTravelRequest
from gogo_agent.travel.repository import BusinessRepository
from gogo_agent.travel.service import TravelBusinessService
from gogo_agent.travel.tools import build_manage_toolkit
from agentscope.state import AgentState
from gogo_agent.chat.executor import _FallbackMockModel
from gogo_agent.planning.models import ReviewSource

TODAY = date(2026, 10, 11)
CONDITIONS = PlanConditions(
    origin="北京", destination="上海", departure_date=date(2026, 10, 20), return_date=date(2026, 10, 22),
)
APPROVED = ApprovedTrip(origin="北京", destination="上海")
ADMIN = UserAccount(user_id="u_001", username="admin", password_hash="x", role="ADMIN")


class Stack:
    """一套隔离的阶段 D 业务库、阶段 E 计划库和模拟供应商。"""

    def __init__(self, modes: list[str] | None = None) -> None:
        engine = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.engine = engine
        self.repo = BusinessRepository(sessionmaker(bind=engine, expire_on_commit=False))
        self.supplier = SimulatedSupplier(modes=list(modes or []))
        self.travel = TravelBusinessService(self.repo, booking_gateway=SupplierCancelGateway(self.supplier))
        with self.repo.transaction() as session:
            session.add(UserProfileModel(user_id="u001", level="P6"))
        counter = iter(range(1, 10_000))
        self.plans = PlanService(
            create_plan_repository(), today=lambda: TODAY, id_factory=lambda: f"p{next(counter):04d}",
        )
        self.service = BookingService(self.travel, self.plans, self.supplier, sleep=lambda _s: None)

    def close(self) -> None:
        self.engine.dispose()

    def booking_rows(self, user_id: str = "u001") -> list[BookingRecordModel]:
        with self.repo.transaction() as session:
            return list(session.scalars(select(BookingRecordModel).where(BookingRecordModel.user_id == user_id)))


@pytest.fixture
def stack():
    box = Stack()
    yield box
    box.close()


def run(coro):
    return asyncio.run(coro)


def approved_order(stack: Stack, idem: str = "order-1") -> str:
    order = stack.travel.submit_order("u001", SubmitTravelRequest(
        destination="上海", departure_city="北京", departure_date=date(2026, 10, 20),
        return_date=date(2026, 10, 22), purpose="客户拜访", confirmed=True, idempotency_key=idem,
    ))
    assert order.approval_id is not None
    stack.travel.decide_approval(ADMIN, order.approval_id, AdminDecisionRequest(decision="agree"))
    return order.order_id


def ready_plan(stack: Stack, order_id: str | None, *, approved: ApprovedTrip | None = APPROVED) -> str:
    plan = stack.plans.create("u001", CONDITIONS, order_id=order_id)
    stack.plans.attach_candidates("u001", plan.plan_id, simulated_search(CONDITIONS))
    stack.plans.compute("u001", plan.plan_id, policy=SIMULATED_POLICY)
    run(stack.plans.review("u001", plan.plan_id, proposal_tag="综合最佳",
                           assessors=simulated_assessors(), approved=approved))
    return plan.plan_id


def ready_stack(modes: list[str] | None = None) -> tuple[Stack, str, str]:
    stack = Stack(modes)
    order_id = approved_order(stack)
    plan_id = ready_plan(stack, order_id)
    return stack, plan_id, order_id


def expected_amount(stack: Stack, plan_id: str, leg: str) -> Decimal:
    plan = stack.plans.get("u001", plan_id)
    proposal = next(p for p in plan.result.proposals if "综合最佳" in p.tags)
    if leg == "hotel":
        return Decimal(f"{proposal.hotel.price_per_night * proposal.hotel.nights:.2f}")
    candidate = proposal.outbound if leg == "outbound" else proposal.inbound
    return Decimal(f"{candidate.price:.2f}")


# ============================ 061 独立预订智能体的边界 ============================


def test_061_planning_and_review_never_book(stack: Stack):
    ready_plan(stack, approved_order(stack))
    assert stack.supplier.create_calls == 0
    assert stack.booking_rows() == []


def test_061_booking_toolkit_has_no_writable_tool_and_no_plan_or_review_mutation(stack: Stack):
    toolkit = build_booking_toolkit(stack.service, RequestContext(user_id="u001", session_id="s", request_id="r"))
    names = {item["function"]["name"] for item in run(toolkit.get_tool_schemas())}
    assert names == {"prepare_booking", "query_booking"}
    for item in run(toolkit.get_tool_schemas()):
        assert "user_id" not in item["function"]["parameters"]["properties"]


def test_061_writable_tools_are_disjoint_across_master_planning_and_booking_toolkits(stack: Stack):
    ctx = RequestContext(user_id="u001", session_id="s", request_id="r")

    async def writable(toolkit) -> set[str]:
        names = [item["function"]["name"] for item in await toolkit.get_tool_schemas()]
        result = set()
        for name in names:
            tool = await toolkit.get_tool(name)
            if not tool.is_read_only:
                result.add(name)
        return result

    plan_w = run(writable(build_plan_toolkit(stack.plans, ctx, search_provider=simulated_provider, assessors=[])))
    book_w = run(writable(build_booking_toolkit(stack.service, ctx)))
    manage_w = run(writable(build_manage_toolkit(stack.travel, ctx)))
    assert book_w == set()
    assert plan_w.isdisjoint(manage_w) and plan_w.isdisjoint(book_w)
    assert "book_flight" not in plan_w | manage_w | book_w


def test_061_booking_agent_is_assembled_with_policy_prompt(stack: Stack):
    toolkit = build_booking_toolkit(stack.service, RequestContext(user_id="u001", session_id="s", request_id="r"))
    agent = build_booking_agent(model=_FallbackMockModel(), toolkit=toolkit, state=AgentState(session_id="s"))
    assert agent.name == "BookingAgent"
    assert "不能自行下单" in BOOKING_SYSTEM_PROMPT


# ============================ 062 下单门禁与成功路径 ============================


def test_062_unconfirmed_booking_is_refused_without_supplier_call(stack: Stack):
    plan_id = ready_plan(stack, approved_order(stack))
    with pytest.raises(BookingError) as caught:
        stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=False)
    assert caught.value.code == "CONFIRMATION_REQUIRED"
    assert stack.supplier.create_calls == 0


def test_062_gates_refuse_unapproved_foreign_unreviewed_and_changed_plans(stack: Stack):
    # 差旅单未审批
    order = stack.travel.submit_order("u001", SubmitTravelRequest(
        destination="上海", departure_city="北京", departure_date=date(2026, 10, 20),
        return_date=date(2026, 10, 22), purpose="客户拜访", confirmed=True, idempotency_key="pending-order",
    ))
    plan_id = ready_plan(stack, order.order_id)
    with pytest.raises(BookingError) as not_approved:
        stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
    assert not_approved.value.code == "APPROVAL_NOT_APPROVED"

    # 计划不属于当前用户
    stack2, plan2, _ = ready_stack()
    try:
        with pytest.raises(BookingError) as foreign:
            stack2.service.book("u002", plan2, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        assert foreign.value.code == "PLAN_NOT_FOUND"
        # 审核没有通过（客观硬规则失败：出发地与审批单不一致）
        stack3 = Stack()
        order3 = approved_order(stack3)
        plan3 = ready_plan(stack3, order3, approved=ApprovedTrip(origin="广州", destination="上海"))
        with pytest.raises(BookingError) as failed_review:
            stack3.service.book("u001", plan3, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        assert failed_review.value.code == "REVIEW_NOT_PASSED"
        stack3.close()
        # 审核之后内容变化：旧审核不能用于新内容
        stack2.plans.compute("u001", plan2, policy=SIMULATED_POLICY, weather_summary="晴")
        with pytest.raises(BookingError) as stale:
            stack2.service.book("u001", plan2, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        assert stale.value.code == "REVIEW_REQUIRED"
    finally:
        stack2.close()
    assert stack.supplier.create_calls == 0


def test_062_plan_without_linked_order_cannot_book(stack: Stack):
    plan_id = ready_plan(stack, None)
    with pytest.raises(BookingError) as caught:
        stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
    assert caught.value.code == "APPROVAL_REQUIRED"


def test_062_successful_booking_matches_supplier_and_local_record(stack: Stack):
    stack, plan_id, _ = ready_stack()
    try:
        outcome = stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        assert outcome.resolution == "CONFIRMED" and outcome.external_order_no == "SUP000001"
        assert outcome.amount == expected_amount(stack, plan_id, "outbound")
        rows = stack.booking_rows()
        assert len(rows) == 1
        assert rows[0].status == "CONFIRMED" and rows[0].external_order_no == "SUP000001"
        assert stack.supplier.orders[rows[0].booking_id].order_no == "SUP000001"
    finally:
        stack.close()


def test_062_rejected_booking_is_failed_and_replay_does_not_resend(stack: Stack):
    stack, plan_id, _ = ready_stack(modes=["reject"])
    try:
        first = stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="hotel", confirmed=True)
        again = stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="hotel", confirmed=True)
        assert first.resolution == "FAILED" and again.resolution == "FAILED"
        assert stack.supplier.create_calls == 1
        assert stack.booking_rows()[0].status == "FAILED"
        assert "SOLD_OUT" in (stack.booking_rows()[0].external_status or "")
    finally:
        stack.close()


# ============================ 063 取消与唯一本地记录 ============================


def test_063_flight_cancel_calls_supplier_once_and_keeps_one_record(stack: Stack):
    stack, plan_id, _ = ready_stack()
    try:
        outcome = stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        result = stack.service.cancel(
            "u001", outcome.booking_id, idempotency_key="cancel-1", confirmed=True, reason="行程变更",
        )
        assert result.cancelled is True and result.supplier_cancelled is True
        assert stack.supplier.cancel_calls == 1
        rows = stack.booking_rows()
        assert len(rows) == 1 and rows[0].status == "CANCELLED"
        replay = stack.service.cancel(
            "u001", outcome.booking_id, idempotency_key="cancel-1", confirmed=True, reason="行程变更",
        )
        assert replay.cancelled is True and stack.supplier.cancel_calls == 1
    finally:
        stack.close()


def test_063_hotel_cancel_cancels_supplier_first_and_keeps_local_state_on_failure(stack: Stack):
    stack, plan_id, _ = ready_stack()
    try:
        outcome = stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="hotel", confirmed=True)
        del stack.supplier.orders[outcome.booking_id]  # 模拟供应商侧已无此订单
        with pytest.raises(BookingError) as failed:
            stack.service.cancel("u001", outcome.booking_id, idempotency_key="cancel-h", confirmed=True)
        assert failed.value.code == "SUPPLIER_CANCEL_FAILED"
        assert stack.booking_rows()[0].status == "CONFIRMED"
    finally:
        stack.close()


def test_063_hotel_cancel_success_marks_supplier_and_local_cancelled(stack: Stack):
    stack, plan_id, _ = ready_stack()
    try:
        outcome = stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="hotel", confirmed=True)
        result = stack.service.cancel("u001", outcome.booking_id, idempotency_key="cancel-h2", confirmed=True)
        assert result.supplier_cancelled is True
        assert stack.supplier.orders[outcome.booking_id].status == "CANCELLED"
        assert stack.booking_rows()[0].status == "CANCELLED"
    finally:
        stack.close()


def test_063_cancel_of_others_or_missing_booking_fails_and_requires_confirmation(stack: Stack):
    stack, plan_id, _ = ready_stack()
    try:
        outcome = stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        with pytest.raises(BookingError) as foreign:
            stack.service.cancel("u002", outcome.booking_id, idempotency_key="x", confirmed=True)
        assert foreign.value.code == "BOOKING_NOT_FOUND" and foreign.value.status == 404
        with pytest.raises(BookingError) as missing:
            stack.service.cancel("u001", "bk_missing", idempotency_key="y", confirmed=True)
        assert missing.value.code == "BOOKING_NOT_FOUND"
        with pytest.raises(BookingError) as unconfirmed:
            stack.service.cancel("u001", outcome.booking_id, idempotency_key="z", confirmed=False)
        assert unconfirmed.value.code == "CONFIRMATION_REQUIRED"
        assert stack.supplier.cancel_calls == 0
    finally:
        stack.close()


# ============================ 064 数据模型与幂等 ============================


def test_064_duplicate_requests_create_one_record_per_leg(stack: Stack):
    stack, plan_id, order_id = ready_stack()
    try:
        first = stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        second = stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        assert first.booking_id == second.booking_id
        assert stack.supplier.create_calls == 1
        rows = stack.booking_rows()
        assert len(rows) == 1 and rows[0].travel_order_id == order_id
        assert rows[0].currency == "CNY" and isinstance(rows[0].total_amount, Decimal)
    finally:
        stack.close()


def test_064_replanned_leg_cannot_book_twice_until_old_booking_is_cancelled(stack: Stack):
    stack, plan_id, _ = ready_stack()
    try:
        old = stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        stack.plans.compute("u001", plan_id, policy=SIMULATED_POLICY, weather_summary="晴")
        run(stack.plans.review("u001", plan_id, proposal_tag="综合最佳",
                               assessors=simulated_assessors(), approved=APPROVED))
        with pytest.raises(BookingError) as duplicate:
            stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        assert duplicate.value.code == "BOOKING_ALREADY_CONFIRMED"
        assert stack.supplier.create_calls == 1
        stack.service.cancel("u001", old.booking_id, idempotency_key="replan-cancel", confirmed=True)
        new = stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        assert new.booking_id != old.booking_id and new.resolution == "CONFIRMED"
        assert stack.supplier.create_calls == 2
    finally:
        stack.close()


def test_064_booking_identity_depends_on_content_version_and_leg():
    base = booking_id_for("u001", "p1", 3, "综合最佳", "outbound")
    assert base == booking_id_for("u001", "p1", 3, "综合最佳", "outbound")
    assert base != booking_id_for("u001", "p1", 4, "综合最佳", "outbound")
    assert base != booking_id_for("u001", "p1", 3, "综合最佳", "inbound")
    assert base != booking_id_for("u002", "p1", 3, "综合最佳", "outbound")


def test_064_supplier_dedupes_same_request_id():
    supplier = SimulatedSupplier()
    first = supplier.create_order("bk_same", {"amount": "100.00"})
    second = supplier.create_order("bk_same", {"amount": "100.00"})
    assert first == second and len(supplier.orders) == 1


# ============================ 066 重试、未知结果与并行搜索 ============================


def test_066_lost_after_commit_is_resolved_by_lookup_without_second_order():
    stack, plan_id, _ = ready_stack(modes=["lost_after_commit"])
    try:
        outcome = stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        assert outcome.resolution == "CONFIRMED" and outcome.external_order_no == "SUP000001"
        assert stack.supplier.create_calls == 1 and len(stack.supplier.orders) == 1
    finally:
        stack.close()


def test_066_lost_before_commit_stays_unknown_and_retry_never_recreates():
    stack, plan_id, _ = ready_stack(modes=["lost_before_commit"])
    try:
        first = stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        again = stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        assert first.resolution == "UNKNOWN" and again.resolution == "UNKNOWN"
        assert "未宣称成功" in first.message
        assert stack.supplier.create_calls == 1
        assert stack.booking_rows()[0].status == "CREATED"
        assert stack.booking_rows()[0].external_status == "UNKNOWN"
    finally:
        stack.close()


def test_066_read_retry_is_limited_and_only_for_retryable_errors():
    calls = {"n": 0}
    sleeps: list[float] = []

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("临时断开")
        return "ok"

    assert call_read_with_retry(flaky, RetryPolicy(), sleep=sleeps.append) == "ok"
    assert calls["n"] == 3 and sleeps == [3.0, 6.0]

    calls["n"] = 0

    def always_io():
        calls["n"] += 1
        raise TimeoutError("超时")

    with pytest.raises(TimeoutError):
        call_read_with_retry(always_io, RetryPolicy(), sleep=lambda _s: None)
    assert calls["n"] == 3

    calls["n"] = 0

    def bad_value():
        calls["n"] += 1
        raise ValueError("参数错误")

    with pytest.raises(ValueError):
        call_read_with_retry(bad_value, RetryPolicy(), sleep=lambda _s: None)
    assert calls["n"] == 1


def test_066_parallel_search_is_bounded_isolated_and_timed_out():
    async def slow(user_id: str) -> list[str]:
        await asyncio.sleep(0.2)
        return [f"{user_id}-item"]

    async def broken(user_id: str) -> list[str]:
        raise RuntimeError("供应商内部错误细节不应进入结果")

    async def hang(user_id: str) -> list[str]:
        await asyncio.sleep(5)
        return []

    async def quick(user_id: str) -> list[str]:
        return [f"{user_id}-quick"]

    async def scenario():
        started = time.monotonic()
        outcomes = await search_in_parallel(
            "u001", {"a": slow, "b": slow, "c": slow}, concurrency=4, timeout_s=2,
        )
        elapsed = time.monotonic() - started
        mixed = await search_in_parallel(
            "u001", {"ok": quick, "broken": broken, "hang": hang}, concurrency=4, timeout_s=0.1,
        )
        pair = await asyncio.gather(
            search_in_parallel("u001", {"s": slow}), search_in_parallel("u002", {"s": slow}),
        )
        return outcomes, elapsed, mixed, pair

    outcomes, elapsed, mixed, pair = run(scenario())
    assert elapsed < 0.5  # 串行约 0.6 秒
    assert all(item.ok and item.user_id == "u001" for item in outcomes.values())
    assert mixed["ok"].ok and mixed["broken"].error_type == "RuntimeError"
    assert mixed["hang"].error_type == "TimeoutError"
    assert "供应商内部错误" not in json.dumps(mixed["broken"].model_dump(mode="json"), ensure_ascii=False)
    assert pair[0]["s"].user_id == "u001" and pair[1]["s"].user_id == "u002"
    assert pair[0]["s"].items == ["u001-item"] and pair[1]["s"].items == ["u002-item"]


# ============================ 067 工具级熔断 ============================


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_067_breaker_opens_after_three_failures_and_stops_outbound_calls():
    clock = FakeClock()
    registry = ToolBreakerRegistry(clock=clock)
    attempts = {"n": 0}

    async def failing():
        attempts["n"] += 1
        raise OSError("外部服务不可用")

    async def scenario():
        for _ in range(3):
            with pytest.raises(OSError):
                await registry.call("query_weather", failing)
        with pytest.raises(CircuitOpenError):
            await registry.call("query_weather", failing)

    run(scenario())
    assert attempts["n"] == 3
    assert registry.breaker("query_weather").state is CircuitState.OPEN


def test_067_cooldown_allows_single_probe_then_recovers_or_reopens():
    clock = FakeClock()
    breaker = CircuitBreaker(name="query_weather", clock=clock)
    for _ in range(3):
        breaker.on_failure()
    assert breaker.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        breaker.before_call()

    clock.now = 60.0
    breaker.before_call()
    assert breaker.state is CircuitState.HALF_OPEN
    with pytest.raises(CircuitOpenError):
        breaker.before_call()  # 探测进行中，第二个请求被拒绝
    breaker.on_success()
    assert breaker.state is CircuitState.CLOSED

    for _ in range(3):
        breaker.on_failure()
    clock.now = 130.0
    breaker.before_call()
    breaker.on_failure()
    assert breaker.state is CircuitState.OPEN
    clock.now = 150.0
    with pytest.raises(CircuitOpenError):
        breaker.before_call()


def test_067_cancelled_probe_releases_half_open_slot():
    clock = FakeClock()
    registry = ToolBreakerRegistry(clock=clock)
    breaker = registry.breaker("query_weather")
    for _ in range(3):
        breaker.on_failure()
    clock.now = 60.0

    async def hangs():
        await asyncio.sleep(10)

    async def scenario():
        task = asyncio.create_task(registry.call("query_weather", hangs))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async def healthy():
            return "recovered"
        return await registry.call("query_weather", healthy)

    assert run(scenario()) == "recovered"
    assert breaker.state is CircuitState.CLOSED


def test_067_unmonitored_tools_stay_available_when_weather_breaker_is_open():
    clock = FakeClock()
    registry = ToolBreakerRegistry(clock=clock)

    async def failing():
        raise OSError("down")

    async def healthy():
        return "rows"

    async def scenario():
        for _ in range(3):
            with pytest.raises(OSError):
                await registry.call("query_weather", failing)
        return await registry.call("query_booking_record", healthy)

    assert run(scenario()) == "rows"
    assert "query_booking_record" not in registry._breakers


# ============================ 阶段验收的四个场景 ============================


def test_acceptance_success_lost_cancel_and_cross_user_are_counted_and_audited():
    stack, plan_id, _ = ready_stack(modes=["ok", "lost_after_commit"])
    try:
        success = stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        lost = stack.service.book("u001", plan_id, proposal_tag="综合最佳", leg="inbound", confirmed=True)
        cancelled = stack.service.cancel("u001", success.booking_id, idempotency_key="acc-cancel", confirmed=True)
        with pytest.raises(BookingError) as cross:
            stack.service.cancel("u002", lost.booking_id, idempotency_key="acc-cross", confirmed=True)
        assert success.resolution == "CONFIRMED" and lost.resolution == "CONFIRMED"
        assert cancelled.cancelled is True and cross.value.code == "BOOKING_NOT_FOUND"
        assert stack.supplier.create_calls == 2
        assert stack.supplier.cancel_calls == 1
        statuses = sorted(row.status for row in stack.booking_rows())
        assert statuses == ["CANCELLED", "CONFIRMED"]
    finally:
        stack.close()


def test_objective_fail_blocks_booking_even_with_passing_subjective_assessors(stack: Stack):
    order_id = approved_order(stack)
    plan = stack.plans.create("u001", CONDITIONS, order_id=order_id)
    stack.plans.attach_candidates("u001", plan.plan_id, simulated_search(CONDITIONS))
    stack.plans.compute("u001", plan.plan_id, policy=SIMULATED_POLICY)
    assessors = [StaticAssessor(ReviewSource.EXPERIENCE)]
    run(stack.plans.review("u001", plan.plan_id, proposal_tag="综合最佳", assessors=assessors,
                           approved=ApprovedTrip(origin="广州", destination="上海")))
    with pytest.raises(BookingError) as caught:
        stack.service.book("u001", plan.plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
    assert caught.value.code == "REVIEW_NOT_PASSED"
