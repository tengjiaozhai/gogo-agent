"""042–060 规划服务：计划生命周期、步骤推进与版本控制。

本服务只读搜索结果、计算方案和保存审核，不创建、修改或提交差旅单，也不触发供应商下单。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from . import steps
from .models import (
    CandidateSet, DimensionWeights, FinalReview, PlanConditions, PlanPolicy, PlanProgress,
    PlanStepName, PlanView, PreferenceScores,
)
from .review import ApprovedTrip, ReviewError, SubjectiveAssessor, review_plan
from .scoring import PlanningError, compute_plan
from .store import PlanRepository

BUSINESS_TZ = timezone(timedelta(hours=8))


class PlanError(ValueError):
    """计划操作失败，带稳定错误码与建议的 HTTP 语义状态。"""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _business_today() -> date:
    return datetime.now(BUSINESS_TZ).date()


class PlanService:
    """单一规划状态入口；每次保存都会递增版本，使旧审核自动失效。"""

    def __init__(
        self,
        repository: PlanRepository,
        *,
        clock: Callable[[], datetime] = _utc_now,
        today: Callable[[], date] = _business_today,
        id_factory: Callable[[], str] = lambda: uuid4().hex,
    ):
        self.repository = repository
        self._clock = clock
        self._today = today
        self._id_factory = id_factory

    def get(self, user_id: str, plan_id: str) -> PlanView:
        """读取本人计划；不存在与越权访问返回相同错误，避免探测他人计划。"""
        plan = self.repository.get(user_id, plan_id)
        if plan is None:
            raise PlanError("PLAN_NOT_FOUND", "计划不存在或不属于当前用户", 404)
        return plan

    def create(self, user_id: str, conditions: PlanConditions, order_id: str | None = None) -> PlanView:
        """收集条件完成后创建计划，版本从 1 开始。"""
        now = self._clock()
        progress = steps.complete(PlanProgress(), PlanStepName.COLLECT_CONDITIONS, data_version=1)
        plan = PlanView(
            plan_id=self._id_factory(), version=1, content_version=1, order_id=order_id, conditions=conditions,
            progress=progress, created_at=now, updated_at=now,
        )
        self.repository.create(user_id, plan)
        return plan

    def _commit(self, user_id: str, before: PlanView, after: PlanView, *, content_changed: bool) -> PlanView:
        """每次保存递增 version 以拒绝并发覆盖；只有内容变化才递增 content_version，审核据此判断有效性。"""
        bumped = after.model_copy(update={
            "version": before.version + 1,
            "content_version": before.content_version + (1 if content_changed else 0),
            "updated_at": self._clock(),
        })
        self.repository.save(user_id, bumped, expected_version=before.version)
        return bumped

    def update_conditions(self, user_id: str, plan_id: str, conditions: PlanConditions) -> PlanView:
        """条件变化时从搜索步骤起失效，候选、方案与审核都不再适用于新条件。"""
        plan = self.get(user_id, plan_id)
        if conditions == plan.conditions:
            return plan
        progress = steps.invalidate_from(plan.progress, PlanStepName.SEARCH_CANDIDATES)
        after = plan.model_copy(update={
            "conditions": conditions, "progress": progress, "candidates": None, "result": None,
        })
        return self._commit(user_id, plan, after, content_changed=True)

    def attach_candidates(self, user_id: str, plan_id: str, candidates: CandidateSet) -> PlanView:
        """保存搜索候选；旧方案和旧审核随之失效。"""
        plan = self.get(user_id, plan_id)
        progress = steps.complete(plan.progress, PlanStepName.SEARCH_CANDIDATES, data_version=plan.version)
        progress = steps.invalidate_from(progress, PlanStepName.COMPUTE_PROPOSALS)
        after = plan.model_copy(update={"candidates": candidates, "progress": progress, "result": None})
        return self._commit(user_id, plan, after, content_changed=True)

    def record_search_failure(self, user_id: str, plan_id: str, error: str) -> PlanView:
        """搜索失败只记录该步骤的失败次数；下次从搜索步骤重新执行。"""
        plan = self.get(user_id, plan_id)
        progress = steps.fail(plan.progress, PlanStepName.SEARCH_CANDIDATES, error)
        return self._commit(user_id, plan, plan.model_copy(update={"progress": progress}), content_changed=False)

    def compute(
        self, user_id: str, plan_id: str, *, scores: PreferenceScores | None = None,
        policy: PlanPolicy | None = None, weights: DimensionWeights | None = None,
        weather_summary: str = "",
    ) -> PlanView:
        """基于当前候选计算方案；计算失败记入步骤失败次数，不生成部分结果。"""
        plan = self.get(user_id, plan_id)
        if plan.candidates is None:
            raise PlanError("NO_CANDIDATES", "尚未搜索候选，不能计算方案")
        try:
            result = compute_plan(
                plan.candidates, origin=plan.conditions.origin, destination=plan.conditions.destination,
                return_date=plan.conditions.return_date.isoformat(), policy=policy, scores=scores,
                weights=weights, weather_summary=weather_summary, preferences=plan.conditions.preferences,
                now=self._clock(),
            )
        except PlanningError as exc:
            progress = steps.fail(plan.progress, PlanStepName.COMPUTE_PROPOSALS, str(exc))
            self._commit(user_id, plan, plan.model_copy(update={"progress": progress}), content_changed=False)
            raise PlanError(exc.code, str(exc)) from None
        progress = steps.complete(plan.progress, PlanStepName.COMPUTE_PROPOSALS, data_version=plan.version)
        progress = steps.invalidate_from(progress, PlanStepName.REVIEW)
        after = plan.model_copy(update={"result": result, "progress": progress})
        return self._commit(user_id, plan, after, content_changed=True)

    async def review(
        self, user_id: str, plan_id: str, *, proposal_tag: str,
        assessors: Sequence[SubjectiveAssessor], approved: ApprovedTrip | None = None,
    ) -> FinalReview:
        """对当前内容版本执行审核；审核结论绑定该内容版本，之后的内容变化都会使其失效。"""
        plan = self.get(user_id, plan_id)
        try:
            final = await review_plan(
                plan, proposal_tag=proposal_tag, assessors=assessors, approved=approved,
                today=self._today(), review_id=self._id_factory(), now=self._clock(),
                plan_version=plan.content_version,
            )
        except ReviewError as exc:
            raise PlanError(exc.code, str(exc), exc.status) from None
        progress = steps.complete(plan.progress, PlanStepName.REVIEW, data_version=plan.content_version)
        after = plan.model_copy(update={"progress": progress, "last_review": final})
        self._commit(user_id, plan, after, content_changed=False)
        return final
