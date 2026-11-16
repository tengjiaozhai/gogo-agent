"""改口词守卫的单元测试：只验证判定与命中词，不涉及模型与 ReMe。"""

from __future__ import annotations

import pytest

from gogo_agent.context.cue_guard import CUE_TERMS, guard_fires, matched_cues


def test_baseline_terms_unchanged() -> None:
    """词表是设计基线；改动它会同时改变线上行为与规则层评测指标。"""
    assert CUE_TERMS == ("以后", "改成", "不再", "换成")


@pytest.mark.parametrize(
    "utterance",
    [
        "以后乘飞机都改成靠窗座位。",
        "我不再要靠过道了，乘飞机以后选靠窗。",
        "以后报销发票换成电子版。",
    ],
)
def test_guard_fires_on_restatement_with_cue(utterance: str) -> None:
    assert guard_fires(utterance) is True


@pytest.mark.parametrize(
    "utterance",
    [
        "从今天起乘飞机都要靠窗。",
        "出差坐飞机还是想要靠过道座位。",
        "下周去成都，帮我看看航班。",
    ],
)
def test_guard_stays_closed_without_cue(utterance: str) -> None:
    """无改口词时不放行：跨轮改口与一次性表述都在此止步，属已知漏判。"""
    assert guard_fires(utterance) is False


def test_matched_cues_reports_every_hit_in_terms_order() -> None:
    """诊断用：同一句含多个词时全部返回，顺序固定为词表顺序，便于日志对比。"""
    assert matched_cues("以后不再坐过道，改成靠窗，旧票换成电子版") == ("以后", "改成", "不再", "换成")


def test_matched_cues_empty_when_nothing_matches() -> None:
    assert matched_cues("这次出差想坐靠窗") == ()


def test_guard_is_case_and_whitespace_insensitive_to_context_only() -> None:
    """守卫只看词是否出现，不做语气、范围或主题判断。

    这条用例固定"故意不做"的边界：不应替换的句子只要含词也会被放行，
    由模型层的动作判定与 id 校验兜底，规则层不承担该职责。
    """
    assert guard_fires("如果以后改成靠窗，你帮我看看能不能订。") is True
