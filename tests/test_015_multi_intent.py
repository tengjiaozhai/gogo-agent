"""015 L1/L2 疑似多意图弃权，只有 L3 产出有序多意图结果。"""

import pytest

from gogo_agent.intent import (
    ConfidenceLevel,
    IntentCategory,
    IntentItem,
    IntentRecognizer,
    IntentResult,
    IntentRuleMatcher,
    IntentVectorMatcher,
    MatchStatus,
    QueryInput,
    RecognitionLayer,
)
from tests.test_010_011_intent import FixedModel
from tests.test_014_vector import StubIndex, _hit


class ForbiddenVector:
    """L1 确定为复合请求后，L2 即使可能高分也不能参与分类。"""

    def __init__(self):
        self.calls = 0

    async def match(self, query):
        self.calls += 1
        raise AssertionError("复合请求不得进入 L2 单标签检索")


def multi_result():
    """固定 L3 回答只替代供应商响应，用于验证编排与有序结果契约。"""
    return IntentResult(
        intents=[
            IntentItem(
                intent=IntentCategory.TRAVEL_ORDER_QUERY,
                confidence=ConfidenceLevel.HIGH,
                reason="先查询已有订单",
                evidence=["查订单"],
            ),
            IntentItem(
                intent=IntentCategory.ITINERARY_PLANNING,
                confidence=ConfidenceLevel.HIGH,
                reason="再规划下一程",
                evidence=["规划下一程"],
            ),
        ],
        primary_intent=IntentCategory.ITINERARY_PLANNING,
        multi_intent=True,
        overall_reason="先查询现有订单，再规划后续行程",
    )


@pytest.mark.parametrize(
    "question",
    (
        "查订单并规划下一程",
        "查我的差旅订单并规划下一程",
        "先查订单，再规划下一程",
        "查订单再规划下一程",
        "我要申请出差并看政策",
    ),
)
def test_l1_abstains_when_separated_actions_include_unclassified_clause(question):
    outcome = IntentRuleMatcher().match(QueryInput(question=question))
    assert outcome.status is MatchStatus.AMBIGUOUS
    assert outcome.result is None
    assert "跳过 L2" in outcome.reason


@pytest.mark.asyncio
async def test_fast_route_abstains_and_full_l3_returns_ordered_intents_once():
    query = QueryInput(question="查订单并规划下一程")
    vector = ForbiddenVector()
    model = FixedModel(multi_result().model_dump(mode="json"))
    recognizer = IntentRecognizer(
        model, rule_matcher=IntentRuleMatcher(), vector_matcher=vector,
    )

    fast = await recognizer.recognize(query, fast_only=True)
    assert fast.result is None
    assert fast.attempted_layers == [RecognitionLayer.RULE]
    assert vector.calls == 0 and model.calls == []

    full = await recognizer.recognize(query)
    assert full.hit_layer is RecognitionLayer.LLM
    assert full.attempted_layers == [RecognitionLayer.RULE, RecognitionLayer.LLM]
    assert [item.intent for item in full.result.intents] == [
        IntentCategory.TRAVEL_ORDER_QUERY, IntentCategory.ITINERARY_PLANNING,
    ]
    assert full.result.primary_intent is IntentCategory.ITINERARY_PLANNING
    assert full.result.multi_intent is True
    assert vector.calls == 0 and len(model.calls) == 1


@pytest.mark.asyncio
async def test_l2_cross_category_close_scores_abstain_then_l3_orders_intents():
    index = StubIndex([
        _hit(IntentCategory.TRAVEL_ORDER_QUERY, 0.82),
        _hit(IntentCategory.ITINERARY_PLANNING, 0.79),
    ])
    model = FixedModel(multi_result().model_dump(mode="json"))
    query = QueryInput(question="那趟公务下一程怎么弄")
    assert IntentRuleMatcher().match(query).status is MatchStatus.MISS
    decision = await IntentRecognizer(
        model, rule_matcher=IntentRuleMatcher(), vector_matcher=IntentVectorMatcher(index),
    ).recognize(query)
    assert decision.hit_layer is RecognitionLayer.LLM
    assert decision.attempted_layers == [
        RecognitionLayer.RULE, RecognitionLayer.VECTOR, RecognitionLayer.LLM,
    ]
    assert [(item.intent, item.score) for item in decision.candidates] == [
        (IntentCategory.TRAVEL_ORDER_QUERY, 0.82),
        (IntentCategory.ITINERARY_PLANNING, 0.79),
    ]
    assert decision.result.multi_intent is True
    assert [item.intent for item in decision.result.intents] == [
        IntentCategory.TRAVEL_ORDER_QUERY, IntentCategory.ITINERARY_PLANNING,
    ]
    assert "分差" in decision.reason
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("question,category", (
    ("查机票和酒店", IntentCategory.FLIGHT_SEARCH),
    ("帮我订机票并订酒店", IntentCategory.FLIGHT_SEARCH),
    ("你好，帮我查机票", IntentCategory.FLIGHT_SEARCH),
    ("帮我查机票，看看价格", IntentCategory.FLIGHT_SEARCH),
    ("帮我查机票并不想申请", IntentCategory.FLIGHT_SEARCH),
    ("修改一下，我的出差申请", IntentCategory.TRAVEL_MODIFY),
))
async def test_single_agent_request_keeps_l1_fast_path(question, category):
    vector = ForbiddenVector()
    model = FixedModel()
    decision = await IntentRecognizer(
        model, rule_matcher=IntentRuleMatcher(), vector_matcher=vector,
    ).recognize(QueryInput(question=question), fast_only=True)
    assert decision.hit_layer is RecognitionLayer.RULE
    assert decision.result.primary_intent is category
    assert decision.result.multi_intent is False
    assert vector.calls == 0 and model.calls == []


@pytest.mark.asyncio
async def test_single_intent_l1_miss_can_still_hit_l2_fast_path():
    index = StubIndex([_hit(IntentCategory.POLICY_QUERY, 0.9)])
    decision = await IntentRecognizer(
        FixedModel(), rule_matcher=IntentRuleMatcher(), vector_matcher=IntentVectorMatcher(index),
    ).recognize(QueryInput(question="出差住的地方公司最多给多少"), fast_only=True)
    assert decision.hit_layer is RecognitionLayer.VECTOR
    assert decision.result.primary_intent is IntentCategory.POLICY_QUERY
    assert decision.result.multi_intent is False
