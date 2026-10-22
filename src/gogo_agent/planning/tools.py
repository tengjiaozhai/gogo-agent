"""043/044 规划 Agent 的模型可见工具：身份只从闭包 RequestContext 注入，模型不能传 userId。

工具只读取候选、计算方案和保存审核；没有差旅单写入、供应商下单或取消能力。
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, Toolkit
from pydantic import ValidationError

from gogo_agent.request_context import RequestContext

from .models import (
    DimensionWeights, PlanConditions, PlanPolicy, PlanStepName, PlanView, PreferenceScores, ReviewVerdict,
)
from .review import ApprovedTrip, SubjectiveAssessor
from .search import PlanSearchError
from .service import PlanError, PlanService
from .steps import next_step

SearchProvider = Callable[[str, PlanConditions], Any]


def _ok(data: dict[str, Any], message: str = "成功") -> str:
    return json.dumps({"success": True, "message": message, "data": data}, ensure_ascii=False)


def _fail(code: str, message: str, *, field: str | None = None) -> str:
    """失败回执；`field` 指出出错的具体参数，让模型能自行修正而不是盲目重试。"""
    payload: dict[str, Any] = {"success": False, "errorCode": code, "message": message}
    if field is not None:
        payload["field"] = field
    return json.dumps(payload, ensure_ascii=False)


def _invalid_field(name: str, value: Any, model: type) -> str:
    """把 Pydantic 的字段级错误翻译成模型可读的一句中文，指明参数名与期望结构。"""
    try:
        model.model_validate(value)
    except ValidationError as exc:
        error = exc.errors()[0]
        location = ".".join(str(part) for part in error["loc"]) or "顶层"
        reason = error["msg"]
        return f"参数 {name} 不合法：字段 {location} {reason}；期望结构见该参数的类型定义"
    return f"参数 {name} 不合法"


def _summary(plan: PlanView) -> dict[str, Any]:
    """面向模型的计划摘要：只含引用、版本、下一步和带来源的方案，不含完整候选。"""
    pending = next_step(plan.progress)
    data: dict[str, Any] = {
        "plan_id": plan.plan_id,
        "version": plan.version,
        "next_step": pending.value if pending is not None else None,
        "terminated": plan.progress.terminated,
        "termination_reason": plan.progress.termination_reason,
        "review_current": plan.review_is_current(),
    }
    if plan.candidates is not None:
        data["candidate_source"] = plan.candidates.source
        data["candidate_captured_at"] = plan.candidates.captured_at.isoformat()
    if plan.result is not None:
        data["proposals"] = [
            {
                "tags": p.tags, "combo_id": p.combo_id, "total_price": p.metrics.total_price,
                "total_transit": p.metrics.total_transit_hhmm, "overall": p.scores.overall,
                "experience_flags": p.experience_flags, "policy_violations": p.policy_violations,
                "warnings": p.warnings,
            }
            for p in plan.result.proposals
        ]
        data["policy_version"] = plan.result.policy.version
    if plan.last_review is not None:
        data["last_review"] = {
            "proposal_tag": plan.last_review.proposal_tag, "verdict": plan.last_review.verdict.value,
            "needs_rework": plan.last_review.needs_rework, "plan_version": plan.last_review.plan_version,
        }
    return data


def build_plan_toolkit(
    service: PlanService,
    request: RequestContext,
    *,
    search_provider: SearchProvider,
    assessors: Sequence[SubjectiveAssessor],
    approved: ApprovedTrip | None = None,
) -> Toolkit:
    """注册规划工具；所有操作都限定在 request.user_id 的计划之内。"""
    user_id = request.user_id

    def create_plan(
        origin: str = "", destination: str = "", departure_date: str = "", return_date: str = "",
        preferences: str = "", budget: float = 0,
    ) -> str:
        """收集到出发城市、目的城市、去程与返程日期后创建计划；缺少字段时只返回需要补充的内容，不创建计划。"""
        missing = [label for label, value in (
            ("出发城市", origin), ("目的城市", destination), ("去程日期", departure_date), ("返程日期", return_date),
        ) if not value.strip()]
        if missing:
            return _fail("NEEDS_INPUT", "请先补充：" + "、".join(missing))
        try:
            conditions = PlanConditions(
                origin=origin.strip(), destination=destination.strip(),
                departure_date=date.fromisoformat(departure_date.strip()),
                return_date=date.fromisoformat(return_date.strip()),
                preferences=preferences, budget=budget or None,
            )
        except ValueError:
            return _fail("INVALID_PARAM", "日期格式应为 YYYY-MM-DD，且返程日期必须晚于去程日期")
        plan = service.create(user_id, conditions)
        return _ok(_summary(plan), "计划已创建，请继续搜索候选")

    async def search_candidates(plan_id: str) -> str:
        """按计划条件搜索交通与酒店候选并保存；搜索失败只记录该步骤的失败次数。"""
        try:
            plan = service.get(user_id, plan_id)
        except PlanError as exc:
            return _fail(exc.code, str(exc))
        try:
            outcome = search_provider(user_id, plan.conditions)
            candidates = await outcome if inspect.isawaitable(outcome) else outcome
        except PlanSearchError as exc:  # 只返回固定中文说明
            failed = service.record_search_failure(user_id, plan_id, exc.code)
            pending = next_step(failed.progress)
            hint = pending.value if pending is not None else "无"
            return _fail("SEARCH_FAILED", f"{exc.message}。当前下一步：{hint}")
        except Exception as exc:  # 外部异常只暴露类型，不把供应商原文交给模型
            failed = service.record_search_failure(user_id, plan_id, type(exc).__name__)
            pending = next_step(failed.progress)
            hint = pending.value if pending is not None else "无"
            return _fail("SEARCH_FAILED", f"搜索失败（{type(exc).__name__}），可稍后重试。当前下一步：{hint}")
        saved = service.attach_candidates(user_id, plan_id, candidates)
        return _ok(_summary(saved), f"已保存 {len(candidates.transports)} 条交通、{len(candidates.hotels)} 家酒店候选")

    def compute_plan(
        plan_id: str,
        scores: PreferenceScores | None = None,
        policy: PlanPolicy | None = None,
        weights: DimensionWeights | None = None,
        weather_summary: str = "",
    ) -> str:
        """基于已保存候选计算代表方案；金额与时间全部来自确定性计算，不由模型估算。

        Args:
            plan_id: 已创建并有候选的计划标识。
            scores: 偏好分，结构为 {"transport_scores": {"F1": {"score": 0-100, "basis": ["理由"]}},
                "hotel_scores": {"H1": {...}}}；不传表示不提供偏好分。
            policy: 数值化差旅政策，如 {"flight_class": "经济舱", "train_seat_class": "二等座",
                "hotel_star_limit": 3}；只接受数值或枚举字段，不接受自然语言描述。不传表示不做政策检查。
            weights: 四个维度权重 {"time": 0.5, "price": 0.3, "preference": 0.1, "experience": 0.1}，
                先归一化再参与评分；不传使用项目默认权重。
            weather_summary: 目的地天气摘要，仅作说明文字，不参与打分。
        """
        # FunctionTool 直接展开 JSON 为 dict，不做 Pydantic 校验，因此这里显式校验以便给出字段级错误。
        parsed: dict[str, Any] = {}
        for name, value, model in (
            ("scores", scores, PreferenceScores),
            ("policy", policy, PlanPolicy),
            ("weights", weights, DimensionWeights),
        ):
            if value is None:
                parsed[name] = None
                continue
            if not isinstance(value, (model, dict)):
                return _fail(
                    "INVALID_PARAM",
                    f"参数 {name} 必须是对象或省略；期望结构见该参数的类型定义",
                    field=name,
                )
            try:
                parsed[name] = value if isinstance(value, model) else model.model_validate(value)
            except ValidationError:
                return _fail("INVALID_PARAM", _invalid_field(name, value, model), field=name)
        try:
            plan = service.compute(
                user_id, plan_id, scores=parsed["scores"], policy=parsed["policy"],
                weights=parsed["weights"], weather_summary=weather_summary,
            )
        except PlanError as exc:
            return _fail(exc.code, str(exc))
        return _ok(_summary(plan), "方案已计算")

    def get_plan(plan_id: str) -> str:
        """读取本人计划的当前状态、版本和方案摘要。"""
        try:
            return _ok(_summary(service.get(user_id, plan_id)))
        except PlanError as exc:
            return _fail(exc.code, str(exc))

    async def review_plan(plan_id: str, proposal_tag: str) -> str:
        """对指定代表方案（综合最佳/时间最短/价格最低/最符合偏好）做独立审核；硬规则失败必须整改。"""
        try:
            final = await service.review(
                user_id, plan_id, proposal_tag=proposal_tag, assessors=assessors, approved=approved,
            )
        except PlanError as exc:
            return _fail(exc.code, str(exc))
        blocking = [issue.evidence for issue in final.arbitration.issues if issue.severity == ReviewVerdict.FAIL]
        return _ok({
            "review_id": final.review_id, "plan_version": final.plan_version,
            "verdict": final.verdict.value, "needs_rework": final.needs_rework,
            "blocking_issues": blocking,
            "warnings": [issue.evidence for issue in final.objective.issues if issue.severity == ReviewVerdict.WARNING],
            "next_step": PlanStepName.AWAIT_CONFIRMATION.value if not final.needs_rework else PlanStepName.COMPUTE_PROPOSALS.value,
        }, "审核完成")

    allowed = PermissionDecision(behavior=PermissionBehavior.ALLOW, message="允许受控行程规划与只读审核")
    read_only = {get_plan}
    functions = (create_plan, search_candidates, compute_plan, get_plan, review_plan)
    return Toolkit(tools=[
        FunctionTool(function, name=function.__name__, is_read_only=function in read_only,
                     is_concurrency_safe=False, permission=allowed)
        for function in functions
    ])
