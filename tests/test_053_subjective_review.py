"""053 主观评估器：稳定模型角色的结构化输出、无效输出转为不可用、以及与客观结论的仲裁。"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone

import pytest
from agentscope.message import TextBlock
from agentscope.model import ChatResponse

from gogo_agent.chat.executor import _FallbackMockModel
from gogo_agent.planning.assessors import ModelSubjectiveAssessor, build_model_assessors, _completed_text
from gogo_agent.planning.models import PlanConditions, ReviewSource, ReviewVerdict
from gogo_agent.planning.review import ApprovedTrip, Itinerary
from gogo_agent.planning.service import PlanService
from gogo_agent.planning.simulated import SIMULATED_POLICY, simulated_search
from gogo_agent.planning.store import create_plan_repository

TODAY = date(2026, 10, 11)
CONDITIONS = PlanConditions(
    origin="北京", destination="上海", departure_date=date(2026, 10, 20), return_date=date(2026, 10, 22),
    preferences="不坐红眼航班",
)
APPROVED = ApprovedTrip(origin="北京", destination="上海")
CLOCK = datetime(2026, 10, 11, 4, 0, tzinfo=timezone.utc)


class ScriptedReviewModel(_FallbackMockModel):
    """按系统提示词决定维度结论的替身；responder 可返回文本或抛出异常。"""

    def __init__(self, responder):
        super().__init__(stream=False)
        self.responder = responder
        self.calls: list[list[str]] = []

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        assert not tools, "主观评估不允许工具"
        self.calls.append([m.get_text_content() or "" for m in messages])
        result = self.responder(messages[0].get_text_content() or "")
        if isinstance(result, BaseException):
            raise result
        return ChatResponse(content=[TextBlock(text=result)], is_last=True)


def verdicts(**overrides: str) -> ScriptedReviewModel:
    """默认三个维度都 pass；可用关键字覆盖某个维度的结论（experience/resilience/preference）。"""
    table = {"experience": "pass", "resilience": "pass", "preference": "pass"}
    table.update(overrides)

    def responder(system_prompt: str) -> str:
        key = "experience" if "出行体验" in system_prompt else "resilience" if "韧性" in system_prompt else "preference"
        value = table[key]
        if value.startswith("RAISE:"):
            raise RuntimeError(value.removeprefix("RAISE:"))
        if value == "BAD":
            return "这不是 JSON"
        return json.dumps({"verdict": value, "issues": [] if value == "pass" else [f"{key} 存在问题"]}, ensure_ascii=False)

    return ScriptedReviewModel(responder)


def sample_itinerary() -> Itinerary:
    candidates = simulated_search(CONDITIONS)
    outbound = next(t for t in candidates.transports if t.id == "F1")
    inbound = next(t for t in candidates.transports if t.id == "F4")
    hotel = next(h for h in candidates.hotels if h.id == "H2")
    return Itinerary(
        origin="北京", destination="上海", departure_date=date(2026, 10, 20), return_date=date(2026, 10, 22),
        outbound=outbound, hotel=hotel, inbound=inbound, total_price=2540, budget=None,
        preferences=CONDITIONS.preferences,
    )


def run(coro):
    return asyncio.run(coro)


def test_053_structured_output_maps_to_review_result_and_issue_severity():
    model = verdicts(experience="fail")
    try:
        assessor = ModelSubjectiveAssessor(ReviewSource.EXPERIENCE, model)
        result = run(assessor.assess(sample_itinerary()))
        assert result.source is ReviewSource.EXPERIENCE and result.verdict is ReviewVerdict.FAIL
        assert result.available is True
        assert result.issues[0].severity is ReviewVerdict.FAIL
        assert "experience 存在问题" in result.issues[0].evidence
        assert len(model.calls) == 1
    finally:
        run(model.client.close())


def test_053_prompt_contains_itinerary_and_preferences_but_no_credentials():
    model = verdicts()
    try:
        run(ModelSubjectiveAssessor(ReviewSource.PREFERENCE, model).assess(sample_itinerary()))
        sent = " ".join(model.calls[0])
        assert "departure_date" in sent and "不坐红眼航班" in sent
        assert "api_key" not in sent.lower() and "secret" not in sent.lower()
    finally:
        run(model.client.close())


@pytest.mark.parametrize("bad", ["BAD", "{\"verdict\": \"maybe\", \"issues\": []}"])
def test_053_invalid_output_raises_and_is_never_treated_as_pass(bad):
    model = verdicts(resilience=bad)
    try:
        with pytest.raises(ValueError):
            run(ModelSubjectiveAssessor(ReviewSource.RESILIENCE, model).assess(sample_itinerary()))
    finally:
        run(model.client.close())


def test_053_incomplete_or_non_text_response_is_rejected():
    with pytest.raises(ValueError):
        _completed_text("not a response")
    with pytest.raises(ValueError):
        _completed_text(ChatResponse(content=[TextBlock(text="x")], is_last=False))


def test_053_full_review_all_pass_gives_pass_and_three_model_calls():
    model = verdicts()
    try:
        final = _review(model)
        assert final.verdict is ReviewVerdict.PASS and final.needs_rework is False
        assert [s.source for s in final.subjective] == [
            ReviewSource.EXPERIENCE, ReviewSource.RESILIENCE, ReviewSource.PREFERENCE,
        ]
        assert len(model.calls) == 3
    finally:
        run(model.client.close())


def test_053_model_fail_on_one_dimension_makes_review_fail():
    model = verdicts(preference="fail")
    try:
        final = _review(model)
        assert final.verdict is ReviewVerdict.FAIL and final.needs_rework is True
    finally:
        run(model.client.close())


def test_053_model_exception_is_visible_warning_and_does_not_hide_objective_result():
    model = verdicts(resilience="RAISE:模拟超时")
    try:
        final = _review(model)
        unavailable = [s for s in final.subjective if not s.available]
        assert [s.source for s in unavailable] == [ReviewSource.RESILIENCE]
        assert "RuntimeError" in unavailable[0].issues[0].evidence
        assert final.verdict is ReviewVerdict.WARNING and final.needs_rework is False
    finally:
        run(model.client.close())


def test_053_invalid_output_on_one_dimension_is_unavailable_not_pass():
    model = verdicts(experience="BAD")
    try:
        final = _review(model)
        assert [s.source for s in final.subjective if not s.available] == [ReviewSource.EXPERIENCE]
        assert final.verdict is ReviewVerdict.WARNING
    finally:
        run(model.client.close())


def test_053_build_model_assessors_returns_the_three_dimensions_on_one_model():
    model = verdicts()
    try:
        assessors = build_model_assessors(model)
        assert [a.source for a in assessors] == [
            ReviewSource.EXPERIENCE, ReviewSource.RESILIENCE, ReviewSource.PREFERENCE,
        ]
    finally:
        run(model.client.close())


def _review(model):
    service = PlanService(
        create_plan_repository(), clock=lambda: CLOCK, today=lambda: TODAY,
        id_factory=lambda: "rv-1",
    )
    plan = service.create("u001", CONDITIONS)
    service.attach_candidates("u001", plan.plan_id, simulated_search(CONDITIONS))
    service.compute("u001", plan.plan_id, policy=SIMULATED_POLICY)
    return run(service.review("u001", plan.plan_id, proposal_tag="综合最佳",
                              assessors=build_model_assessors(model), approved=APPROVED))
