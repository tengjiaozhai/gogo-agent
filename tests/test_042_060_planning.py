"""042–060 行程规划的针对性验收：步骤推进、方案计算、审核仲裁、版本与归属。"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone

import pytest
from agentscope.agent import Agent

from gogo_agent.planning import steps
from gogo_agent.planning.agent import PLAN_SYSTEM_PROMPT, build_plan_agent
from gogo_agent.planning.models import (
    CandidateSet, DimensionWeights, HotelCandidate, PlanConditions, PlanPolicy, PlanProgress, PlanStepName,
    PlanStepStatus, ReviewSource, ReviewVerdict, TransportCandidate, TransportType,
)
from gogo_agent.planning.review import ApprovedTrip, Itinerary, objective_review
from gogo_agent.planning.scoring import PlanningError, compute_plan
from gogo_agent.planning.service import PlanError, PlanService
from gogo_agent.chat.executor import _FallbackMockModel
from gogo_agent.planning.simulated import SIMULATED_POLICY, StaticAssessor, simulated_assessors, simulated_provider, simulated_search
from gogo_agent.planning.store import PlanVersionConflict, create_plan_repository
from gogo_agent.planning.tools import build_plan_toolkit
from gogo_agent.request_context import RequestContext
from agentscope.state import AgentState

TODAY = date(2026, 10, 11)
CONDITIONS = PlanConditions(
    origin="北京", destination="上海", departure_date=date(2026, 10, 20), return_date=date(2026, 10, 22),
)
APPROVED = ApprovedTrip(origin="北京", destination="上海")
CLOCK_TIME = datetime(2026, 10, 11, 4, 0, tzinfo=timezone.utc)


def make_service() -> PlanService:
    counter = iter(range(1, 10_000))
    return PlanService(
        create_plan_repository(),
        clock=lambda: CLOCK_TIME,
        today=lambda: TODAY,
        id_factory=lambda: f"id{next(counter):04d}",
    )


def leg(id_: str, type_: TransportType, dep: str, arr: str, price: float, *, origin="北京",
        destination="上海", cabin="经济舱", direction=None) -> TransportCandidate:
    return TransportCandidate(
        id=id_, type=type_, departure_time=dep, arrival_time=arr, origin=origin, destination=destination,
        price=price, cabin_class=cabin, direction=direction,
    )


def hotel(id_: str, price: float = 400, nights: int = 2, star: int = 4) -> HotelCandidate:
    return HotelCandidate(id=id_, name=f"酒店{id_}", star_rating=star, price_per_night=price, nights=nights)


def small_candidates(**overrides) -> CandidateSet:
    base = dict(
        transports=[
            leg("F1", TransportType.FLIGHT, "2026-10-20T10:00:00+08:00", "2026-10-20T12:10:00+08:00", 1200, direction="outbound"),
            leg("F2", TransportType.FLIGHT, "2026-10-22T18:00:00+08:00", "2026-10-22T20:10:00+08:00", 1100,
                origin="上海", destination="北京", direction="return"),
        ],
        hotels=[hotel("H1")],
        source="simulated",
        captured_at=CLOCK_TIME,
    )
    base.update(overrides)
    return CandidateSet(**base)


def ctx(user_id: str = "u001") -> RequestContext:
    return RequestContext(user_id=user_id, session_id="s1", request_id="r1")


def run(coro):
    return asyncio.run(coro)


def call_tool(toolkit, name: str, **kwargs) -> dict:
    tool = run(toolkit.get_tool(name))
    return json.loads(run(tool.call(**kwargs)).content[0].text)


# ============================ 042 Plan-and-Execute 步骤 ============================


def test_042_new_plan_starts_at_search_and_search_failure_retries_from_search():
    service = make_service()
    plan = service.create("u001", CONDITIONS)
    assert steps.next_step(plan.progress) == PlanStepName.SEARCH_CANDIDATES

    after_failure = service.record_search_failure("u001", plan.plan_id, "TimeoutError")
    search = after_failure.progress.steps[1]
    assert search.status == PlanStepStatus.FAILED and search.attempts == 1
    assert steps.next_step(after_failure.progress) == PlanStepName.SEARCH_CANDIDATES
    assert not after_failure.progress.terminated


def test_042_repeated_search_failure_terminates_plan_after_three_attempts():
    service = make_service()
    plan = service.create("u001", CONDITIONS)
    for _ in range(3):
        plan = service.record_search_failure("u001", plan.plan_id, "ConnectionError")
    assert plan.progress.terminated
    assert "连续失败 3 次" in (plan.progress.termination_reason or "")
    with pytest.raises(steps.StepTransitionError) as caught:
        steps.complete(plan.progress, PlanStepName.SEARCH_CANDIDATES, data_version=plan.version)
    assert caught.value.code == "PLAN_TERMINATED"


def test_042_changing_dates_reruns_from_search_and_keeps_collected_conditions():
    service = make_service()
    plan = service.create("u001", CONDITIONS)
    plan = service.attach_candidates("u001", plan.plan_id, simulated_search(CONDITIONS))
    plan = service.compute("u001", plan.plan_id, policy=SIMULATED_POLICY)
    assert plan.result is not None

    changed = PlanConditions(
        origin="北京", destination="上海", departure_date=date(2026, 10, 21), return_date=date(2026, 10, 23),
    )
    updated = service.update_conditions("u001", plan.plan_id, changed)
    assert updated.candidates is None and updated.result is None
    assert steps.next_step(updated.progress) == PlanStepName.SEARCH_CANDIDATES
    assert updated.progress.steps[0].status == PlanStepStatus.COMPLETED


def test_042_steps_cannot_complete_out_of_order():
    progress = PlanProgress()
    with pytest.raises(steps.StepTransitionError) as caught:
        steps.complete(progress, PlanStepName.COMPUTE_PROPOSALS, data_version=1)
    assert caught.value.code == "STEP_ORDER"


def test_042_confirmed_external_write_is_recorded_once_and_conflicts_are_rejected():
    progress = steps.record_write(PlanProgress(), "order-key-1", "ext-1")
    assert steps.committed_write(progress, "order-key-1") == "ext-1"
    assert steps.record_write(progress, "order-key-1", "ext-1") == progress
    with pytest.raises(steps.StepTransitionError) as caught:
        steps.record_write(progress, "order-key-1", "ext-2")
    assert caught.value.code == "WRITE_CONFLICT"


# ============================ 044 / 045 确定性方案计算 ============================


def test_044_tool_and_direct_calculation_agree_and_model_cannot_inject_user():
    service = make_service()
    toolkit = build_plan_toolkit(service, ctx(), search_provider=simulated_provider, assessors=[])
    schema_names = {item["function"]["name"] for item in run(toolkit.get_tool_schemas())}
    assert {"create_plan", "search_candidates", "compute_plan", "get_plan", "review_plan"} <= schema_names
    assert not any("order" in name or "book" in name or "cancel" in name for name in schema_names)
    for item in run(toolkit.get_tool_schemas()):
        assert "user_id" not in item["function"]["parameters"]["properties"]

    created = call_tool(toolkit, "create_plan", origin="北京", destination="上海",
                        departure_date="2026-10-20", return_date="2026-10-22")
    plan_id = created["data"]["plan_id"]
    call_tool(toolkit, "search_candidates", plan_id=plan_id)
    computed = call_tool(toolkit, "compute_plan", plan_id=plan_id, policy=SIMULATED_POLICY.model_dump())
    assert computed["success"] is True

    direct = compute_plan(
        simulated_search(CONDITIONS), origin="北京", destination="上海", return_date="2026-10-22",
        policy=SIMULATED_POLICY, scores=None, weights=DimensionWeights(),
        now=CLOCK_TIME,
    )
    stored = service.get("u001", plan_id).result
    assert stored is not None
    assert [p.combo_id for p in stored.proposals] == [p.combo_id for p in direct.proposals]
    assert [p.scores.overall for p in stored.proposals] == [p.scores.overall for p in direct.proposals]


def test_045_red_eye_fastest_option_is_avoided_within_tolerance():
    cands = CandidateSet(
        transports=[
            leg("FR", TransportType.FLIGHT, "2026-10-20T23:00:00+08:00", "2026-10-21T01:00:00+08:00", 500, direction="outbound"),
            leg("FC", TransportType.FLIGHT, "2026-10-20T10:00:00+08:00", "2026-10-20T12:10:00+08:00", 520, direction="outbound"),
            leg("FB", TransportType.FLIGHT, "2026-10-22T18:00:00+08:00", "2026-10-22T20:10:00+08:00", 1100,
                origin="上海", destination="北京", direction="return"),
        ],
        hotels=[hotel("H1")],
        source="simulated",
        captured_at=CLOCK_TIME,
    )
    result = compute_plan(cands, origin="北京", destination="上海", return_date="2026-10-22", policy=PlanPolicy(),
                          now=CLOCK_TIME)
    fastest = next(p for p in result.proposals if "时间最短" in p.tags)
    assert fastest.outbound.id == "FC"
    assert "已避开更快但有体验风险的 FR" in " ".join(fastest.selection_basis)


def test_045_cross_timezone_duration_uses_real_instants_not_wall_clock():
    cands = small_candidates(transports=[
        leg("F1", TransportType.FLIGHT, "2026-10-20T08:00:00+08:00", "2026-10-20T10:30:00+09:00", 1200, direction="outbound"),
        leg("F2", TransportType.FLIGHT, "2026-10-22T18:00:00+08:00", "2026-10-22T20:10:00+08:00", 1100,
            origin="上海", destination="北京", direction="return"),
    ])
    result = compute_plan(cands, origin="北京", destination="上海", return_date="2026-10-22", now=CLOCK_TIME)
    assert result.proposals[0].outbound.id == "F1"
    assert result.proposals[0].metrics.total_transit_min == 90 + 130


def test_045_empty_and_abnormal_inputs_have_explicit_results():
    with pytest.raises(PlanningError) as no_hotel:
        compute_plan(small_candidates(hotels=[]), origin="北京", destination="上海", return_date="2026-10-22", now=CLOCK_TIME)
    assert no_hotel.value.code == "NO_CANDIDATES"

    zero_price = small_candidates(transports=[
        leg("F1", TransportType.FLIGHT, "2026-10-20T10:00:00+08:00", "2026-10-20T12:10:00+08:00", 0, direction="outbound"),
        leg("F2", TransportType.FLIGHT, "2026-10-22T18:00:00+08:00", "2026-10-22T20:10:00+08:00", 1100,
            origin="上海", destination="北京", direction="return"),
    ])
    with pytest.raises(PlanningError) as no_leg:
        compute_plan(zero_price, origin="北京", destination="上海", return_date="2026-10-22", now=CLOCK_TIME)
    assert no_leg.value.code == "NO_LEG"

    late_return = small_candidates(transports=[
        leg("F1", TransportType.FLIGHT, "2026-10-20T10:00:00+08:00", "2026-10-20T12:10:00+08:00", 1200, direction="outbound"),
        leg("F2", TransportType.FLIGHT, "2026-10-20T11:00:00+08:00", "2026-10-20T12:00:00+08:00", 1100,
            origin="上海", destination="北京", direction="return"),
    ])
    with pytest.raises(PlanningError) as filtered:
        compute_plan(late_return, origin="北京", destination="上海", return_date="2026-10-20", now=CLOCK_TIME)
    assert filtered.value.code == "ALL_FILTERED"


# ============================ 053 / 054 审核 ============================


def _full_plan(service: PlanService) -> str:
    plan = service.create("u001", CONDITIONS)
    service.attach_candidates("u001", plan.plan_id, simulated_search(CONDITIONS))
    service.compute("u001", plan.plan_id, policy=SIMULATED_POLICY)
    return plan.plan_id


def test_053_full_plan_passes_with_matching_approval_and_passing_assessors():
    service = make_service()
    plan_id = _full_plan(service)
    final = run(service.review("u001", plan_id, proposal_tag="综合最佳", assessors=simulated_assessors(),
                               approved=APPROVED))
    assert final.verdict == ReviewVerdict.PASS
    assert final.needs_rework is False
    assert [s.source for s in final.subjective] == [
        ReviewSource.EXPERIENCE, ReviewSource.RESILIENCE, ReviewSource.PREFERENCE,
    ]


def test_053_hard_budget_failure_cannot_be_overridden_by_subjective_pass():
    service = make_service()
    plan = service.create("u001", CONDITIONS)
    service.attach_candidates("u001", plan.plan_id, simulated_search(CONDITIONS))
    # 只保留超限酒店 H3（780 > 450），让客观硬规则必然失败
    cands = simulated_search(CONDITIONS).model_copy(update={"hotels": [hotel("H3", price=780, star=5)]})
    service.attach_candidates("u001", plan.plan_id, cands)
    service.compute("u001", plan.plan_id, policy=SIMULATED_POLICY)
    final = run(service.review("u001", plan.plan_id, proposal_tag="综合最佳", assessors=simulated_assessors(),
                               approved=APPROVED))
    assert final.verdict == ReviewVerdict.FAIL and final.needs_rework
    assert any(i.field == "hotel.price_per_night" for i in final.objective.issues)
    assert any("不能覆盖" in issue.evidence for issue in final.arbitration.issues)


def test_053_subjective_exception_is_visible_and_does_not_hide_objective_veto():
    service = make_service()
    plan_id = _full_plan(service)
    assessors = [StaticAssessor(ReviewSource.EXPERIENCE, error=RuntimeError("boom")), *simulated_assessors()[1:]]
    final = run(service.review("u001", plan_id, proposal_tag="综合最佳", assessors=assessors, approved=APPROVED))
    unavailable = [s for s in final.subjective if not s.available]
    assert len(unavailable) == 1 and unavailable[0].verdict == ReviewVerdict.WARNING
    assert "RuntimeError" in unavailable[0].issues[0].evidence
    assert final.verdict == ReviewVerdict.WARNING and not final.needs_rework


def test_053_without_subjective_assessors_never_passes_silently():
    service = make_service()
    plan_id = _full_plan(service)
    final = run(service.review("u001", plan_id, proposal_tag="综合最佳", assessors=[], approved=APPROVED))
    assert final.objective.verdict == ReviewVerdict.PASS
    assert final.verdict == ReviewVerdict.WARNING
    assert any("未注入主观评估器" in issue.evidence for issue in final.arbitration.issues)


def test_053_origin_mismatch_and_missing_approval_have_explicit_outcomes():
    service = make_service()
    plan_id = _full_plan(service)
    mismatch = run(service.review("u001", plan_id, proposal_tag="综合最佳", assessors=simulated_assessors(),
                                  approved=ApprovedTrip(origin="广州", destination="上海")))
    assert mismatch.verdict == ReviewVerdict.FAIL
    assert any(i.field == "origin" for i in mismatch.objective.issues)

    no_approval = run(service.review("u001", plan_id, proposal_tag="综合最佳", assessors=simulated_assessors()))
    assert no_approval.verdict == ReviewVerdict.WARNING
    assert any(i.field == "approved" for i in no_approval.objective.issues)


def test_053_timing_and_budget_rules_on_direct_itinerary():
    itinerary = Itinerary(
        origin="北京", destination="上海", departure_date=date(2026, 10, 20), return_date=date(2026, 10, 22),
        outbound=leg("F1", TransportType.FLIGHT, "2026-10-20T10:00:00+08:00", "2026-10-20T12:10:00+08:00", 1200,
                     direction="outbound"),
        hotel=hotel("H1", price=400, nights=3),
        inbound=leg("F2", TransportType.FLIGHT, "2026-10-20T11:00:00+08:00", "2026-10-20T12:00:00+08:00", 1100,
                    origin="上海", destination="北京"),
        total_price=2700, budget=2000,
    )
    result = objective_review(itinerary, PlanPolicy(hotel_limit=300, approval_threshold=2500), APPROVED, TODAY)
    assert result.verdict == ReviewVerdict.FAIL
    fields = {issue.field for issue in result.issues}
    assert {"inbound.departure_time", "hotel.price_per_night", "total_price", "hotel.nights"} <= fields


# ============================ 060 版本与归属 ============================


def test_060_review_binds_to_version_and_any_later_change_makes_it_stale():
    service = make_service()
    plan_id = _full_plan(service)
    final = run(service.review("u001", plan_id, proposal_tag="综合最佳", assessors=simulated_assessors(),
                               approved=APPROVED))
    current = service.get("u001", plan_id)
    assert current.review_is_current() and final.plan_version == current.content_version

    again = run(service.review("u001", plan_id, proposal_tag="综合最佳", assessors=simulated_assessors(),
                               approved=APPROVED))
    assert again.plan_version == current.content_version
    assert service.get("u001", plan_id).review_is_current()

    changed = service.compute("u001", plan_id, policy=SIMULATED_POLICY, weather_summary="晴")
    assert not changed.review_is_current()
    assert changed.version == current.version + 2 and changed.content_version == current.content_version + 1


def test_060_plans_are_isolated_by_owner_and_cannot_be_reviewed_by_others():
    service = make_service()
    plan_id = _full_plan(service)
    with pytest.raises(PlanError) as read_other:
        service.get("u002", plan_id)
    assert read_other.value.code == "PLAN_NOT_FOUND" and read_other.value.status == 404
    with pytest.raises(PlanError) as review_other:
        run(service.review("u002", plan_id, proposal_tag="综合最佳", assessors=simulated_assessors()))
    assert review_other.value.code == "PLAN_NOT_FOUND"


def test_060_stale_writer_gets_version_conflict_instead_of_overwriting():
    service = make_service()
    plan = service.create("u001", CONDITIONS)
    stale = plan
    service.record_search_failure("u001", plan.plan_id, "TimeoutError")
    with pytest.raises(PlanVersionConflict):
        service.repository.save("u001", stale.model_copy(update={"version": 2}), expected_version=stale.version)


def test_060_review_of_missing_proposal_is_rejected():
    service = make_service()
    plan_id = _full_plan(service)
    with pytest.raises(PlanError) as caught:
        run(service.review("u001", plan_id, proposal_tag="不存在", assessors=simulated_assessors()))
    assert caught.value.code == "PROPOSAL_NOT_FOUND"


# ============================ 043 规划工具与 Agent 装配 ============================


def test_043_missing_fields_request_input_without_creating_plan():
    service = make_service()
    toolkit = build_plan_toolkit(service, ctx(), search_provider=simulated_provider, assessors=[])
    result = call_tool(toolkit, "create_plan", origin="北京", destination="")
    assert result["errorCode"] == "NEEDS_INPUT"
    assert "目的城市" in result["message"] and "去程日期" in result["message"]
    assert service.repository.get("u001", "id0001") is None


def test_043_search_failure_returns_retry_hint_and_records_step():
    service = make_service()

    def broken(_user_id, _conditions):
        raise TimeoutError("供应商超时")

    toolkit = build_plan_toolkit(service, ctx(), search_provider=broken, assessors=[])
    created = call_tool(toolkit, "create_plan", origin="北京", destination="上海",
                        departure_date="2026-10-20", return_date="2026-10-22")
    failed = call_tool(toolkit, "search_candidates", plan_id=created["data"]["plan_id"])
    assert failed["errorCode"] == "SEARCH_FAILED" and "TimeoutError" in failed["message"]
    assert service.get("u001", created["data"]["plan_id"]).progress.steps[1].attempts == 1


def test_043_full_tool_path_reaches_review_without_order_writes():
    service = make_service()
    toolkit = build_plan_toolkit(
        service, ctx(), search_provider=simulated_provider, assessors=simulated_assessors(),
        approved=APPROVED,
    )

    def call(name: str, **kwargs):
        return call_tool(toolkit, name, **kwargs)

    plan_id = call("create_plan", origin="北京", destination="上海", departure_date="2026-10-20",
                   return_date="2026-10-22")["data"]["plan_id"]
    assert call("search_candidates", plan_id=plan_id)["success"] is True
    computed = call("compute_plan", plan_id=plan_id, policy=SIMULATED_POLICY.model_dump())
    assert computed["data"]["candidate_source"] == "simulated"
    assert computed["data"]["proposals"]
    reviewed = call("review_plan", plan_id=plan_id, proposal_tag="综合最佳")
    assert reviewed["data"]["verdict"] == "pass"
    assert reviewed["data"]["next_step"] == "await_confirmation"
    assert service.get("u001", plan_id).order_id is None


def test_043_plan_agent_is_assembled_with_only_planning_tools():
    service = make_service()
    toolkit = build_plan_toolkit(service, ctx(), search_provider=simulated_provider, assessors=[])

    agent = build_plan_agent(model=_FallbackMockModel(), toolkit=toolkit, state=AgentState(session_id="s1"))
    assert isinstance(agent, Agent) and agent.name == "ItineraryPlanAgent"
    assert "没有提交差旅单" in PLAN_SYSTEM_PROMPT


# ============================ 060 计划持久化（跨进程读回） ============================


def test_060_plan_survives_new_repository_instance_over_shared_database(tmp_path):
    """数据库模式下，新建的仓储实例仍能读回计划，覆盖"重启/多 worker 丢计划"的回归。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from gogo_agent.db.base import Base
    from gogo_agent.planning.store import ItineraryPlanModel, PlanRepository

    engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[ItineraryPlanModel.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    writer = PlanService(PlanRepository(factory), clock=lambda: CLOCK_TIME, today=lambda: TODAY, id_factory=lambda: "id060")
    created = writer.create("u001", CONDITIONS)

    reader = PlanService(PlanRepository(factory), clock=lambda: CLOCK_TIME, today=lambda: TODAY)
    assert reader.get("u001", created.plan_id).conditions.destination == "上海"


def test_060_memory_fallback_still_isolates_users():
    """未配置数据库时回落内存实现，跨用户读取仍必须视为不存在。"""
    service = make_service()
    plan = service.create("u001", CONDITIONS)
    assert service.get("u001", plan.plan_id).plan_id == plan.plan_id
    with pytest.raises(PlanError) as denied:
        service.get("u002", plan.plan_id)
    assert denied.value.code == "PLAN_NOT_FOUND"


# ============================ 044 compute_plan 参数契约 ============================


def _compute_toolkit():
    """构造一个已搜索到候选的规划工具集，供 compute_plan 参数校验用例复用。"""
    service = make_service()
    plan = service.create("u001", CONDITIONS)
    service.attach_candidates("u001", plan.plan_id, simulated_search(CONDITIONS))
    toolkit = build_plan_toolkit(
        service, ctx(), search_provider=simulated_provider, assessors=simulated_assessors(),
    )
    return toolkit, plan.plan_id


def test_044_compute_plan_exposes_structured_schema_to_model():
    """模型必须能直接从 tool schema 看到 scores/policy/weights 的结构，而不是一段无约束字符串。"""
    toolkit, _ = _compute_toolkit()
    schema = run(toolkit.get_tool("compute_plan")).input_schema
    properties = schema["properties"]

    assert "scores_json" not in properties and "policy_json" not in properties
    assert {"scores", "policy", "weights"} <= set(properties)
    # 三个参数都以 $ref 指向可解析的 $defs，模型才能看到字段级约束
    for name, definition in (("scores", "PreferenceScores"), ("policy", "PlanPolicy"), ("weights", "DimensionWeights")):
        assert {"$ref": f"#/$defs/{definition}"} in properties[name]["anyOf"]
        assert definition in schema["$defs"]


def test_044_compute_plan_accepts_structured_objects():
    toolkit, plan_id = _compute_toolkit()
    result = call_tool(
        toolkit, "compute_plan", plan_id=plan_id,
        policy={"hotel_star_limit": 3},
        weights={"time": 0.5, "price": 0.3, "preference": 0.1, "experience": 0.1},
        scores={"transport_scores": {}, "hotel_scores": {}},
    )
    assert result["success"] is True
    assert result["data"]["proposals"]


def test_044_compute_plan_rejects_unknown_field_and_names_the_parameter():
    """非法字段必须回指具体参数，模型才能自纠；这是原 bug 的直接回归点。"""
    toolkit, plan_id = _compute_toolkit()

    bad_scores = call_tool(toolkit, "compute_plan", plan_id=plan_id,
                           scores={"preference_weights": {"时间优先": 0.5}})
    assert bad_scores["errorCode"] == "INVALID_PARAM"
    assert bad_scores["field"] == "scores"
    assert "preference_weights" in bad_scores["message"]

    bad_policy = call_tool(toolkit, "compute_plan", plan_id=plan_id,
                           policy={"标准差旅政策": "经济舱优先"})
    assert bad_policy["errorCode"] == "INVALID_PARAM"
    assert bad_policy["field"] == "policy"
    assert "标准差旅政策" in bad_policy["message"]


def test_044_compute_plan_rejects_string_payload_instead_of_silently_parsing():
    """旧的 JSON 字符串用法必须被明确拒绝，不能静默接受后产生难以诊断的失败。"""
    toolkit, plan_id = _compute_toolkit()
    result = call_tool(toolkit, "compute_plan", plan_id=plan_id, scores='{"transport_scores": {}}')
    assert result["errorCode"] == "INVALID_PARAM"
    assert result["field"] == "scores"
    assert "必须是对象" in result["message"]


# ============================ 044/060 失效 plan_id 的恢复指引 ============================


MISSING_PLAN_ID = "ffffffffffffffffffffffffffffffff"


def test_044_stale_plan_id_returns_recovery_hint_on_every_plan_tool():
    """旧 plan_id 失效时四个工具都必须回指参数并给出重建指引，模型才能自纠。"""
    toolkit = build_plan_toolkit(
        make_service(), ctx(), search_provider=simulated_provider, assessors=simulated_assessors(),
    )
    calls = (
        ("get_plan", {"plan_id": MISSING_PLAN_ID}),
        ("search_candidates", {"plan_id": MISSING_PLAN_ID}),
        ("compute_plan", {"plan_id": MISSING_PLAN_ID}),
        ("review_plan", {"plan_id": MISSING_PLAN_ID, "proposal_tag": "综合最佳"}),
    )
    for name, kwargs in calls:
        result = call_tool(toolkit, name, **kwargs)
        assert result["success"] is False, name
        assert result["errorCode"] == "PLAN_NOT_FOUND", name
        assert result["field"] == "plan_id", name
        assert "create_plan" in result["message"], name


def test_044_not_found_hint_does_not_reveal_other_users_plan():
    """重建提示不得泄露他人的计划是否存在：跨用户与不存在必须给出同一份文案。"""
    service = make_service()
    others = service.create("u002", CONDITIONS)
    toolkit = build_plan_toolkit(
        service, ctx(), search_provider=simulated_provider, assessors=simulated_assessors(),
    )

    foreign = call_tool(toolkit, "get_plan", plan_id=others.plan_id)
    missing = call_tool(toolkit, "get_plan", plan_id=MISSING_PLAN_ID)

    assert foreign["errorCode"] == missing["errorCode"] == "PLAN_NOT_FOUND"
    assert foreign["message"] == missing["message"]


def test_044_plan_prompt_instructs_rebuild_instead_of_reusing_stale_id():
    """提示词必须显式要求重建计划，否则模型会继续用会话里残留的 plan_id。"""
    assert "PLAN_NOT_FOUND" in PLAN_SYSTEM_PROMPT
    assert "create_plan" in PLAN_SYSTEM_PROMPT
    assert "已失效" in PLAN_SYSTEM_PROMPT
