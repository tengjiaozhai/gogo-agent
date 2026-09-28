"""012 三层识别编排验收：假规则/向量匹配器控制短路与回退。"""

import pytest
from pydantic import ValidationError

from gogo_agent.intent import (
    ConfidenceLevel,
    FastMatch,
    IntentCandidate,
    IntentCategory,
    IntentItem,
    IntentRecognizer,
    IntentResult,
    MatchStatus,
    ModelOutputError,
    QueryInput,
    RecognitionLayer,
)
from tests.test_010_011_intent import FixedModel


QUERY = QueryInput(question="帮我查差旅政策")


def intent_result(category=IntentCategory.POLICY_QUERY, confidence=ConfidenceLevel.HIGH):
    return IntentResult(
        intents=[IntentItem(intent=category, confidence=confidence, reason="固定类别", evidence=["用户问题片段"])],
        primary_intent=category,
        multi_intent=False,
        overall_reason="固定分类结果",
    )


def candidate(layer, category=IntentCategory.POLICY_QUERY, score=None):
    return IntentCandidate(intent=category, layer=layer, score=score, reason="固定匹配依据")


def match(status, *, layer, result=None, score=None, threshold=None, candidates=None, reason=None):
    return FastMatch(
        status=status,
        result=result,
        candidates=candidates if candidates is not None else [candidate(layer, score=score)],
        threshold=threshold,
        reason=reason or f"固定{status.value}结果",
    )


class FakeRuleMatcher:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def match(self, query):
        self.calls.append(query.question)
        return self.result


class FakeVectorMatcher:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def match(self, query):
        self.calls.append(query.question)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.asyncio
async def test_rule_hit_short_circuits_vector_and_llm():
    rule = FakeRuleMatcher(match(
        MatchStatus.HIT, layer=RecognitionLayer.RULE, result=intent_result()
    ))
    vector = FakeVectorMatcher(match(
        MatchStatus.HIT, layer=RecognitionLayer.VECTOR,
        result=intent_result(), score=0.9, threshold=0.75,
    ))
    model = FixedModel()
    decision = await IntentRecognizer(model, rule_matcher=rule, vector_matcher=vector).recognize(QUERY)
    assert decision.result.primary_intent is IntentCategory.POLICY_QUERY
    assert decision.hit_layer is RecognitionLayer.RULE
    assert decision.confidence is ConfidenceLevel.HIGH
    assert decision.vector_threshold is None
    assert decision.attempted_layers == [RecognitionLayer.RULE]
    assert len(decision.candidates) == 1 and decision.candidates[0].layer is RecognitionLayer.RULE
    assert rule.calls == [QUERY.question]
    assert vector.calls == model.calls == []


@pytest.mark.asyncio
async def test_rule_miss_then_vector_hit_short_circuits_llm():
    rule = FakeRuleMatcher(match(MatchStatus.MISS, layer=RecognitionLayer.RULE))
    vector = FakeVectorMatcher(match(
        MatchStatus.HIT, layer=RecognitionLayer.VECTOR,
        result=intent_result(confidence=ConfidenceLevel.MEDIUM), score=0.82, threshold=0.75,
    ))
    model = FixedModel()
    decision = await IntentRecognizer(model, rule_matcher=rule, vector_matcher=vector).recognize(QUERY)
    assert decision.hit_layer is RecognitionLayer.VECTOR
    assert decision.confidence is ConfidenceLevel.MEDIUM
    assert decision.vector_threshold == 0.75
    assert [item.layer for item in decision.candidates] == [RecognitionLayer.RULE, RecognitionLayer.VECTOR]
    assert decision.attempted_layers == [RecognitionLayer.RULE, RecognitionLayer.VECTOR]
    assert len(rule.calls) == len(vector.calls) == 1
    assert model.calls == []


@pytest.mark.asyncio
async def test_both_fast_layers_miss_then_one_llm_fallback():
    rule = FakeRuleMatcher(match(MatchStatus.MISS, layer=RecognitionLayer.RULE))
    vector = FakeVectorMatcher(match(
        MatchStatus.MISS, layer=RecognitionLayer.VECTOR, score=0.72, threshold=0.75,
        reason="0.72 低于 0.75",
    ))
    model = FixedModel(intent_result().model_dump(mode="json"))
    decision = await IntentRecognizer(model, rule_matcher=rule, vector_matcher=vector).recognize(QUERY)
    assert decision.hit_layer is RecognitionLayer.LLM
    assert decision.result.primary_intent is IntentCategory.POLICY_QUERY
    assert decision.vector_threshold == 0.75
    assert decision.attempted_layers == [RecognitionLayer.RULE, RecognitionLayer.VECTOR, RecognitionLayer.LLM]
    assert [item.score for item in decision.candidates] == [None, 0.72]
    assert "L1" in decision.reason and "L2" in decision.reason and "L3" in decision.reason
    assert len(rule.calls) == len(vector.calls) == len(model.calls) == 1


@pytest.mark.asyncio
async def test_ambiguous_rule_skips_vector_and_defers_to_llm():
    rule = FakeRuleMatcher(match(
        MatchStatus.AMBIGUOUS, layer=RecognitionLayer.RULE,
        candidates=[candidate(RecognitionLayer.RULE, IntentCategory.POLICY_QUERY),
                    candidate(RecognitionLayer.RULE, IntentCategory.TRAVEL_APPLICATION)],
        reason="L0/L1 疑似复合请求",
    ))
    vector = FakeVectorMatcher(match(
        MatchStatus.HIT, layer=RecognitionLayer.VECTOR,
        result=intent_result(), score=0.9, threshold=0.75,
    ))
    model = FixedModel(intent_result().model_dump(mode="json"))
    recognizer = IntentRecognizer(model, rule_matcher=rule, vector_matcher=vector)
    fast = await recognizer.recognize(QUERY, fast_only=True)
    assert fast.result is None and fast.hit_layer is None
    assert fast.attempted_layers == [RecognitionLayer.RULE]
    assert len(fast.candidates) == 2 and vector.calls == model.calls == []
    full = await recognizer.recognize(QUERY)
    assert full.hit_layer is RecognitionLayer.LLM
    assert full.attempted_layers == [RecognitionLayer.RULE, RecognitionLayer.LLM]
    assert len(rule.calls) == 2 and vector.calls == [] and len(model.calls) == 1


@pytest.mark.asyncio
async def test_vector_ambiguity_defers_to_llm_without_claiming_a_vector_hit():
    vector = FakeVectorMatcher(match(
        MatchStatus.AMBIGUOUS,
        layer=RecognitionLayer.VECTOR,
        score=0.84,
        threshold=0.75,
        candidates=[
            candidate(RecognitionLayer.VECTOR, IntentCategory.POLICY_QUERY, 0.84),
            candidate(RecognitionLayer.VECTOR, IntentCategory.TRAVEL_APPLICATION, 0.82),
        ],
        reason="Top-2 属于不同类别且分差不足",
    ))
    model = FixedModel(intent_result().model_dump(mode="json"))
    decision = await IntentRecognizer(model, vector_matcher=vector).recognize(QUERY)
    assert decision.hit_layer is RecognitionLayer.LLM
    assert decision.vector_threshold == 0.75
    assert [item.score for item in decision.candidates] == [0.84, 0.82]
    assert decision.attempted_layers == [RecognitionLayer.VECTOR, RecognitionLayer.LLM]
    assert len(vector.calls) == len(model.calls) == 1


@pytest.mark.asyncio
async def test_fast_only_miss_returns_candidates_without_model_call():
    rule = FakeRuleMatcher(match(MatchStatus.MISS, layer=RecognitionLayer.RULE))
    vector = FakeVectorMatcher(match(MatchStatus.MISS, layer=RecognitionLayer.VECTOR, score=0.7, threshold=0.75))
    model = FixedModel()
    decision = await IntentRecognizer(model, rule_matcher=rule, vector_matcher=vector).recognize(
        QUERY, fast_only=True
    )
    assert decision.result is None and decision.confidence is None
    assert decision.vector_threshold == 0.75 and len(decision.candidates) == 2
    assert decision.attempted_layers == [RecognitionLayer.RULE, RecognitionLayer.VECTOR]
    assert model.calls == []


@pytest.mark.asyncio
async def test_vector_failure_is_recorded_then_llm_runs_once():
    vector = FakeVectorMatcher(TimeoutError("secret query should not be logged"))
    model = FixedModel(intent_result().model_dump(mode="json"))
    decision = await IntentRecognizer(model, vector_matcher=vector).recognize(QUERY)
    assert decision.hit_layer is RecognitionLayer.LLM
    assert decision.attempted_layers == [RecognitionLayer.VECTOR, RecognitionLayer.LLM]
    assert "TimeoutError" in decision.reason
    assert "secret query" not in decision.reason
    assert len(vector.calls) == len(model.calls) == 1


@pytest.mark.asyncio
async def test_unconfigured_fast_mode_is_explicit_miss_and_full_mode_uses_llm():
    model = FixedModel(intent_result().model_dump(mode="json"))
    recognizer = IntentRecognizer(model)
    fast = await recognizer.recognize(QUERY, fast_only=True)
    assert fast.result is None and fast.attempted_layers == []
    assert "尚未配置" in fast.reason and model.calls == []
    full = await recognizer.recognize(QUERY)
    assert full.hit_layer is RecognitionLayer.LLM
    assert full.attempted_layers == [RecognitionLayer.LLM]
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_invalid_vector_hit_below_threshold_is_rejected_not_routed():
    vector = FakeVectorMatcher(match(
        MatchStatus.HIT, layer=RecognitionLayer.VECTOR,
        result=intent_result(), score=0.74, threshold=0.75,
    ))
    model = FixedModel()
    with pytest.raises(ValueError, match="低于阈值"):
        await IntentRecognizer(model, vector_matcher=vector).recognize(QUERY)
    assert len(vector.calls) == 1 and model.calls == []


@pytest.mark.asyncio
async def test_matcher_contract_rejects_wrong_source_or_missing_vector_threshold():
    model = FixedModel()
    wrong_source = FakeRuleMatcher(match(MatchStatus.MISS, layer=RecognitionLayer.VECTOR))
    with pytest.raises(ValueError, match="来源层级"):
        await IntentRecognizer(model, rule_matcher=wrong_source).recognize(QUERY)
    no_threshold = FakeVectorMatcher(match(MatchStatus.MISS, layer=RecognitionLayer.VECTOR, score=0.8))
    with pytest.raises(ValueError, match="阈值"):
        await IntentRecognizer(model, vector_matcher=no_threshold).recognize(QUERY)
    assert model.calls == []


@pytest.mark.asyncio
async def test_rule_failure_and_bad_llm_json_are_not_hidden():
    class BrokenRule:
        def match(self, query):
            raise RuntimeError("broken local rule")

    model = FixedModel()
    with pytest.raises(RuntimeError, match="broken local rule"):
        await IntentRecognizer(model, rule_matcher=BrokenRule()).recognize(QUERY)
    assert model.calls == []

    vector = FakeVectorMatcher(match(MatchStatus.MISS, layer=RecognitionLayer.VECTOR, score=0.7, threshold=0.75))
    bad_model = FixedModel("{broken")
    with pytest.raises(ModelOutputError):
        await IntentRecognizer(bad_model, vector_matcher=vector).recognize(QUERY)
    assert len(vector.calls) == len(bad_model.calls) == 1


def test_invalid_candidate_and_match_state_are_rejected():
    with pytest.raises(ValidationError):
        candidate(RecognitionLayer.VECTOR, score=float("nan"))
    with pytest.raises(ValidationError):
        match(MatchStatus.HIT, layer=RecognitionLayer.RULE, result=None)


@pytest.mark.asyncio
async def test_invalid_entry_arguments_fail_before_any_matcher_or_model_call():
    rule = FakeRuleMatcher(match(MatchStatus.HIT, layer=RecognitionLayer.RULE, result=intent_result()))
    model = FixedModel()
    recognizer = IntentRecognizer(model, rule_matcher=rule)
    with pytest.raises(TypeError, match="QueryInput"):
        await recognizer.recognize("帮我查政策")
    with pytest.raises(TypeError, match="fast_only"):
        await recognizer.recognize(QUERY, fast_only="false")
    assert rule.calls == model.calls == []


@pytest.mark.asyncio
async def test_llm_multi_intent_keeps_primary_and_order():
    result = IntentResult(
        intents=[
            IntentItem(intent=IntentCategory.POLICY_QUERY, confidence=ConfidenceLevel.HIGH,
                       reason="先查政策", evidence=["先查政策"]),
            IntentItem(intent=IntentCategory.TRAVEL_APPLICATION, confidence=ConfidenceLevel.MEDIUM,
                       reason="再申请", evidence=["再申请出差"]),
        ],
        primary_intent=IntentCategory.TRAVEL_APPLICATION,
        multi_intent=True,
        overall_reason="政策查询后申请",
    )
    model = FixedModel(result.model_dump(mode="json"))
    decision = await IntentRecognizer(model).recognize(QUERY)
    assert decision.hit_layer is RecognitionLayer.LLM
    assert decision.confidence is ConfidenceLevel.MEDIUM
    assert [item.intent for item in decision.result.intents] == [
        IntentCategory.POLICY_QUERY, IntentCategory.TRAVEL_APPLICATION,
    ]
    assert len(model.calls) == 1
