"""review_outcome 的 warnings 必须同时包含客观层与仲裁层的警告，并保持顺序与去重。

仲裁层才出现的警告（体验、韧性）若未被带出，用户会看到「未通过」却不知原因。
使用真实的 FinalReview / ReviewResult / ReviewIssue，与生产数据形状一致。
"""

from datetime import datetime, timezone

from gogo_agent.planning.models import (
    FinalReview, ReviewIssue, ReviewResult, ReviewSource, ReviewVerdict,
)
from gogo_agent.planning.review_flow import review_outcome


def _issue(severity: ReviewVerdict, evidence: str) -> ReviewIssue:
    return ReviewIssue(dimension="test", severity=severity, evidence=evidence, suggestion="修改")


def _final(objective: list[ReviewIssue], arbitration: list[ReviewIssue], verdict: ReviewVerdict) -> FinalReview:
    return FinalReview(
        review_id="rv1", plan_id="p1", plan_version=1, proposal_tag="综合最佳",
        verdict=verdict, needs_rework=verdict == ReviewVerdict.FAIL,
        objective=ReviewResult(source=ReviewSource.OBJECTIVE, verdict=ReviewVerdict.PASS, issues=objective),
        arbitration=ReviewResult(source=ReviewSource.ARBITRATOR, verdict=verdict, issues=arbitration),
        reviewed_at=datetime.now(timezone.utc),
    )


def test_arbitration_only_warnings_reach_the_caller():
    """主观层无警告、仲裁层有警告时，警告必须完整返回（修复前返回空列表）。"""
    final = _final(
        objective=[],
        arbitration=[_issue(ReviewVerdict.WARNING, "返程起飞较早")],
        verdict=ReviewVerdict.WARNING,
    )
    assert review_outcome(final)["warnings"] == ["返程起飞较早"]


def test_objective_warnings_come_first_then_arbitration_and_duplicates_are_removed():
    final = _final(
        objective=[_issue(ReviewVerdict.WARNING, "预算接近上限")],
        arbitration=[
            _issue(ReviewVerdict.WARNING, "返程起飞较早"),
            _issue(ReviewVerdict.WARNING, "预算接近上限"),
        ],
        verdict=ReviewVerdict.WARNING,
    )
    assert review_outcome(final)["warnings"] == ["预算接近上限", "返程起飞较早"]


def test_blocking_issues_still_come_only_from_arbitration_fail():
    final = _final(
        objective=[_issue(ReviewVerdict.WARNING, "客观警告")],
        arbitration=[_issue(ReviewVerdict.FAIL, "预算超上限")],
        verdict=ReviewVerdict.FAIL,
    )
    out = review_outcome(final)
    assert out["blocking_issues"] == ["预算超上限"]
    assert "预算超上限" not in out["warnings"]
