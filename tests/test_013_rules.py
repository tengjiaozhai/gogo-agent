"""013 Java L0/L1 规则优先级、排除词、复合弃权与安全读写边界。"""

import pytest
from pydantic import ValidationError

from gogo_agent.intent import IntentCategory, MatchStatus, QueryInput, RecognitionLayer
from gogo_agent.intent.rules import IntentRuleMatcher
from gogo_agent.intent import rules as rule_module


def match(text: str):
    return IntentRuleMatcher().match(QueryInput(question=text))


@pytest.mark.parametrize(
    "text,category",
    [
        ("下午好，请问在吗？", IntentCategory.GREETING),
        ("帮我报销这几张发票", IntentCategory.REIMBURSEMENT),
        ("北京的差旅政策是什么", IntentCategory.POLICY_QUERY),
        ("我的审批进度走到哪了", IntentCategory.APPROVAL_QUERY),
        ("取消出差", IntentCategory.TRAVEL_CANCEL),
        ("修改差旅申请的日期", IntentCategory.TRAVEL_MODIFY),
        ("查下我的差旅订单", IntentCategory.TRAVEL_ORDER_QUERY),
        ("上海有什么好玩的地方", IntentCategory.ATTRACTIONS_QUERY),
        ("上海这周天气怎么样", IntentCategory.GENERAL_INFO),
        ("帮我规划北京到上海的行程", IntentCategory.ITINERARY_PLANNING),
        ("明天有哪些航班可以选", IntentCategory.FLIGHT_SEARCH),
        ("去南京的高铁有哪些", IntentCategory.TRAIN_SEARCH),
        ("上海附近有什么酒店", IntentCategory.HOTEL_SEARCH),
        ("第二个方案就订下来", IntentCategory.BOOKING),
        ("我要申请出差去杭州", IntentCategory.TRAVEL_APPLICATION),
    ],
)
def test_valid_java_rule_categories(text, category):
    outcome = match(text)
    assert outcome.status is MatchStatus.HIT
    assert outcome.result.primary_intent is category
    assert outcome.result.intents[0].confidence == "high"
    assert outcome.candidates[0].layer is RecognitionLayer.RULE
    assert outcome.candidates[0].score is None


@pytest.mark.parametrize(
    "text,category",
    [
        ("报销政策中住宿标准是多少", IntentCategory.POLICY_QUERY),
        ("取消差旅订单", IntentCategory.TRAVEL_CANCEL),
        ("取消订单", IntentCategory.BOOKING),
        ("查机票和酒店", IntentCategory.FLIGHT_SEARCH),
        ("你好，帮我查机票", IntentCategory.FLIGHT_SEARCH),
        ("顺便问下餐标", IntentCategory.POLICY_QUERY),
    ],
)
def test_exclusions_and_priority_preserve_read_write_distinctions(text, category):
    assert match(text).result.primary_intent is category


def test_strong_conjunction_guard_abstains_before_single_intent_rules():
    outcome = match("帮我查一下差旅政策，并且报销这张发票")
    assert outcome.status is MatchStatus.AMBIGUOUS
    assert outcome.result is None
    assert outcome.candidates == []
    assert "L0" in outcome.reason


def test_clause_level_guard_abstains_across_different_target_groups():
    outcome = match("查下差旅政策，帮我报销这张发票")
    assert outcome.status is MatchStatus.AMBIGUOUS
    assert outcome.result is None
    assert [c.intent for c in outcome.candidates] == [
        IntentCategory.POLICY_QUERY, IntentCategory.REIMBURSEMENT
    ]


def test_only_known_clause_categories_on_same_target_use_full_rule_priority():
    outcome = match("查机票和酒店")
    assert outcome.status is MatchStatus.HIT
    assert outcome.result.primary_intent is IntentCategory.FLIGHT_SEARCH


def test_ambiguous_generic_order_lookup_stays_miss_for_llm():
    outcome = match("查一下订单")
    assert outcome.status is MatchStatus.MISS
    assert outcome.result is None


def test_colloquial_cancel_outside_java_rule_falls_through():
    assert match("取消这次出差").status is MatchStatus.MISS


def test_java_baseline_does_not_recognize_bare_policy_in_composite_sentence():
    outcome = match("我要申请出差并看政策")
    assert outcome.status is MatchStatus.HIT
    assert outcome.result.primary_intent is IntentCategory.TRAVEL_APPLICATION
    # 015/018 才修复该复合句的漏判，013 不伪称 Java 原有规则已识别政策。


def test_appending_lower_priority_rule_keeps_existing_higher_priority(monkeypatch):
    before = match("差旅政策是什么")
    extra = rule_module._rule(IntentCategory.BOOKING, r"差旅政策|橘色航程测试")
    monkeypatch.setattr(rule_module, "_RULES", (*rule_module._RULES, extra))
    after = match("差旅政策是什么")
    assert before.result.primary_intent is after.result.primary_intent is IntentCategory.POLICY_QUERY
    assert match("橘色航程测试").result.primary_intent is IntentCategory.BOOKING


def test_rule_table_covers_every_java_category_except_unknown():
    assert {rule.category for rule in rule_module._RULES} == set(IntentCategory) - {IntentCategory.UNKNOWN}


def test_invalid_input_is_rejected_before_rule_evaluation():
    with pytest.raises(ValidationError):
        QueryInput(question="  ")
    with pytest.raises(TypeError):
        IntentRuleMatcher().match("查差旅政策")
