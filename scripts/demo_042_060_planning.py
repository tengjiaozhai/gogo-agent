"""阶段 E 行程规划（042–060）调试示例：模拟搜索、固定政策、确定性评估器，不访问网络或数据库。

最短运行路线（在仓库根目录执行）：
    PYTHONPATH=src .venv/bin/python scripts/demo_042_060_planning.py --case all

可选 --case：
    042  Plan-and-Execute 步骤推进：搜索失败从哪一步重做，日期变化如何失效
    043  途牛候选搜索（用假 CLI 包装成可执行文件）：无用户 Key 明确失败，有 Key 才得到带来源的候选
    045  确定性方案计算：组合、过滤、评分与代表方案
    053  模型主观评估（脚本化模型替身）：结构化结论、无效输出不算通过、异常可见
    054  最终审核：硬规则拦截、主观异常可见、结论绑定版本
    060  版本与归属：审核过期、跨用户访问失败

注意：043 的“有 Key”路径使用仓库内的假途牛 CLI，不访问真实途牛；053 的模型是脚本化替身。

输出中的 plan_id 由固定计数器生成，每次运行结果一致；时间固定为 2026-10-11，不读取系统时间。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import date, datetime, timezone
from itertools import count
from pathlib import Path

from agentscope.message import TextBlock
from agentscope.model import ChatResponse

from gogo_agent.integrations.tuniu import TuniuCliAdapter
from gogo_agent.planning import steps
from gogo_agent.planning.assessors import build_model_assessors
from gogo_agent.planning.models import PlanConditions, ReviewSource, ReviewVerdict
from gogo_agent.planning.review import ApprovedTrip
from gogo_agent.planning.search import PlanSearchError, TuniuPlanSearch, UnconfiguredTuniuKeys
from gogo_agent.planning.service import PlanError, PlanService
from gogo_agent.planning.simulated import (
    SIMULATED_POLICY, StaticAssessor, simulated_assessors, simulated_provider, simulated_search,
)
from gogo_agent.planning.store import create_plan_repository
from gogo_agent.planning.tools import build_plan_toolkit
from gogo_agent.request_context import RequestContext

ROOT = Path(__file__).resolve().parents[1]
FAKE_TUNIU_CLI = ROOT / "scripts" / "fake_tuniu_cli.py"
DEMO_USER_KEY = "demo-user-key-不会被打印"

TODAY = date(2026, 10, 11)
NOW = datetime(2026, 10, 11, 4, 0, tzinfo=timezone.utc)
CONDITIONS = PlanConditions(
    origin="北京", destination="上海", departure_date=date(2026, 10, 20), return_date=date(2026, 10, 22),
)
APPROVED = ApprovedTrip(origin="北京", destination="上海")


def make_service() -> PlanService:
    """每次运行使用新的内存库和固定计数器，保证输出可复现。"""
    ids = count(1)
    return PlanService(
        create_plan_repository(), clock=lambda: NOW, today=lambda: TODAY,
        id_factory=lambda: f"demo-{next(ids):03d}",
    )


def case_042() -> None:
    print("== 042 步骤推进 ==")
    service = make_service()
    plan = service.create("u001", CONDITIONS)
    print(f"创建后 next_step={steps.next_step(plan.progress).value} version={plan.version}")

    plan = service.record_search_failure("u001", plan.plan_id, "TimeoutError")
    print(f"搜索失败 1 次 next_step={steps.next_step(plan.progress).value} attempts={plan.progress.steps[1].attempts}")
    for _ in range(2):
        plan = service.record_search_failure("u001", plan.plan_id, "TimeoutError")
    print(f"连续失败 3 次 terminated={plan.progress.terminated} reason={plan.progress.termination_reason}")

    fresh = service.create("u001", CONDITIONS)
    fresh = service.attach_candidates("u001", fresh.plan_id, simulated_search(CONDITIONS))
    fresh = service.compute("u001", fresh.plan_id, policy=SIMULATED_POLICY)
    moved = PlanConditions(origin="北京", destination="上海", departure_date=date(2026, 10, 21), return_date=date(2026, 10, 23))
    changed = service.update_conditions("u001", fresh.plan_id, moved)
    print(f"改日期后 next_step={steps.next_step(changed.progress).value} candidates={changed.candidates is not None}")
    print(f"收集条件仍为 {changed.progress.steps[0].status.value}，从搜索重新执行")


class _DemoUserKeys:
    """演示用：只为 u001 提供一个假 Key；其他用户没有 Key。"""

    def get_api_key(self, user_id: str) -> str:
        if user_id == "u001":
            return DEMO_USER_KEY
        raise PlanSearchError("TUNIU_KEY_MISSING", "当前用户未配置可用的途牛 API Key，无法搜索真实候选")


def case_043() -> None:
    print("== 043 途牛候选搜索（假 CLI 包装，非真实途牛）==")
    ctx = RequestContext(user_id="u001", session_id="demo-043", request_id="demo-043-r")
    with tempfile.TemporaryDirectory() as tmp:
        wrapper = Path(tmp) / "tuniu"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_TUNIU_CLI}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
        adapter = TuniuCliAdapter(executable=str(wrapper), timeout_s=10, extra_env={"FAKE_TUNIU_ECHO_DATES": "1"})
        search = TuniuPlanSearch(adapter, _DemoUserKeys())
        no_key = TuniuPlanSearch(adapter, UnconfiguredTuniuKeys())
        try:
            asyncio.run(no_key("u001", CONDITIONS))
        except PlanSearchError as exc:
            print(f"无用户 Key code={exc.code} 说明={exc.message}")
        service = make_service()
        toolkit = build_plan_toolkit(service, ctx, search_provider=search, assessors=simulated_assessors())

        def call(name: str, **kwargs) -> dict:
            tool = asyncio.run(toolkit.get_tool(name))
            return json.loads(asyncio.run(tool.call(**kwargs)).content[0].text)

        plan_id = call("create_plan", origin="北京", destination="上海", departure_date="2026-10-20",
                       return_date="2026-10-22")["data"]["plan_id"]
        searched = call("search_candidates", plan_id=plan_id)
        plan = service.get("u001", plan_id)
        assert plan.candidates is not None
        print(f"有用户 Key success={searched['success']} 来源={plan.candidates.source} "
              f"交通={len(plan.candidates.transports)} 酒店={len(plan.candidates.hotels)}")
        print(f"密钥未出现在计划快照中={DEMO_USER_KEY not in plan.model_dump_json()}")


class _ScriptedAssessModel:
    """脚本化模型替身：按系统提示词返回三个维度的结论；不发起网络请求。"""

    def __init__(self, table: dict[str, str]) -> None:
        self._table = table

    async def __call__(self, messages, **_kwargs):
        system = messages[0].get_text_content() or ""
        key = "experience" if "出行体验" in system else "resilience" if "韧性" in system else "preference"
        value = self._table[key]
        if value == "RAISE":
            raise RuntimeError("模拟模型超时")
        text = '{"verdict": "%s", "issues": %s}' % (value, '[]' if value == "pass" else f'["{key} 存在问题"]')
        return ChatResponse(content=[TextBlock(text=text)], is_last=True)


def case_053() -> None:
    print("== 053 模型主观评估（脚本化替身）==")
    scenarios = [
        ("全部通过", {"experience": "pass", "resilience": "pass", "preference": "pass"}),
        ("体验 fail", {"experience": "fail", "resilience": "pass", "preference": "pass"}),
        ("韧性异常", {"experience": "pass", "resilience": "RAISE", "preference": "pass"}),
    ]
    for name, table in scenarios:
        service = make_service()
        plan = service.create("u001", CONDITIONS)
        service.attach_candidates("u001", plan.plan_id, simulated_search(CONDITIONS))
        service.compute("u001", plan.plan_id, policy=SIMULATED_POLICY)
        assessors = build_model_assessors(_ScriptedAssessModel(table))
        final = asyncio.run(service.review("u001", plan.plan_id, proposal_tag="综合最佳",
                                           assessors=assessors, approved=APPROVED))
        unavailable = [s.source.value for s in final.subjective if not s.available]
        print(f"{name}: verdict={final.verdict.value} needs_rework={final.needs_rework} 不可用={unavailable or '无'}")


def case_045() -> None:
    print("== 045 方案计算 ==")
    service = make_service()
    plan = service.create("u001", CONDITIONS)
    plan = service.attach_candidates("u001", plan.plan_id, simulated_search(CONDITIONS))
    plan = service.compute("u001", plan.plan_id, policy=SIMULATED_POLICY)
    result = plan.result
    assert result is not None
    print(f"来源={result.candidate_source} 组合数={result.combo_count} 被淘汰={len(result.excluded)}")
    print(f"权重={result.weights.model_dump()}")
    for item in result.proposals:
        print(
            f"- {'/'.join(item.tags)} | {item.combo_id} | 价格 {item.metrics.total_price:g} | "
            f"通勤 {item.metrics.total_transit_hhmm} | 综合 {item.scores.overall:g}"
        )
        print(f"  体验标记: {item.experience_flags or '无'}；政策违规: {item.policy_violations or '无'}")


async def case_054() -> None:
    print("== 054 最终审核 ==")
    service = make_service()
    plan = service.create("u001", CONDITIONS)
    service.attach_candidates("u001", plan.plan_id, simulated_search(CONDITIONS))
    service.compute("u001", plan.plan_id, policy=SIMULATED_POLICY)
    passed = await service.review("u001", plan.plan_id, proposal_tag="综合最佳",
                                  assessors=simulated_assessors(), approved=APPROVED)
    print(f"通过样例 verdict={passed.verdict.value} needs_rework={passed.needs_rework} 绑定内容版本={passed.plan_version}")

    unavailable = [StaticAssessor(ReviewSource.EXPERIENCE, error=RuntimeError("模拟超时")), *simulated_assessors()[1:]]
    warned = await service.review("u001", plan.plan_id, proposal_tag="综合最佳",
                                  assessors=unavailable, approved=APPROVED)
    print(f"主观异常 verdict={warned.verdict.value} 不可用维度={[s.source.value for s in warned.subjective if not s.available]}")

    hard = make_service()
    hplan = hard.create("u001", CONDITIONS)
    hotel_only = simulated_search(CONDITIONS).model_copy(update={"hotels": [h for h in simulated_search(CONDITIONS).hotels if h.id == "H3"]})
    hard.attach_candidates("u001", hplan.plan_id, hotel_only)
    hard.compute("u001", hplan.plan_id, policy=SIMULATED_POLICY)
    blocked = await hard.review("u001", hplan.plan_id, proposal_tag="综合最佳",
                                assessors=simulated_assessors(), approved=APPROVED)
    print(f"超预算样例 verdict={blocked.verdict.value} needs_rework={blocked.needs_rework}")
    for issue in blocked.objective.issues:
        if issue.severity == ReviewVerdict.FAIL:
            print(f"  硬规则: {issue.field} | {issue.evidence}")


def case_060() -> None:
    print("== 060 版本与归属 ==")
    service = make_service()
    plan = service.create("u001", CONDITIONS)
    plan = service.attach_candidates("u001", plan.plan_id, simulated_search(CONDITIONS))
    plan = service.compute("u001", plan.plan_id, policy=SIMULATED_POLICY)
    print(f"计算后 version={plan.version} content_version={plan.content_version}")
    final = asyncio.run(service.review("u001", plan.plan_id, proposal_tag="综合最佳",
                                       assessors=simulated_assessors(), approved=APPROVED))
    current = service.get("u001", plan.plan_id)
    print(f"审核后 version={current.version} content_version={current.content_version} 审核绑定内容版本={final.plan_version} 当前有效={current.review_is_current()}")
    again_review = asyncio.run(service.review("u001", plan.plan_id, proposal_tag="综合最佳",
                                              assessors=simulated_assessors(), approved=APPROVED))
    print(f"同一内容重复审核 绑定内容版本={again_review.plan_version} 仍有效={service.get('u001', plan.plan_id).review_is_current()}")
    again = service.compute("u001", plan.plan_id, policy=SIMULATED_POLICY, weather_summary="晴")
    print(f"重新计算后 version={again.version} content_version={again.content_version} 旧审核当前有效={again.review_is_current()}")
    try:
        service.get("u002", plan.plan_id)
    except PlanError as exc:
        print(f"跨用户读取 code={exc.code} status={exc.status}")


def main() -> None:
    parser = argparse.ArgumentParser(description="阶段 E 行程规划调试示例")
    parser.add_argument("--case", default="all", choices=["042", "043", "045", "053", "054", "060", "all"])
    args = parser.parse_args()
    selected = {
        "042": case_042, "043": case_043, "045": case_045, "053": case_053,
        "054": lambda: asyncio.run(case_054()), "060": case_060,
    }
    for name in ("042", "043", "045", "053", "054", "060"):
        if args.case in (name, "all"):
            selected[name]()


if __name__ == "__main__":
    main()
