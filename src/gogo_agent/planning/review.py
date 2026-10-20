"""053/054 行程审核：客观六维确定性检查、有限次数的主观评估与确定性仲裁。

审核只读计划与代表方案，不修改差旅单、审批状态，也不触发供应商下单。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from .models import (
    FinalReview, HotelCandidate, PlanPolicy, PlanView, Proposal, ReviewCheck, ReviewIssue,
    ReviewResult, ReviewSource, ReviewVerdict, TransportCandidate, TransportType,
)
from .scoring import is_cabin_compliant, parse_dt

SUBJECTIVE_TIMEOUT_SECONDS = 30.0
MAX_HOTEL_DISTANCE_KM = 15.0
RED_EYE_DEPARTURE_HOUR = 22
RED_EYE_ARRIVAL_HOUR = 6

DIMENSION_COMPLETENESS = "行程完整性"
DIMENSION_TIMING = "时间安排合理性"
DIMENSION_TRIP = "出发地与目的地核实"
DIMENSION_BUDGET = "预算合规性"
DIMENSION_TRANSPORT = "交通舱位合规性"
DIMENSION_ROUTE = "路径规划合理性"


class ReviewError(ValueError):
    """审核前置条件不满足时的明确错误。"""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class ApprovedTrip(BaseModel):
    """审批单上的出发地与目的地，用于核实；未关联差旅单时不传。"""

    model_config = ConfigDict(extra="forbid")

    origin: str = Field(..., min_length=1, description="审批单出发城市")
    destination: str = Field(..., min_length=1, description="审批单目的城市")


@dataclass(frozen=True)
class Itinerary:
    """审核输入：由代表方案展开的去程、住宿、返程与用户条件，只读。"""

    origin: str
    destination: str
    departure_date: date
    return_date: date
    outbound: TransportCandidate
    hotel: HotelCandidate
    inbound: TransportCandidate
    total_price: float
    budget: float | None
    preferences: str = ""


def itinerary_from_plan(plan: PlanView, proposal: Proposal) -> Itinerary:
    """把代表方案展开为审核输入；不接受模型自报的 JSON。"""
    conditions = plan.conditions
    return Itinerary(
        origin=conditions.origin, destination=conditions.destination,
        departure_date=conditions.departure_date, return_date=conditions.return_date,
        outbound=proposal.outbound, hotel=proposal.hotel, inbound=proposal.inbound,
        total_price=proposal.metrics.total_price, budget=conditions.budget,
        preferences=conditions.preferences,
    )


def city_matches(actual: str, expected: str) -> bool:
    """城市名宽松匹配：任一包含另一个即视为一致（与 Java ObjectiveReviewer 一致）。"""
    return actual.strip() == expected.strip() or expected.strip() in actual or actual.strip() in expected


@dataclass
class _Dimension:
    """单个检查维度的问题收集；最严重的问题决定该维度判定。"""

    name: str
    problems: list[ReviewIssue] = field(default_factory=list)

    def add(self, severity: ReviewVerdict, field_name: str, evidence: str, suggestion: str) -> None:
        self.problems.append(ReviewIssue(
            dimension=self.name, severity=severity, field=field_name, evidence=evidence, suggestion=suggestion,
        ))

    def check(self, ok_detail: str) -> ReviewCheck:
        if not self.problems:
            return ReviewCheck(dimension=self.name, status=ReviewVerdict.PASS, detail=ok_detail)
        status = ReviewVerdict.FAIL if any(p.severity == ReviewVerdict.FAIL for p in self.problems) else ReviewVerdict.WARNING
        return ReviewCheck(dimension=self.name, status=status, detail="；".join(p.evidence for p in self.problems))


def _worst(verdicts: Sequence[ReviewVerdict]) -> ReviewVerdict:
    if any(v == ReviewVerdict.FAIL for v in verdicts):
        return ReviewVerdict.FAIL
    if any(v == ReviewVerdict.WARNING for v in verdicts):
        return ReviewVerdict.WARNING
    return ReviewVerdict.PASS


def objective_review(
    itinerary: Itinerary, policy: PlanPolicy, approved: ApprovedTrip | None, today: date,
) -> ReviewResult:
    """客观六维检查。硬规则（空字段、日期冲突、超预算、出发地不匹配）判 fail；其余按 Java 判 warning。"""
    completeness = _Dimension(DIMENSION_COMPLETENESS)
    timing = _Dimension(DIMENSION_TIMING)
    trip = _Dimension(DIMENSION_TRIP)
    budget = _Dimension(DIMENSION_BUDGET)
    transport = _Dimension(DIMENSION_TRANSPORT)
    route = _Dimension(DIMENSION_ROUTE)
    it = itinerary
    fail, warn = ReviewVerdict.FAIL, ReviewVerdict.WARNING

    # 1 完整性：字段齐全，时间与价格可用
    for label, leg, field_name in (("去程", it.outbound, "outbound"), ("返程", it.inbound, "inbound")):
        if leg.price <= 0:
            completeness.add(fail, field_name, f"{label}价格无效（{leg.price}）", "重新搜索或剔除该候选")
        if parse_dt(leg.departure_time) is None or parse_dt(leg.arrival_time) is None:
            completeness.add(fail, field_name, f"{label}时间无法解析", "核对供应商返回的时间格式")
    if not it.origin.strip():
        completeness.add(fail, "origin", "出发地为空", "补充出发城市")
    if not it.destination.strip():
        completeness.add(fail, "destination", "目的地为空", "补充目的城市")
    if it.hotel.nights < 1:
        completeness.add(fail, "hotel.nights", "住宿晚数少于 1", "补充入住晚数")

    # 2 时间：日期顺序、衔接、晚数、提前预订与红眼航班
    if it.return_date <= it.departure_date:
        timing.add(fail, "return_date", f"返程日期 {it.return_date} 不晚于去程日期 {it.departure_date}", "调整返程日期")
    outbound_arrival = parse_dt(it.outbound.arrival_time)
    inbound_departure = parse_dt(it.inbound.departure_time)
    if outbound_arrival is not None and inbound_departure is not None and inbound_departure <= outbound_arrival:
        timing.add(fail, "inbound.departure_time", "返程出发不晚于去程到达，行程时间冲突", "选择更晚的返程班次")
    if it.departure_date < today:
        timing.add(warn, "departure_date", f"去程日期 {it.departure_date} 早于今天 {today}", "确认出发日期")
    lead_days = (it.departure_date - today).days
    if policy.advance_booking_days is not None and lead_days < policy.advance_booking_days:
        timing.add(warn, "departure_date", f"距出发仅 {lead_days} 天，政策要求提前 {policy.advance_booking_days} 天预订", "调整出发日期或说明例外原因")
    nights = (it.return_date - it.departure_date).days
    if it.hotel.nights != nights:
        timing.add(warn, "hotel.nights", f"住宿 {it.hotel.nights} 晚与日期区间 {nights} 晚不一致", "同步住宿晚数")
    for label, leg, field_name in (("去程", it.outbound, "outbound"), ("返程", it.inbound, "inbound")):
        if leg.type == TransportType.FLIGHT:
            dep, arr = parse_dt(leg.departure_time), parse_dt(leg.arrival_time)
            if dep is not None and arr is not None and (dep.hour >= RED_EYE_DEPARTURE_HOUR or arr.hour < RED_EYE_ARRIVAL_HOUR):
                timing.add(warn, field_name, f"{label}为红眼航班", "考虑白天班次")

    # 3 出发地与目的地：与审批单一致
    if approved is None:
        trip.add(warn, "approved", "未关联差旅单，未核实审批出发地与目的地", "关联本人差旅单后再审核")
    else:
        if not city_matches(it.origin, approved.origin):
            trip.add(fail, "origin", f"出发地「{it.origin}」与审批单「{approved.origin}」不一致", "改为审批单出发地或另行申请")
        if not city_matches(it.destination, approved.destination):
            trip.add(fail, "destination", f"目的地「{it.destination}」与审批单「{approved.destination}」不一致", "改为审批单目的地或另行申请")

    # 4 预算：超出政策硬上限、审批阈值或用户预算均拦截
    if policy.hotel_limit is not None and it.hotel.price_per_night > policy.hotel_limit:
        budget.add(fail, "hotel.price_per_night", f"酒店单晚 ¥{it.hotel.price_per_night:g} 超过政策上限 ¥{policy.hotel_limit:g}", "选择不超过上限的酒店")
    if policy.hotel_star_limit is not None and it.hotel.star_rating is not None and it.hotel.star_rating > policy.hotel_star_limit:
        budget.add(warn, "hotel.star_rating", f"酒店星级 {it.hotel.star_rating} 超过政策上限 {policy.hotel_star_limit} 星", "选择不高于政策星级的酒店")
    if policy.approval_threshold is not None and it.total_price > policy.approval_threshold:
        budget.add(fail, "total_price", f"总价 ¥{it.total_price:.0f} 超过审批阈值 ¥{policy.approval_threshold:g}，需额外审批", "降低总价或走额外审批")
    if it.budget is not None and it.total_price > it.budget:
        budget.add(fail, "total_price", f"总价 ¥{it.total_price:.0f} 超过用户预算 ¥{it.budget:g}", "提高预算或选择更便宜的组合")

    # 5 舱位：不高于政策允许的最高等级
    for label, leg, field_name in (("去程", it.outbound, "outbound"), ("返程", it.inbound, "inbound")):
        allowed = policy.train_seat_class if leg.type == TransportType.TRAIN else policy.flight_class
        cabin = (leg.cabin_class or "").strip()
        if cabin and allowed and not is_cabin_compliant(cabin, allowed):
            transport.add(warn, f"{field_name}.cabin_class", f"{label}舱位「{cabin}」超出政策允许的「{allowed}」", "降级舱位或在说明中注明原因")

    # 6 路径：交通端点与行程城市一致，酒店距离合理
    if not city_matches(it.outbound.destination, it.destination):
        route.add(warn, "outbound.destination", f"去程到达「{it.outbound.destination}」与目的地「{it.destination}」不一致", "核对去程班次")
    if not city_matches(it.inbound.origin, it.destination):
        route.add(warn, "inbound.origin", f"返程出发「{it.inbound.origin}」与目的地「{it.destination}」不一致", "核对返程班次")
    if it.hotel.distance_to_dest_km is not None and it.hotel.distance_to_dest_km > MAX_HOTEL_DISTANCE_KM:
        route.add(warn, "hotel.distance_to_dest_km", f"酒店距目的地约 {it.hotel.distance_to_dest_km:g} km，超过 {MAX_HOTEL_DISTANCE_KM:g} km", "选择更近的住宿")

    dimensions = [completeness, timing, trip, budget, transport, route]
    checks = [
        completeness.check("字段齐全，时间与价格可用"),
        timing.check("日期顺序与衔接正常"),
        trip.check("出发地与目的地与审批单一致" if approved else "未关联差旅单"),
        budget.check(f"预算合规，总价 ¥{it.total_price:.0f}"),
        transport.check("舱位符合政策或政策未限定"),
        route.check("交通端点与行程城市一致"),
    ]
    issues = [issue for dim in dimensions for issue in dim.problems]
    return ReviewResult(
        source=ReviewSource.OBJECTIVE, verdict=_worst([c.status for c in checks]),
        checks=checks, issues=issues, details={"dimension_count": len(checks)},
    )


class SubjectiveAssessor(Protocol):
    """单个主观评估器协议：每个评估器只允许一次调用，结果必须是结构化 ReviewResult。"""

    source: ReviewSource

    async def assess(self, itinerary: Itinerary) -> ReviewResult:
        """对输入方案给出主观结论；不得修改计划或业务数据。"""
        ...


def _unavailable(source: ReviewSource, reason: str) -> ReviewResult:
    return ReviewResult(
        source=source, verdict=ReviewVerdict.WARNING, available=False,
        issues=[ReviewIssue(
            dimension=source.value, severity=ReviewVerdict.WARNING,
            evidence=f"主观评估不可用（{reason}）", suggestion="稍后重试，或由人工复核该维度",
        )],
    )


async def _run_assessor(assessor: SubjectiveAssessor, itinerary: Itinerary) -> ReviewResult:
    """超时或异常都转换为可见的不可用结果，且不掩盖客观硬规则。"""
    try:
        async with asyncio.timeout(SUBJECTIVE_TIMEOUT_SECONDS):
            return await assessor.assess(itinerary)
    except TimeoutError:
        return _unavailable(assessor.source, "超时")
    except Exception as exc:  # 只记录异常类型，避免把模型或供应商内容带入结论
        return _unavailable(assessor.source, type(exc).__name__)


def arbitrate(objective: ReviewResult, subjective: Sequence[ReviewResult]) -> ReviewResult:
    """确定性仲裁：客观 fail 不可被主观 pass 覆盖；主观不可用或告警只能使结论至少为 warning。"""
    issues = list(objective.issues)
    if objective.verdict == ReviewVerdict.FAIL:
        issues.append(ReviewIssue(
            dimension="仲裁", severity=ReviewVerdict.FAIL,
            evidence="客观硬规则未通过，主观评估不能覆盖该结论", suggestion="按客观问题整改后重新审核",
        ))
    if not subjective:
        issues.append(ReviewIssue(
            dimension="仲裁", severity=ReviewVerdict.WARNING,
            evidence="未注入主观评估器，结论不包含主观维度", suggestion="注入三个主观评估器后再审核",
        ))
    for result in subjective:
        issues.extend(result.issues)
    verdicts = [objective.verdict, *(s.verdict for s in subjective)]
    if not subjective:
        verdicts.append(ReviewVerdict.WARNING)
    verdict = _worst(verdicts)
    return ReviewResult(
        source=ReviewSource.ARBITRATOR, verdict=verdict, issues=issues,
        details={"objective_verdict": objective.verdict.value,
                 "subjective_unavailable": [s.source.value for s in subjective if not s.available]},
    )


async def review_plan(
    plan: PlanView, *, proposal_tag: str, assessors: Sequence[SubjectiveAssessor],
    approved: ApprovedTrip | None, today: date, review_id: str, now: datetime, plan_version: int,
) -> FinalReview:
    """对指定代表方案执行完整审核；结论绑定 plan_version，由服务层在保存时确定。"""
    if plan.result is None or not plan.result.proposals:
        raise ReviewError("EMPTY_PLAN", "计划没有可审核的方案，请先计算方案")
    proposal = next((p for p in plan.result.proposals if proposal_tag in p.tags), None)
    if proposal is None:
        raise ReviewError("PROPOSAL_NOT_FOUND", f"没有标签为「{proposal_tag}」的代表方案", 404)
    itinerary = itinerary_from_plan(plan, proposal)
    objective = objective_review(itinerary, plan.result.policy, approved, today)
    subjective = list(await asyncio.gather(*(_run_assessor(a, itinerary) for a in assessors)))
    arbitration = arbitrate(objective, subjective)
    return FinalReview(
        review_id=review_id, plan_id=plan.plan_id, plan_version=plan_version, proposal_tag=proposal_tag,
        verdict=arbitration.verdict, needs_rework=arbitration.verdict == ReviewVerdict.FAIL,
        objective=objective, subjective=subjective, arbitration=arbitration, reviewed_at=now,
    )
