"""008 契约验收：有效样例、结构拒绝、跨字段一致性与文档 schema。"""

from copy import deepcopy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from gogo_agent.intent import (
    ConfidenceLevel,
    IntentCategory,
    IntentItem,
    IntentResult,
    QueryInput,
    RewriteResult,
)


EXAMPLES = json.loads(
    (Path(__file__).resolve().parents[1] / "docs/契约样例/008-两轮对话.json").read_text(
        encoding="utf-8"
    )
)
FIRST = EXAMPLES["dialogue"][0]
CASES = EXAMPLES["dialogue"] + EXAMPLES["standalone"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_documented_examples_round_trip(case):
    """文档中的 JSON 直接通过权威模型解析，序列化后仍满足同一契约。"""
    for model, payload in (
        (QueryInput, case["query"]),
        (RewriteResult, case["expected_rewrite"]),
        (IntentResult, case["expected_intent"]),
    ):
        if payload is None:
            continue
        parsed = model.model_validate_json(json.dumps(payload, ensure_ascii=False))
        assert model.model_validate_json(parsed.model_dump_json()) == parsed


def test_two_turn_context_and_missing_history_are_explicit():
    """同一句追问可表达补全成功或缺上下文，日期样例不依赖运行当天。"""
    assert EXAMPLES["reference_date"] == "2026-09-27"
    second = EXAMPLES["dialogue"][1]
    assert second["query"]["question"] == "明天去上海呢？"
    resolved = RewriteResult.model_validate(second["expected_rewrite"])
    assert resolved.related is True
    assert resolved.rewritten_question == (
        "请规划2026年9月28日从北京去上海的两天出差行程，包括往返交通和酒店。"
    )
    missing = next(case for case in CASES if case["id"] == "missing_history")
    assert missing["query"] == second["query"]
    unresolved = RewriteResult.model_validate(missing["expected_rewrite"])
    assert unresolved.rewritten_question is None
    assert "出发城市" in unresolved.missing_context


CONTRACTS = [
    (QueryInput, FIRST["query"]),
    (RewriteResult, FIRST["expected_rewrite"]),
    (IntentItem, FIRST["expected_intent"]["intents"][0]),
    (IntentResult, FIRST["expected_intent"]),
]


@pytest.mark.parametrize(
    "model,payload,field",
    [(model, payload, field) for model, payload in CONTRACTS for field in model.model_fields],
    ids=[f"{model.__name__}.{field}" for model, _ in CONTRACTS for field in model.model_fields],
)
def test_missing_contract_fields_are_rejected(model, payload, field):
    incomplete = deepcopy(payload)
    del incomplete[field]
    with pytest.raises(ValidationError) as err:
        model.model_validate(incomplete)
    assert any(item["loc"] == (field,) and item["type"] == "missing" for item in err.value.errors())


@pytest.mark.parametrize("question", ["", " \n\t ", None, 123])
def test_empty_or_non_text_query_is_rejected(question):
    with pytest.raises(ValidationError):
        QueryInput(question=question)


def test_query_only_trims_surrounding_whitespace():
    assert QueryInput(question="  明天 去上海呢？\n").question == "明天 去上海呢？"


@pytest.mark.parametrize(
    "patch",
    [
        {"intent": "flight_query"},
        {"confidence": "HIGH"},
        {"confidence": 0.95},
        {"reason": "   "},
        {"evidence": []},
        {"evidence": ["   "]},
    ],
)
def test_invalid_intent_details_are_rejected(patch):
    payload = {**FIRST["expected_intent"]["intents"][0], **patch}
    with pytest.raises(ValidationError):
        IntentItem.model_validate(payload)


@pytest.mark.parametrize("category", ["unknown", "reimbursement"])
def test_recognized_category_does_not_supply_an_execution_target(category):
    item = IntentItem(
        intent=category, confidence="low", reason="分类样例，不授权执行", evidence=["用户原文"]
    )
    assert item.intent == IntentCategory(category)
    assert item.confidence is ConfidenceLevel.LOW
    assert "target_agent" not in item.model_dump()


@pytest.mark.parametrize("model,payload", CONTRACTS, ids=[item[0].__name__ for item in CONTRACTS])
def test_untrusted_extra_fields_are_rejected(model, payload):
    for key, value in (("user_id", "other-user"), ("target_agent", "bookingAgent")):
        with pytest.raises(ValidationError) as err:
            model.model_validate({**payload, key: value})
        assert any(item["type"] == "extra_forbidden" for item in err.value.errors())


@pytest.mark.parametrize(
    "patch",
    [
        {"rewritten_question": None},
        {"missing_context": ["出发城市"]},
        {"rewritten_question": "  "},
        {"reason": "  "},
        {"evidence": []},
        {"evidence": ["  "]},
        {"rewritten_question": None, "missing_context": ["  "]},
    ],
)
def test_incomplete_or_contradictory_rewrite_is_rejected(patch):
    with pytest.raises(ValidationError):
        RewriteResult.model_validate({**FIRST["expected_rewrite"], **patch})


@pytest.mark.parametrize("value", [0, 1, "false", "true"])
def test_boolean_flags_are_not_coerced(value):
    with pytest.raises(ValidationError):
        RewriteResult.model_validate({**FIRST["expected_rewrite"], "related": value})
    with pytest.raises(ValidationError):
        IntentResult.model_validate({**FIRST["expected_intent"], "multi_intent": value})


def test_multi_intent_preserves_order_and_primary_can_be_later():
    case = next(case for case in CASES if case["id"] == "multiple_intents")
    result = IntentResult.model_validate(case["expected_intent"])
    assert [item.intent for item in result.intents] == [
        IntentCategory.POLICY_QUERY, IntentCategory.TRAVEL_APPLICATION
    ]
    assert result.primary_intent is IntentCategory.TRAVEL_APPLICATION
    assert result.multi_intent is True


@pytest.mark.parametrize(
    "patch",
    [
        {"intents": []},
        {"primary_intent": "booking"},
        {"primary_intent": "not-an-intent"},
        {"multi_intent": True},
        {"overall_reason": "  "},
    ],
)
def test_invalid_result_is_rejected(patch):
    with pytest.raises(ValidationError):
        IntentResult.model_validate({**FIRST["expected_intent"], **patch})


def test_multiple_items_cannot_claim_single_intent():
    case = next(case for case in CASES if case["id"] == "multiple_intents")
    with pytest.raises(ValidationError, match="multi_intent"):
        IntentResult.model_validate({**case["expected_intent"], "multi_intent": False})


def test_nested_target_agent_and_non_json_response_are_rejected():
    payload = deepcopy(FIRST["expected_intent"])
    payload["intents"][0]["target_agent"] = "bookingAgent"
    with pytest.raises(ValidationError):
        IntentResult.model_validate_json(json.dumps(payload))
    with pytest.raises(ValidationError):
        RewriteResult.model_validate_json("改写结果：明天去上海")


@pytest.mark.parametrize("model,payload", CONTRACTS, ids=[item[0].__name__ for item in CONTRACTS])
def test_json_schema_documents_required_business_fields(model, payload):
    schema = model.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(payload)
    for field in model.model_fields:
        description = schema["properties"][field]["description"]
        assert any("\u4e00" <= char <= "\u9fff" for char in description)
