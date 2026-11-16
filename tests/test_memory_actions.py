"""memory_actions 的单元测试：条目解析、动作校验、状态转换与降级。

全部不连网络：模型调用用返回固定 JSON 的替身，状态转换直接调纯函数。
"""

from __future__ import annotations

import asyncio
import json

from agentscope.message import TextBlock
from agentscope.model import ChatResponse

from gogo_agent.context import memory_actions as ma

TODAY = "2026-11-16"
PLANE = ma.Entry(entry_id="a1b2c3d4", fact="用户乘飞机时偏好靠过道座位", day="2026-11-13")


class _FixedModel:
    """返回固定文本的模型替身，记录调用参数。"""

    def __init__(self, text: str, *, finished_reason: str = "completed"):
        self.text = text
        self.finished_reason = finished_reason
        self.calls: list[tuple] = []

    async def __call__(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return ChatResponse(
            content=[TextBlock(text=self.text)], is_last=True, finished_reason=self.finished_reason,
        )


def _body(*entries: ma.Entry) -> str:
    return ma.render_body(list(entries))


# ---------------------------------------------------------------- 解析与渲染


def test_parse_entries_reads_metadata_and_identity() -> None:
    """带注释的行解析出 id 与来源，且正文不含注释。"""
    body = (
        "# 自动记忆\n\n"
        "- 2026-11-13 用户乘飞机时偏好靠过道座位 <!-- id:a1b2c3d4 src:r-42 -->\n"
    )
    (entry,) = ma.parse_entries(body)
    assert (entry.entry_id, entry.fact, entry.day, entry.source) == (
        "a1b2c3d4", "用户乘飞机时偏好靠过道座位", "2026-11-13", "r-42",
    )


def test_parse_entries_tolerates_legacy_lines_without_metadata() -> None:
    """旧行没有注释：按内容派生 id，来源留空（惰性迁移，不批量改写文件）。"""
    (entry,) = ma.parse_entries("# 自动记忆\n\n- 用户住酒店偏好大床房\n")
    assert len(entry.entry_id) == 8 and entry.entry_id.isalnum()
    assert entry.source is None and entry.day is None


def test_parse_entries_is_stable_and_case_insensitive_for_duplicates() -> None:
    """同一内容重复出现只保留第一条；id 只由内容决定，与日期无关。"""
    body = (
        "# 自动记忆\n\n"
        "- 2026-11-13 用户偏好大床房\n"
        "- 2026-11-15 用户偏好大床房\n"
    )
    (entry,) = ma.parse_entries(body)
    assert entry.day == "2026-11-13"
    assert entry.entry_id == ma._fact_id("用户偏好大床房")


def test_round_trip_render_and_parse() -> None:
    """渲染与解析互逆，保证读改写在多轮之后不漂移。"""
    entries = [
        ma.Entry(entry_id="aaaaaaaa", fact="用户偏好靠窗座位", day=TODAY, source="r-1"),
        ma.Entry(entry_id="bbbbbbbb", fact="用户住酒店偏好大床房"),
    ]
    assert ma.parse_entries(_body(*entries)) == entries


def test_render_retired_keeps_identity_and_records_reason() -> None:
    line = ma.render_retired(PLANE, ma.SUPERSEDED)
    assert line == (
        "- 2026-11-13 用户乘飞机时偏好靠过道座位 <!-- id:a1b2c3d4 reason:superseded -->"
    )


# ---------------------------------------------------------------- 动作校验


def _ids(*entries: ma.Entry) -> set[str]:
    return {entry.entry_id for entry in entries}


def test_validate_rejects_unknown_id_and_drops_whole_turn() -> None:
    actions, error = ma.validate_actions(
        [{"action": "REFINE", "id": "deadbeef", "content": "用户偏好靠窗座位"}],
        existing_ids=_ids(PLANE),
    )
    assert actions == [] and error == "invalid_action:0:unknown_id"


def test_validate_rejects_duplicate_id_within_one_turn() -> None:
    """同轮两次引用同一条目会让状态来源不明，整轮丢弃。"""
    actions, error = ma.validate_actions(
        [
            {"action": "NOOP", "id": "a1b2c3d4"},
            {"action": "REPLACE", "id": "a1b2c3d4", "content": "用户偏好靠窗座位"},
        ],
        existing_ids=_ids(PLANE),
    )
    assert actions == [] and error == "invalid_action:1:duplicate_id"


def test_validate_rejects_add_with_id_and_noop_with_content() -> None:
    assert ma.validate_actions(
        [{"action": "ADD", "id": "a1b2c3d4", "content": "用户偏好靠窗座位"}],
        existing_ids=_ids(PLANE),
    )[1] == "invalid_action:0:add_has_id"
    assert ma.validate_actions(
        [{"action": "NOOP", "id": "a1b2c3d4", "content": "用户偏好靠窗座位"}],
        existing_ids=_ids(PLANE),
    )[1] == "invalid_action:0:noop_has_content"


def test_validate_rejects_unknown_action_kind() -> None:
    assert ma.validate_actions(
        [{"action": "ARCHIVE", "id": "a1b2c3d4"}], existing_ids=_ids(PLANE),
    )[1] == "invalid_action:0:unknown_kind"


def test_validate_drops_sensitive_and_overlong_content() -> None:
    """清洗复用 normalize_facts：敏感内容与超长陈述都不通过。"""
    assert ma.validate_actions(
        [{"action": "ADD", "content": "用户的银行卡密码是123456"}], existing_ids=set(),
    )[1] == "invalid_action:0:add_invalid_content"
    assert ma.validate_actions(
        [{"action": "ADD", "content": "用户" + "很" * 100}], existing_ids=set(),
    )[1] == "invalid_action:0:add_invalid_content"


def test_validate_counts_add_overflow_instead_of_silently_dropping() -> None:
    """超过每轮 5 条时只保留前 5 条，但计入告警而不是无声截断。"""
    raw = [{"action": "ADD", "content": f"用户事实{i}"} for i in range(7)]
    actions, error = ma.validate_actions(raw, existing_ids=set())
    assert error is None
    assert [a.content for a in actions] == [f"用户事实{i}" for i in range(5)]


# ---------------------------------------------------------------- 状态转换


def test_replace_moves_old_entry_to_history_and_records_new_one() -> None:
    change = ma.apply_actions(
        [PLANE],
        [ma.Action(ma.REPLACE, "a1b2c3d4", "用户乘飞机时偏好靠窗座位")],
        utterance="以后乘飞机都改成靠窗。",
        today=TODAY,
        source="r-43",
    )
    active = ma.parse_entries(change.body)
    assert [entry.fact for entry in active] == ["用户乘飞机时偏好靠窗座位"]
    assert active[0].source == "r-43"
    assert any("reason:superseded" in line for line in change.evicted)
    assert change.applied == 1


def test_replace_downgrades_to_add_without_cue_term() -> None:
    """守卫未放行：旧条目保留，新事实另起一条（Q12 接受的已知漏判）。"""
    change = ma.apply_actions(
        [PLANE],
        [ma.Action(ma.REPLACE, "a1b2c3d4", "用户乘飞机时偏好靠窗座位")],
        utterance="我最近更喜欢靠窗了。",
        today=TODAY,
    )
    facts = [entry.fact for entry in ma.parse_entries(change.body)]
    assert facts == ["用户乘飞机时偏好靠过道座位", "用户乘飞机时偏好靠窗座位"]
    assert change.evicted == ()


def test_refine_keeps_identity_and_retires_previous_text() -> None:
    change = ma.apply_actions(
        [PLANE],
        [ma.Action(ma.REFINE, "a1b2c3d4", "用户乘飞机时偏好靠过道座位，仅限国内航班")],
        utterance="国内航班我还是靠过道。",
        today=TODAY,
    )
    (active,) = ma.parse_entries(change.body)
    assert active.entry_id == "a1b2c3d4"
    assert active.fact == "用户乘飞机时偏好靠过道座位，仅限国内航班"
    assert any("reason:refined" in line for line in change.evicted)


def test_refine_with_unchanged_content_changes_nothing() -> None:
    change = ma.apply_actions(
        [PLANE], [ma.Action(ma.REFINE, "a1b2c3d4", PLANE.fact)], utterance="", today=TODAY,
    )
    assert change.applied == 0 and change.evicted == ()


def test_noop_and_duplicate_add_leave_memory_untouched() -> None:
    change = ma.apply_actions(
        [PLANE],
        [ma.Action(ma.NOOP, "a1b2c3d4"), ma.Action(ma.ADD, None, PLANE.fact)],
        utterance="以后还是靠过道。",
        today=TODAY,
    )
    assert change.applied == 0
    assert [entry.fact for entry in ma.parse_entries(change.body)] == [PLANE.fact]


def test_overflow_evicts_oldest_entries_to_history() -> None:
    """超出上限时从最早条目开始退役，活跃集合永不超过上限。"""
    entries = [
        ma.Entry(entry_id=f"{i:08d}", fact=f"用户事实{i}", day=TODAY) for i in range(3)
    ]
    change = ma.apply_actions(
        entries, [ma.Action(ma.ADD, None, "用户事实新")], utterance="", today=TODAY, limit=3,
    )
    facts = [entry.fact for entry in ma.parse_entries(change.body)]
    assert facts == ["用户事实1", "用户事实2", "用户事实新"]
    assert any("用户事实0" in line and "reason:evicted" in line for line in change.evicted)


# ---------------------------------------------------------------- 决策调用


def _payload(**kwargs) -> str:
    return json.dumps(kwargs, ensure_ascii=False)


def test_decide_actions_returns_validated_actions() -> None:
    model = _FixedModel(_payload(actions=[{"action": "NOOP", "id": "a1b2c3d4"}]))
    decision = asyncio.run(ma.decide_actions(model, [PLANE], "还是靠过道", "好的"))
    assert decision.error is None
    assert decision.actions == (ma.Action(ma.NOOP, "a1b2c3d4"),)


def test_decide_actions_reports_truncation_as_error_not_partial_result() -> None:
    """输出被截断时不采用半截动作：截断意味着 JSON 不完整，任何解析都是猜。"""
    model = _FixedModel(_payload(actions=[]), finished_reason="length")
    decision = asyncio.run(ma.decide_actions(model, [PLANE], "q", "a"))
    assert decision.actions == () and decision.error == "truncated"


def test_decide_actions_rejects_invalid_json_without_retry() -> None:
    model = _FixedModel("not json")
    decision = asyncio.run(ma.decide_actions(model, [PLANE], "q", "a"))
    assert decision.actions == () and decision.error == "invalid_json"
    assert len(model.calls) == 1


def test_decide_actions_propagates_unknown_id_as_turn_error() -> None:
    model = _FixedModel(_payload(actions=[{"action": "REFINE", "id": "deadbeef", "content": "用户偏好靠窗"}]))
    decision = asyncio.run(ma.decide_actions(model, [PLANE], "q", "a"))
    assert decision.actions == () and decision.error == "invalid_action:0:unknown_id"


def test_decide_actions_caps_prompt_context_and_logs_truncation(caplog) -> None:
    """条目过多时裁剪上下文并留下日志，避免静默丢信息。"""
    entries = [ma.Entry(entry_id=f"{i:08d}", fact="用户" + "很" * 70) for i in range(200)]
    model = _FixedModel(_payload(actions=[]))
    with caplog.at_level("WARNING"):
        asyncio.run(ma.decide_actions(model, entries, "q", "a"))
    messages, _ = model.calls[0]
    payload = "".join(block.text for block in messages[1].content if isinstance(block, TextBlock))
    assert "其余条目省略" in payload
    assert any("context truncated" in record.message for record in caplog.records)
