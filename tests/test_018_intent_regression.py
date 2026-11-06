"""018 否定动作、上下文短追问和改写动作保真回归。"""

import json
from datetime import date
from pathlib import Path

import pytest

from gogo_agent.chat.repository import InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.intent import (
    HistoryMessage,
    IntentCategory,
    IntentRuleMatcher,
    MatchStatus,
    ModelOutputError,
    QueryInput,
    QueryRewriter,
    RewriteContext,
)
from gogo_agent.request_context import RequestContext
from tests.test_010_011_intent import FixedModel
from tests.test_016_017_pipeline import ProbeRewriter, ProbeVector, pipeline_for


CORPUS = json.loads(
    (Path(__file__).resolve().parents[1] / "docs/契约样例/018-意图回归语料.json").read_text(encoding="utf-8")
)
CASES = {case["id"]: case for case in CORPUS["cases"]}


def pipeline_request(session_id: str, request_id: str) -> RequestContext:
    return RequestContext(user_id="u1", session_id=session_id, request_id=request_id)


def test_corpus_has_unique_cases_and_valid_category_contract():
    assert CORPUS["schema_version"] == 1
    assert len(CASES) == len(CORPUS["cases"]) == 17
    for case in CASES.values():
        assert case["question"] and case["source"] and case["before"]
        assert set(case["expected"]["intents"]) <= {item.value for item in IntentCategory}


@pytest.mark.parametrize("case_id", (
    "negative_booking_clause", "negative_booking_inline", "negative_then_train",
    "negative_then_flight_without_punctuation",
    "positive_booking", "not_only_flight", "explicit_flight_with_history",
))
def test_affirmative_l1_cases_remain_fast_and_correct(case_id):
    case = CASES[case_id]
    result = IntentRuleMatcher().match(QueryInput(question=case["question"]))
    assert result.status is MatchStatus.HIT
    assert [item.intent.value for item in result.result.intents] == case["expected"]["intents"]


@pytest.mark.parametrize("question", ("我不预订机票", "不要取消出差"))
def test_negative_only_abstains_instead_of_executing_denied_action(question):
    result = IntentRuleMatcher().match(QueryInput(question=question))
    assert result.status is MatchStatus.AMBIGUOUS
    assert result.result is None


@pytest.mark.parametrize("question", (
    "不能不查机票",
    "不能不查机票，同时查酒店",
))
def test_double_negation_abstains_before_any_single_intent_fast_hit(question):
    result = IntentRuleMatcher().match(QueryInput(question=question))
    assert result.status is MatchStatus.AMBIGUOUS
    assert result.result is None


@pytest.mark.asyncio
async def test_contextual_followup_rewrites_before_a_false_positive_vector_hit():
    case = CASES["contextual_shanghai"]
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    for message in case["history"]:
        if message["role"] == "user":
            history.save_user_message("context-018", "u1", message["content"])
        else:
            history.save_assistant_message("context-018", "u1", message["content"])
    message_id = history.save_user_message("context-018", "u1", case["question"])
    vector = ProbeVector(hit=IntentCategory.FLIGHT_SEARCH)
    rewriter = ProbeRewriter("请规划2026年9月29日从北京去上海的两天行程，包括交通和酒店。")
    prepared = await pipeline_for(history, rewriter, vector).prepare(
        pipeline_request("context-018", message_id),
        reference_date=date.fromisoformat(CORPUS["reference_date"]),
    )
    assert prepared.branch == "rewritten"
    assert prepared.fast_decision.attempted_layers == []
    assert vector.calls == []
    assert len(rewriter.calls) == 1
    assert prepared.decision.result.primary_intent is IntentCategory.ITINERARY_PLANNING
    assert prepared.decision.hit_layer.value == "rule"


@pytest.mark.asyncio
async def test_contextual_followup_without_history_requests_context_without_model():
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    case = CASES["contextual_without_history"]
    message_id = history.save_user_message("missing-018", "u1", case["question"])
    vector = ProbeVector(hit=IntentCategory.FLIGHT_SEARCH)
    rewriter = ProbeRewriter("不应调用")
    prepared = await pipeline_for(history, rewriter, vector).prepare(
        pipeline_request("missing-018", message_id),
        reference_date=date.fromisoformat(CORPUS["reference_date"]),
    )
    assert prepared.branch == "needs_context"
    assert prepared.decision is None
    assert vector.calls == rewriter.calls == []


@pytest.mark.asyncio
async def test_elliptic_flight_noun_still_rewrites_with_history():
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    history.save_user_message("elliptic-018", "u1", "请规划2026年10月8日从北京去杭州的两天行程。")
    history.save_assistant_message("elliptic-018", "u1", "已讨论北京出发的两天行程，尚未订票。")
    question = "明天去上海的航班呢？"
    message_id = history.save_user_message("elliptic-018", "u1", question)
    vector = ProbeVector(hit=IntentCategory.FLIGHT_SEARCH)
    rewriter = ProbeRewriter("请查询2026年9月29日从北京到上海的航班。")
    prepared = await pipeline_for(history, rewriter, vector).prepare(
        pipeline_request("elliptic-018", message_id),
        reference_date=date.fromisoformat(CORPUS["reference_date"]),
    )
    assert prepared.branch == "rewritten"
    assert prepared.fast_decision.attempted_layers == []
    assert len(rewriter.calls) == 1
    assert vector.calls == []
    assert prepared.decision.result.primary_intent is IntentCategory.FLIGHT_SEARCH


@pytest.mark.asyncio
async def test_existing_vector_seed_is_not_blocked_by_context_guard():
    case = CASES["approval_vector_seed"]
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    message_id = history.save_user_message("vector-018", "u1", case["question"])
    vector = ProbeVector(hit=IntentCategory.APPROVAL_QUERY)
    rewriter = ProbeRewriter("不应调用")
    prepared = await pipeline_for(history, rewriter, vector).prepare(
        pipeline_request("vector-018", message_id),
        reference_date=date.fromisoformat(CORPUS["reference_date"]),
    )
    assert prepared.branch == "fast"
    assert prepared.decision.hit_layer.value == "vector"
    assert vector.calls == [case["question"]]
    assert rewriter.calls == []


def sanya_context() -> RewriteContext:
    case = CASES["sanya_new_trip"]
    return RewriteContext(
        query=QueryInput(question=case["question"]),
        history=[HistoryMessage(**message) for message in case["history"]],
        reference_date=date.fromisoformat(CORPUS["reference_date"]),
    )


def rewrite_response(question: str) -> dict:
    return {
        "related": True,
        "rewritten_question": question,
        "reason": "固定改写验证",
        "evidence": ["原问题", "历史动作"],
        "missing_context": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", (
    "我本周五到周日去三亚参加海天盛筵。",
    "我本周五到周日去三亚参加海天盛筵，请帮我安排出差行程。",
))
async def test_rewriter_rejects_lost_or_invented_sanya_action(bad):
    model = FixedModel(rewrite_response(bad))
    with pytest.raises(ModelOutputError):
        await QueryRewriter(model).rewrite(sanya_context())
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_rewriter_accepts_preserved_vague_travel_action():
    expected = "帮我处理这次出差，本周五到周日去三亚参加海天盛筵。"
    result = await QueryRewriter(FixedModel(rewrite_response(expected))).rewrite(sanya_context())
    assert result.rewritten_question == expected


@pytest.mark.asyncio
async def test_rewriter_rejects_explicit_application_replaced_by_planning():
    context = RewriteContext(
        query=QueryInput(question="我要申请出差去杭州"),
        history=[], reference_date=date.fromisoformat(CORPUS["reference_date"]),
    )
    model = FixedModel(rewrite_response("请帮我规划去杭州的行程"))
    with pytest.raises(ModelOutputError, match="申请"):
        await QueryRewriter(model).rewrite(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("original,rewritten,missing", (
    ("查询航班并申请出差", "申请出差", "查询"),
    ("查机票并查酒店", "查机票酒店", "查"),
))
async def test_rewriter_rejects_lost_action_or_reduced_action_count(original, rewritten, missing):
    context = RewriteContext(
        query=QueryInput(question=original), history=[],
        reference_date=date.fromisoformat(CORPUS["reference_date"]),
    )
    model = FixedModel(rewrite_response(rewritten))
    with pytest.raises(ModelOutputError, match=missing):
        await QueryRewriter(model).rewrite(context)
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_rewriter_keeps_double_negated_positive_action():
    context = RewriteContext(
        query=QueryInput(question="不能不查机票"), history=[],
        reference_date=date.fromisoformat(CORPUS["reference_date"]),
    )
    with pytest.raises(ModelOutputError, match="查"):
        await QueryRewriter(FixedModel(rewrite_response("先不处理机票"))).rewrite(context)


@pytest.mark.asyncio
async def test_negative_action_is_not_required_in_rewrite():
    context = RewriteContext(
        query=QueryInput(question="我不预订机票"),
        history=[], reference_date=date.fromisoformat(CORPUS["reference_date"]),
    )
    result = await QueryRewriter(FixedModel(rewrite_response("我暂时不办理机票订单"))).rewrite(context)
    assert result.rewritten_question == "我暂时不办理机票订单"


@pytest.mark.asyncio
async def test_unrelated_new_topic_does_not_inherit_old_travel_action():
    context = RewriteContext(
        query=QueryInput(question="给我写一个Python函数"),
        history=[HistoryMessage(role="user", content="帮我处理这次出差。")],
        reference_date=date.fromisoformat(CORPUS["reference_date"]),
    )
    response = rewrite_response(context.query.question)
    response["related"] = False
    result = await QueryRewriter(FixedModel(response)).rewrite(context)
    assert result.rewritten_question == context.query.question


PASTED_SUMMARY = (
    "差旅申请待确认摘要\n"
    "项目\t内容\n"
    "行程方向\t北京 ⇄ 广州\n"
    "出发日期\t2026-12-10\n"
    "返回日期\t2026-12-12（3天2晚）\n"
    "出发城市\t北京\n"
    "目的地\t广州\n"
    "出差事由\t客户拜访\n"
    "方案\t去程 F8 + 酒店 H7（2晚）+ 返程 F16\n"
    "总费用\t¥2,566.00\n"
    "具体安排：\n\n"
    "✈️ 去程 F8：东航 MU6309（11-10 16:45→19:50）\n"
    "🏨 酒店 H7：维也纳酒店·广州惠福西北京路步行街店（2晚，¥273/晚，含早）\n"
    "✈️ 返程 F16：深航 ZH9147（11-12 07:30→10:20）；  帮我提交差旅单"
)


def summary_context(question: str) -> RewriteContext:
    return RewriteContext(
        query=QueryInput(question=question), history=[],
        reference_date=date.fromisoformat(CORPUS["reference_date"]),
    )


@pytest.mark.asyncio
async def test_pasted_summary_titles_do_not_require_their_action_words():
    """粘贴摘要的标题与明细不是用户动作；改写保留请求句即可通过。"""
    expected = "帮我提交差旅单：北京⇄广州，2026-12-10出发，2026-12-12返回（3天2晚），出差事由客户拜访。"
    result = await QueryRewriter(FixedModel(rewrite_response(expected))).rewrite(summary_context(PASTED_SUMMARY))
    assert result.rewritten_question == expected


@pytest.mark.asyncio
async def test_pasted_summary_still_requires_its_request_action():
    """粘贴内容中的请求句仍受保护：改写丢掉「提交」必须拒绝，且报错应指向「提交」。"""
    model = FixedModel(rewrite_response("北京到广州的差旅单，2026-12-10至2026-12-12。"))
    with pytest.raises(ModelOutputError, match="提交"):
        await QueryRewriter(model).rewrite(summary_context(PASTED_SUMMARY))


@pytest.mark.asyncio
async def test_multiline_request_without_blank_line_still_requires_every_action():
    """同一段内的多行请求仍整体计数：丢掉「查询」必须拒绝。"""
    model = FixedModel(rewrite_response("申请出差"))
    with pytest.raises(ModelOutputError, match="查询"):
        await QueryRewriter(model).rewrite(summary_context("查询航班\n并申请出差"))


@pytest.mark.asyncio
async def test_action_falls_back_to_earlier_paragraph_when_last_has_none():
    """最后一段没有动作词时，取前面最近的含动作词段落。"""
    model = FixedModel(rewrite_response("北京到广州的差旅单，2026-12-10至2026-12-12。"))
    with pytest.raises(ModelOutputError, match="提交"):
        await QueryRewriter(model).rewrite(summary_context("帮我提交差旅单\n\n（补充：出发日期 2026-12-10）"))
