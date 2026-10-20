"""043 行程规划接入聊天：途牛候选只用当前用户的 Key、失败显式、主协调只看到规划工具。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from gogo_agent.chat.config import master_system_prompt
from gogo_agent.chat.executor import ChatAgentExecutor
from gogo_agent.intent.models import IntentCategory
from gogo_agent.integrations.tuniu import TuniuCliAdapter
from gogo_agent.planning.dependencies import get_plan_service, reset_plan_service
from gogo_agent.planning.models import PlanConditions, PlanStepName
from gogo_agent.planning.search import PlanSearchError, TuniuPlanSearch, UnconfiguredTuniuKeys
from gogo_agent.planning.simulated import SIMULATED_POLICY, simulated_assessors
from gogo_agent.planning.tools import build_plan_toolkit
from gogo_agent.request_context import RequestContext

ROOT = Path(__file__).resolve().parents[1]
FAKE_CLI = ROOT / "scripts" / "fake_tuniu_cli.py"
CONDITIONS = PlanConditions(
    origin="北京", destination="上海", departure_date=date(2026, 10, 20), return_date=date(2026, 10, 22),
)
SECRET = "sk-test-user-key-do-not-leak"


class UserKeys:
    """只为 u001 提供 Key；其他用户明确缺失，用于验证不会回退。"""

    def get_api_key(self, user_id: str) -> str:
        if user_id == "u001":
            return SECRET
        raise PlanSearchError("TUNIU_KEY_MISSING", "当前用户未配置可用的途牛 API Key，无法搜索真实候选")


@pytest.fixture
def cli_wrapper(tmp_path: Path) -> str:
    """把假 CLI 包装成可执行文件：SkillRunner 要求 executable 可被 shutil.which 找到。"""
    wrapper = tmp_path / "tuniu"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLI}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return str(wrapper)


def adapter_for(executable: str, *, mode: str = "ok") -> TuniuCliAdapter:
    return TuniuCliAdapter(
        executable=executable, timeout_s=10,
        extra_env={"FAKE_TUNIU_ECHO_DATES": "1", "FAKE_TUNIU_MODE": mode},
    )


def ctx(user_id: str = "u001") -> RequestContext:
    return RequestContext(user_id=user_id, session_id="s-043", request_id="r-043")


def run(coro):
    return asyncio.run(coro)


def tool_names(toolkit) -> list[str]:
    """通过 AgentScope 的公开接口读取工具名，不依赖内部字段。"""
    if toolkit is None:
        return []
    return [item["function"]["name"] for item in run(toolkit.get_tool_schemas())]


@pytest.fixture(autouse=True)
def fresh_plan_service():
    reset_plan_service()
    yield
    reset_plan_service()


def test_043_search_without_user_key_fails_explicitly_and_never_calls_cli(cli_wrapper):
    search = TuniuPlanSearch(adapter_for(cli_wrapper), UnconfiguredTuniuKeys())
    with pytest.raises(PlanSearchError) as caught:
        run(search("u001", CONDITIONS))
    assert caught.value.code == "TUNIU_KEY_SERVICE_UNCONFIGURED"
    assert SECRET not in caught.value.message


def test_043_other_user_without_key_does_not_borrow_another_users_key(cli_wrapper):
    search = TuniuPlanSearch(adapter_for(cli_wrapper), UserKeys())
    with pytest.raises(PlanSearchError) as caught:
        run(search("u002", CONDITIONS))
    assert caught.value.code == "TUNIU_KEY_MISSING"


def test_043_search_with_user_key_builds_sourced_candidates(cli_wrapper):
    candidates = run(TuniuPlanSearch(adapter_for(cli_wrapper), UserKeys())("u001", CONDITIONS))
    assert candidates.source == "tuniu_cli"
    assert any(t.id.startswith("F") and t.origin == "北京" for t in candidates.transports)
    assert any(t.id.startswith("T") and t.destination == "北京" for t in candidates.transports)
    assert candidates.hotels and candidates.hotels[0].nights == 2


def test_043_vendor_failure_becomes_explicit_search_failure_without_key_leak(cli_wrapper):
    search = TuniuPlanSearch(adapter_for(cli_wrapper, mode="fail"), UserKeys())
    with pytest.raises(PlanSearchError) as caught:
        run(search("u001", CONDITIONS))
    assert caught.value.code == "TUNIU_SEARCH_FAILED"
    assert SECRET not in caught.value.message


def test_043_end_to_end_tool_path_with_tuniu_candidates_is_sourced_and_no_order(cli_wrapper):
    search = TuniuPlanSearch(adapter_for(cli_wrapper), UserKeys())
    toolkit = build_plan_toolkit(
        get_plan_service(), ctx(), search_provider=search, assessors=simulated_assessors(),
    )

    def call(name: str, **kwargs):
        tool = run(toolkit.get_tool(name))
        return json.loads(run(tool.call(**kwargs)).content[0].text)

    created = call("create_plan", origin="北京", destination="上海", departure_date="2026-10-20",
                   return_date="2026-10-22")
    plan_id = created["data"]["plan_id"]
    searched = call("search_candidates", plan_id=plan_id)
    assert searched["success"] is True
    computed = call("compute_plan", plan_id=plan_id, policy_json=SIMULATED_POLICY.model_dump_json())
    assert computed["data"]["candidate_source"] == "tuniu_cli"
    assert computed["data"]["proposals"]
    reviewed = call("review_plan", plan_id=plan_id, proposal_tag="综合最佳")
    assert reviewed["success"] is True
    plan = get_plan_service().get("u001", plan_id)
    assert plan.order_id is None
    search_step = next(step for step in plan.progress.steps if step.name is PlanStepName.SEARCH_CANDIDATES)
    assert search_step.status.value == "completed"


def test_043_tool_reports_missing_key_to_model_without_secret(cli_wrapper):
    toolkit = build_plan_toolkit(
        get_plan_service(), ctx("u002"), search_provider=TuniuPlanSearch(adapter_for(cli_wrapper), UserKeys()),
        assessors=[],
    )

    def call(name: str, **kwargs):
        tool = run(toolkit.get_tool(name))
        return json.loads(run(tool.call(**kwargs)).content[0].text)

    plan_id = call("create_plan", origin="北京", destination="上海", departure_date="2026-10-20",
                   return_date="2026-10-22")["data"]["plan_id"]
    failed = call("search_candidates", plan_id=plan_id)
    assert failed["errorCode"] == "SEARCH_FAILED"
    assert "未配置" in failed["message"]
    assert SECRET not in json.dumps(failed, ensure_ascii=False)


def _executor(monkeypatch) -> ChatAgentExecutor:
    monkeypatch.setenv("GOGO_MODEL_API_KEY", "dummy-key")
    monkeypatch.setenv("GOGO_MODEL_BASE_URL", "https://gateway.example.test/v1")
    monkeypatch.setenv("GOGO_MODEL_NAME", "chat-model")
    monkeypatch.setenv("GOGO_STABLE_MODEL_NAME", "stable-model")
    return ChatAgentExecutor(
        chat_history_service=SimpleNamespace(), agent_session_store=_MemoryStore(),
        pipeline_factory=None, active_continuation=SimpleNamespace(),
    )


class _MemoryStore:
    def __init__(self):
        self.saved = {}

    def load_agent_state(self, session_id, agent_name):
        return None

    def save_agent_state(self, session_id, state, agent_name):
        self.saved[agent_name] = state

    def delete_agent_state(self, session_id, agent_name):
        self.saved.pop(agent_name, None)

    def delete_active_agent(self, session_id):
        return None


def _prepared(intent: IntentCategory):
    return SimpleNamespace(decision=SimpleNamespace(result=SimpleNamespace(
        intents=[SimpleNamespace(intent=intent)],
    )))


def test_043_planning_intent_registers_plan_tool_and_prompt(monkeypatch):
    executor = _executor(monkeypatch)
    agent = executor._build_agent(
        state=None, prepared=_prepared(IntentCategory.ITINERARY_PLANNING), request=ctx(),
        tool_calls=[], stream=False,
    )
    assert "itinerary_plan_agent" in tool_names(agent.toolkit)


def test_043_non_planning_intent_does_not_expose_plan_tool(monkeypatch):
    executor = _executor(monkeypatch)
    agent = executor._build_agent(
        state=None, prepared=_prepared(IntentCategory.GREETING), request=ctx(),
        tool_calls=[], stream=False,
    )
    assert "itinerary_plan_agent" not in tool_names(agent.toolkit)


def test_043_master_prompt_only_mentions_enabled_tools_and_no_booking_claims():
    text = master_system_prompt("itinerary_planning", info_tool_enabled=False, plan_tool_enabled=True)
    assert "itinerary_plan_agent" in text and "itinerary_manage_agent" not in text
    assert "不得声称已预订" in text
    assert "行程规划尚未接入" not in text


def test_043_plan_agent_builder_exposes_planning_toolkit_only(monkeypatch):
    executor = _executor(monkeypatch)
    agent, review_model = executor._build_plan_agent(ctx())
    try:
        names = tool_names(agent.toolkit)
        assert {"create_plan", "search_candidates", "compute_plan", "get_plan", "review_plan"} <= set(names)
        assert not any("book" in name or "cancel" in name or "submit" in name for name in names)
    finally:
        run(review_model.client.close())
        run(agent.model.client.close())
