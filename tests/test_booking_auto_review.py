"""预订准备阶段自动审核：未审核计划时由 prepare_booking 调用审核端口；结论为 fail 时不出卡，warning 只作为建议随卡片展示。

端口调用的是真实的 `PlanService.review`（会写入 last_review），只替换了主观评估模型。
服务层门禁不变：`book()` 仍不触发审核。
"""

from gogo_agent.booking.tools import build_booking_toolkit
from gogo_agent.planning.models import ReviewIssue, ReviewResult, ReviewSource, ReviewVerdict
from gogo_agent.planning.review import Itinerary
from gogo_agent.planning.review_flow import review_outcome, review_proposal
from gogo_agent.planning.simulated import SIMULATED_POLICY, simulated_assessors, simulated_search
from gogo_agent.request_context import RequestContext
from tests.test_061_067_booking import CONDITIONS, Stack, approved_order, call_tool, run


class IssueAssessor:
    """产生具体 warning issue 的主观评估替身：真实评估器才会给出具体警告文字，模拟评估器只给结论。"""

    def __init__(self, source: ReviewSource, text: str):
        self.source = source
        self.text = text

    async def assess(self, itinerary: Itinerary) -> ReviewResult:
        return ReviewResult(source=self.source, verdict=ReviewVerdict.WARNING, issues=[ReviewIssue(
            dimension=self.source.value, severity=ReviewVerdict.WARNING, evidence=self.text, suggestion="确认",
        )])


class PlanServiceReviewer:
    """端口实现：调用真实审核入口，返回摘要字典，并计数。"""

    def __init__(self, stack: Stack, assessors):
        self.stack = stack
        self.assessors = assessors
        self.calls = 0

    async def review(self, user_id: str, plan_id: str, proposal_tag: str) -> dict:
        self.calls += 1
        # 走审核权威路径 review_proposal：关联差旅单时由它校验审批并构造 ApprovedTrip。
        final = await review_proposal(
            self.stack.plans, user_id, plan_id, proposal_tag=proposal_tag,
            assessors=self.assessors, travel_provider=lambda: self.stack.travel,
        )
        # 与生产共用同一个摘要函数，避免测试替身与生产路径不一致。
        return review_outcome(final)


def _unreviewed_plan(stack: Stack) -> str:
    """已计算方案、差旅单已审批、但从未审核的计划。"""
    order_id = approved_order(stack)
    plan = stack.plans.create("u001", CONDITIONS, order_id=order_id)
    stack.plans.attach_candidates("u001", plan.plan_id, simulated_search(CONDITIONS))
    stack.plans.compute("u001", plan.plan_id, policy=SIMULATED_POLICY)
    return plan.plan_id


class _history_stub:
    """prepare 路径不读取历史，此处只提供空消息，避免引入持久化依赖。"""

    def list_messages(self, session_id, user_id):
        return []


def _prepare(stack: Stack, plan_id: str, reviewer) -> dict:
    ctx = RequestContext(user_id="u001", session_id="s-auto", request_id="r")
    toolkit = build_booking_toolkit(stack.service, ctx, reviewer=reviewer)
    return run(call_tool(toolkit, "prepare_booking",
                         {"plan_id": plan_id, "proposal_tag": "综合最佳", "leg": "outbound"}))


def test_unreviewed_plan_is_auto_reviewed_then_gets_confirmation_card():
    stack = Stack()
    plan_id = _unreviewed_plan(stack)
    reviewer = PlanServiceReviewer(stack, simulated_assessors(ReviewVerdict.PASS))

    body = _prepare(stack, plan_id, reviewer)

    assert body.get("errorCode") == "CONFIRMATION_REQUIRED", body
    assert reviewer.calls == 1
    assert stack.supplier.create_calls == 0
    # 审核结论已落库并绑定当前内容版本，而不只是返回了一个卡片。
    stored = stack.plans.get("u001", plan_id)
    assert stored.last_review is not None and stored.review_is_current()


def test_warning_review_still_gets_confirmation_card_and_is_not_reviewed_again():
    """warning 只是建议：自动审核后仍出确认卡；同一内容版本再次准备复用已有审核，不重新审核。"""
    stack = Stack()
    plan_id = _unreviewed_plan(stack)
    reviewer = PlanServiceReviewer(stack, simulated_assessors(ReviewVerdict.WARNING))

    first = _prepare(stack, plan_id, reviewer)
    second = _prepare(stack, plan_id, reviewer)

    assert first.get("errorCode") == "CONFIRMATION_REQUIRED", first
    assert second.get("errorCode") == "CONFIRMATION_REQUIRED", second
    assert reviewer.calls == 1
    assert stack.supplier.create_calls == 0


def test_fail_review_blocks_confirmation_card_and_returns_verdict():
    stack = Stack()
    plan_id = _unreviewed_plan(stack)
    reviewer = PlanServiceReviewer(stack, simulated_assessors(ReviewVerdict.FAIL))

    body = _prepare(stack, plan_id, reviewer)

    assert body.get("errorCode") == "REVIEW_NOT_PASSED", body
    assert body.get("data", {}).get("verdict") == "fail", body
    assert reviewer.calls == 1
    assert stack.supplier.create_calls == 0


def test_fail_review_denial_reaches_turn_on_first_and_repeat_prepare():
    # 延迟导入：顶层导入 booking.chat 会在 chat 包未完成初始化时触发循环依赖（chat/executor -> booking.chat）。
    from gogo_agent.booking.chat import BookingChatTurn

    stack = Stack()
    plan_id = _unreviewed_plan(stack)
    reviewer = PlanServiceReviewer(stack, simulated_assessors(ReviewVerdict.FAIL))
    ctx = RequestContext(user_id="u001", session_id="s-auto", request_id="r")
    turn = BookingChatTurn(ctx, _history_stub(), lambda: stack.service)  # type: ignore[arg-type]
    toolkit = build_booking_toolkit(stack.service, ctx, turn=turn, reviewer=reviewer)
    call = lambda: run(call_tool(toolkit, "prepare_booking", {"plan_id": plan_id, "proposal_tag": "综合最佳", "leg": "outbound"}))

    first = call()  # 自动审核产生 fail
    second = call()  # 已有审核为 fail，门禁直接报 REVIEW_NOT_PASSED

    assert first.get("errorCode") == "REVIEW_NOT_PASSED" and second.get("errorCode") == "REVIEW_NOT_PASSED"
    assert reviewer.calls == 1
    assert len(turn.denials) == 2 and all("审核结论不是通过" in d for d in turn.denials), turn.denials
    assert turn.pending == []


def test_warning_is_kept_as_advisory_not_denial_and_not_repeated():
    # 延迟导入理由同上。
    from gogo_agent.booking.chat import BookingChatTurn

    stack = Stack()
    plan_id = _unreviewed_plan(stack)
    reviewer = PlanServiceReviewer(stack, [IssueAssessor(ReviewSource.EXPERIENCE, "返程航班起飞较早")])
    ctx = RequestContext(user_id="u001", session_id="s-auto", request_id="r")
    turn = BookingChatTurn(ctx, _history_stub(), lambda: stack.service)  # type: ignore[arg-type]
    toolkit = build_booking_toolkit(stack.service, ctx, turn=turn, reviewer=reviewer)
    call = lambda: run(call_tool(toolkit, "prepare_booking", {"plan_id": plan_id, "proposal_tag": "综合最佳", "leg": "outbound"}))

    first = call()
    second = call()

    assert first.get("errorCode") == "CONFIRMATION_REQUIRED", first
    assert second.get("errorCode") == "CONFIRMATION_REQUIRED", second
    assert reviewer.calls == 1
    assert turn.denials == []
    assert turn.advisories.count("返程航班起飞较早") == 1, turn.advisories


def test_advisory_appears_in_fixed_reply_next_to_confirmation_card():
    """预订回复正文由业务事实生成：审核建议要进入待确认摘要的固定回复，模型正文不被采用。"""
    # 延迟导入理由同上。
    from gogo_agent.booking.chat import BookingChatTurn
    from gogo_agent.intent.models import (
        ConfidenceLevel, IntentCategory, IntentItem, IntentResult, QueryInput, RecognitionDecision, RecognitionLayer,
    )
    from gogo_agent.intent.pipeline import PreparedIntentTurn

    stack = Stack()
    plan_id = _unreviewed_plan(stack)
    reviewer = PlanServiceReviewer(stack, [IssueAssessor(ReviewSource.EXPERIENCE, "返程航班起飞较早")])
    ctx = RequestContext(user_id="u001", session_id="s-auto", request_id="r")
    turn = BookingChatTurn(ctx, _history_stub(), lambda: stack.service)  # type: ignore[arg-type]
    toolkit = build_booking_toolkit(stack.service, ctx, turn=turn, reviewer=reviewer)
    run(call_tool(toolkit, "prepare_booking", {"plan_id": plan_id, "proposal_tag": "综合最佳", "leg": "outbound"}))

    item = IntentItem(intent=IntentCategory.BOOKING, confidence=ConfidenceLevel.HIGH, reason="固定预订意图", evidence=["预订去程"])
    result = IntentResult(intents=[item], primary_intent=IntentCategory.BOOKING, multi_intent=False, overall_reason="固定回归")
    decision = RecognitionDecision(result=result, hit_layer=RecognitionLayer.RULE, confidence=ConfidenceLevel.HIGH,
                                   vector_threshold=None, candidates=[], attempted_layers=[RecognitionLayer.RULE], reason="fixed")
    query = QueryInput(question="预订这个方案的去程")
    prepared = PreparedIntentTurn(branch="fast", original_question=query, effective_question=query,
                                  fast_decision=decision, decision=decision, rewrite=None)

    text = turn.reply_text("模型正文不应被采用", prepared)

    assert "尚未下单" in text and "返程航班起飞较早" in text and "模型正文不应被采用" not in text, text
