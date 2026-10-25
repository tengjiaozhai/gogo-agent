"""068–085 离线调试入口，真实 SDK + 真实规划/偏好服务，无外部模型、供应商或共享库写入。

从仓库根目录运行：
    uv sync --frozen
    PYTHONPATH=src .venv/bin/python scripts/demo_068_085_context.py --case all
可选 --case routes|prompt|compress|repair|reference|memory|qa；qa 默认只打印待测问答与
旧/新提示词对照计划，加 --live 才发起 17 次真实模型请求；每个 case 的前置轮次见：
docs/契约样例/阶段G-068-085-调试指南.md 。PyCharm 选择 .venv/bin/python，工作目录为仓库根。
--report /tmp/stage-g.json 保存量测；不传则只在终端打印。所有数据库都在临时目录/内存，结束即清理。
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from agentscope.message import Msg, SystemMsg, TextBlock, ToolResultBlock, UserMsg
from agentscope.state import AgentState
from sqlalchemy import Table, create_engine
from typing import cast
from sqlalchemy.orm import sessionmaker

from gogo_agent.chat.config import master_system_prompt
from gogo_agent.chat.executor import ChatAgentExecutor, _FallbackMockModel
from gogo_agent.chat.repository import InMemoryAgentSessionStore
from gogo_agent.context.input import ModelInputContext, PROMPT_VERSION
from gogo_agent.context.preferences import PreferenceRow, PreferenceStore, PreferenceWrite
from gogo_agent.intent.models import IntentCategory, IntentItem, IntentResult, QueryInput, RecognitionDecision, ConfidenceLevel, RecognitionLayer
from gogo_agent.intent.pipeline import PreparedIntentTurn
from gogo_agent.planning.models import PlanConditions
from gogo_agent.planning.review import ApprovedTrip
from gogo_agent.planning.service import PlanError, PlanService
from gogo_agent.planning.simulated import SIMULATED_POLICY, simulated_assessors, simulated_search
from gogo_agent.planning.store import create_plan_repository
from gogo_agent.request_context import RequestContext

NOW = datetime(2026, 10, 25, 15, 59, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
CONDITIONS = PlanConditions(origin="北京", destination="上海", departure_date=date(2026, 11, 1), return_date=date(2026, 11, 3))


def request(user_id="alice", session_id="g-demo"):
    return RequestContext(user_id=user_id, session_id=session_id, request_id="demo-request")


async def transform(middleware, messages):
    async def capture(**kwargs):
        return kwargs["messages"]
    return await middleware.on_model_call(None, {"messages": messages}, capture)


class OfflineExecutor(ChatAgentExecutor):
    """仅替换模型供应商；路由和 Toolkit 来自正式执行器。"""
    def _build_model(self, stream=False, *, role="master"):
        return _FallbackMockModel(stream=stream)


async def routes():
    fixture = json.loads((ROOT / "docs/契约样例/阶段G-固定回归.json").read_text())
    assert fixture["version"] == PROMPT_VERSION
    executor = OfflineExecutor(agent_session_store=InMemoryAgentSessionStore())
    outcomes = []
    for case in fixture["cases"]:
        question = QueryInput(question=case["question"])
        result = IntentResult(
            intents=[IntentItem(intent=code, confidence=ConfidenceLevel.HIGH, reason="固定语料注入", evidence=[case["question"]])
                     for code in case["intents"]], primary_intent=case["intents"][0],
            multi_intent=len(case["intents"]) > 1, overall_reason="离线固定意图，不测识别器",
        )
        decision = RecognitionDecision(result=result, hit_layer=RecognitionLayer.RULE, confidence=ConfidenceLevel.HIGH, vector_threshold=None,
                                       candidates=[], attempted_layers=[RecognitionLayer.RULE], reason="固定语料注入")
        prepared = PreparedIntentTurn(branch="fast", original_question=question, effective_question=question,
                                      fast_decision=decision, rewrite=None, decision=decision)
        agent = executor._build_agent(state=AgentState(session_id="demo"), prepared=prepared,
                                      request=request(), tool_calls=[])
        try:
            schemas = await agent.toolkit.get_tool_schemas()
            names = [item["function"]["name"] for item in schemas]
            passed = names == case["tools"]
            assert passed, (case["question"], names)
            outcomes.append({"question": case["question"], "tools": names, "passed": passed,
                             "expected_answer_behavior": case["expected_answer_behavior"],
                             "forbidden_behavior": case["forbidden_behavior"], "real_model_result": "unverified"})
        finally:
            await executor._close_agent_model(agent)
    return {"passed": len(outcomes), "total": len(outcomes), "cases": outcomes,
            "model_tool_accuracy": "unavailable: 输入意图固定，不测模型选择质量"}


async def prompt():
    a = [SystemMsg(name="system", content=master_system_prompt("general_info", info_tool_enabled=True))]
    b = [SystemMsg(name="system", content=master_system_prompt("itinerary_planning", info_tool_enabled=False, plan_tool_enabled=True))]
    one = await transform(ModelInputContext(request(), clock=lambda: NOW), a)
    two = await transform(ModelInputContext(request("bob"), clock=lambda: NOW + timedelta(minutes=2)), b)
    stable = one[0].get_text_content() == two[0].get_text_content()
    assert stable
    return {"static_prefix_equal": stable, "times": ["2026-10-25T23:59+08:00", "2026-10-26T00:01+08:00"],
            "cache_hit_tokens": "unavailable: 未调用实际供应商", "provider_cost": "unavailable"}


async def compress():
    body = {"success": True, "data": {"currency": "CNY", "source": "simulated", "items": [
        {"id": "F1", "price": 0, "is_direct": False, "departure_time": "2026-11-01", "policy": "不可退"}
    ] * 120}}
    text = json.dumps(body, ensure_ascii=False, indent=2)
    def tool(call):
        return Msg(name="tool", role="assistant", content=[ToolResultBlock(id=call, name="get_candidates", output=[TextBlock(text=text)])])
    messages = [SystemMsg(name="system", content="固定任务"), UserMsg(name="user", content="预算1000去杭州"), tool("first"),
                UserMsg(name="user", content="改成上海，预算2000；撤回旧授权，不要下单"), tool("second")]
    raw = [m.model_dump_json() for m in messages]
    middleware = ModelInputContext(request(), clock=lambda: NOW)
    started = perf_counter()
    after = await transform(middleware, messages)
    elapsed = (perf_counter() - started) * 1000
    assert raw == [m.model_dump_json() for m in messages]
    facts_before = [m.get_text_content() for m in messages if m.role == "user"]
    facts_after = [m.get_text_content() for m in after if m.role == "user"]
    assert facts_before == facts_after
    model = _FallbackMockModel()
    try:
        tokens_before = await model.count_tokens(messages, tools=None)
        tokens_after = await model.count_tokens(after, tools=None)
    finally:
        await model.client.close()
    assert tokens_after < tokens_before
    return {**middleware.last_metrics, "sdk_estimated_tokens_before": tokens_before, "sdk_estimated_tokens_after": tokens_after,
            "estimator": "AgentScope 2.0.8 bytes/4，非供应商计费token", "local_transform_ms": round(elapsed, 3),
            "user_fact_retention": "2/2 原文一致", "raw_state_unchanged": True}


def prepare(clock=lambda: NOW):
    service = PlanService(create_plan_repository(), clock=clock, today=lambda: NOW.date())
    plan = service.create("alice", CONDITIONS)
    service.attach_candidates("alice", plan.plan_id, simulated_search(CONDITIONS))
    service.compute("alice", plan.plan_id, policy=SIMULATED_POLICY)
    return service, plan.plan_id


async def repair():
    service, pid = prepare()
    bad = ApprovedTrip(origin="广州", destination="上海")
    verdicts = []
    for index in range(3):
        final = await service.review("alice", pid, proposal_tag="综合最佳", assessors=simulated_assessors(), approved=bad)
        verdicts.append(final.verdict.value)
        if index < 2:
            service.compute("alice", pid, policy=SIMULATED_POLICY)
    try:
        service.compute("alice", pid, policy=SIMULATED_POLICY)
    except PlanError as exc:
        assert exc.code == "REPAIR_LIMIT"
        blocked = exc.code
    else:
        raise AssertionError("第三次修订未被拒绝")
    state = service.get("alice", pid)
    fixed, fixed_pid = prepare()
    candidates = simulated_search(CONDITIONS)
    # 模拟首次只搜到超政策酒店，审核后真实补搜合规候选，政策和审批依据保持相同。
    fixed.attach_candidates("alice", fixed_pid, candidates.model_copy(update={"hotels": [candidates.hotels[-1]]}))
    fixed.compute("alice", fixed_pid, policy=SIMULATED_POLICY)
    approval = ApprovedTrip(origin="北京", destination="上海")
    first = await fixed.review("alice", fixed_pid, proposal_tag="综合最佳", assessors=simulated_assessors(), approved=approval)
    assert first.needs_rework
    fixed.attach_candidates("alice", fixed_pid, candidates)
    fixed.compute("alice", fixed_pid, policy=SIMULATED_POLICY)
    final = await fixed.review("alice", fixed_pid, proposal_tag="综合最佳", assessors=simulated_assessors(), approved=approval)
    assert not final.needs_rework
    return {"unfixable_verdicts": verdicts, "repairs": state.repair_attempts, "saved_reviews": len(state.review_history),
            "third_repair": blocked, "repaired_candidate_verdict": final.verdict.value,
            "boundary": "可修复分支真实替换超标候选并重算；政策和审批依据保持相同"}


async def reference():
    clock = [NOW]
    service, pid = prepare(lambda: clock[0])
    full = service.get_candidates("alice", pid, limit=50)
    summary = service.get("alice", pid)
    assert summary.result is not None
    best = summary.result.proposals[0].combo_id
    reloaded = service.get("alice", pid)
    assert reloaded.result is not None and reloaded.result.proposals[0].combo_id == best
    try:
        service.get_candidates("bob", pid)
    except PlanError as exc:
        assert exc.code == "PLAN_NOT_FOUND"
    clock[0] += timedelta(hours=24)
    try:
        service.get_candidates("alice", pid)
    except PlanError as exc:
        assert exc.code == "NO_CANDIDATES"
    else:
        raise AssertionError("过期候选未清理")
    return {"source": full["source"], "total": full["total"], "cross_user": "PLAN_NOT_FOUND",
            "after_24h": "NO_CANDIDATES", "proposal_unchanged_by_read": True}


async def memory():
    with TemporaryDirectory(prefix="gogo-g-preference-") as directory:
        url = f"sqlite+pysqlite:///{directory}/preferences.sqlite3"
        engine = create_engine(url)
        cast(Table, PreferenceRow.__table__).create(engine)
        store = PreferenceStore(sessionmaker(engine))
        store.put("alice", "transport", PreferenceWrite(value="高铁优先", source_message="以后优先高铁"))
        engine.dispose()
        engine = create_engine(url)
        store = PreferenceStore(sessionmaker(engine))
        assert store.list("bob") == []
        assert store.list("alice")[0].value == "高铁优先"
        store.put("alice", "transport", PreferenceWrite(value="飞机优先", source_message="改为飞机优先"))
        assert store.list("alice")[0].value == "飞机优先"
        store.delete("alice", "transport")
        assert store.list("alice") == []
        engine.dispose()
    return {"reopened_database": True, "bob_preferences": [], "updated": "飞机优先", "after_revoke": []}


CASES = {"routes": routes, "prompt": prompt, "compress": compress, "repair": repair, "reference": reference, "memory": memory}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["all", *CASES], default="all")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = {"prompt_version": PROMPT_VERSION, "external_model_calls": 0}
    selected = CASES.items() if args.case == "all" else [(args.case, CASES[args.case])]
    for name, function in selected:
        report[name] = await function()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
