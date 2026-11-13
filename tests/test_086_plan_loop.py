'''086 计划执行闭环：步骤状态机的重做起点、连续失败终止、补充输入后重做、外部写入幂等。

被测对象是 gogo_agent.planning.steps。无候选、用户修改预算走 PlanService 真实路径；审核不通过的真实路径用 monkeypatch 把 review_plan 替换为 needs_rework 结论，不调用模型或外部服务。天气失败：规划路径中天气只是 compute 工具的文字摘要（planning/tools.py:210-222），不产生独立失败，这里以“计算步骤失败”的状态转移模拟（推断）。steps.need_input、steps.terminate、steps.record_write 在 src 中没有业务调用方（rg 核对），对应用例只证明状态机本身。仓库为内存替身，不连接数据库或 Redis。
'''

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from gogo_agent.planning import service as service_module
from gogo_agent.planning import steps
from gogo_agent.planning.models import (
    CandidateSet, DimensionWeights, FinalReview, PlanConditions, PlanPolicy, PlanProgress, PlanResult,
    PlanStepName, PlanStepState, PlanStepStatus, PlanView, ReviewResult, ReviewSource, ReviewVerdict,
)
from gogo_agent.planning.service import PlanError, PlanService
from gogo_agent.planning.steps import MAX_STEP_ATTEMPTS, StepTransitionError

NOW = datetime(2026, 10, 11, 13, 48, tzinfo=UTC)
COND = PlanConditions(
    origin='北京', destination='上海', departure_date=date(2026, 10, 20),
    return_date=date(2026, 10, 22), budget=3000,
)
COLLECT = PlanStepName.COLLECT_CONDITIONS
SEARCH = PlanStepName.SEARCH_CANDIDATES
COMPUTE = PlanStepName.COMPUTE_PROPOSALS
REVIEW = PlanStepName.REVIEW
CONFIRM = PlanStepName.AWAIT_CONFIRMATION
EMPTY = CandidateSet(transports=[], hotels=[], source='simulated', captured_at=NOW)
PLAN_RESULT = PlanResult(
    weights=DimensionWeights(), policy=PlanPolicy(), combo_count=0, proposals=[],
    candidate_source='simulated', generated_at=NOW,
)


class MemoryPlanRepository:
    '''内存仓库替身，只实现 PlanService 用到的 create / get / save；版本不符时抛普通异常，不用 assert。'''

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], PlanView] = {}

    def create(self, user_id: str, plan: PlanView) -> None:
        self.rows[(user_id, plan.plan_id)] = plan

    def get(self, user_id: str, plan_id: str) -> PlanView | None:
        return self.rows.get((user_id, plan_id))

    def save(self, user_id: str, plan: PlanView, *, expected_version: int) -> None:
        if self.rows[(user_id, plan.plan_id)].version != expected_version:
            raise RuntimeError('测试替身检测到版本冲突')
        self.rows[(user_id, plan.plan_id)] = plan


def make_service(repo: MemoryPlanRepository) -> PlanService:
    return PlanService(repo, clock=lambda: NOW, today=lambda: date(2026, 10, 11), id_factory=lambda: 'plan-1')


def progress_through(*done: PlanStepName) -> PlanProgress:
    '''用状态机自身按顺序完成前缀步骤，得到合法的进度前置条件。'''
    progress = PlanProgress()
    for name in done:
        progress = steps.complete(progress, name, data_version=1)
    return progress


def state_of(progress: PlanProgress, name: PlanStepName) -> PlanStepState:
    return next(state for state in progress.steps if state.name is name)


def make_plan(progress: PlanProgress, candidates: CandidateSet | None = None, result: PlanResult | None = None) -> PlanView:
    return PlanView(
        plan_id='plan-1', version=1, content_version=1, session_id='s-1', conditions=COND,
        progress=progress, candidates=candidates, result=result,
        candidates_expires_at=NOW + timedelta(hours=24) if candidates is not None else None,
        created_at=NOW, updated_at=NOW,
    )


def seeded_repo(progress: PlanProgress, candidates: CandidateSet | None = None, result: PlanResult | None = None) -> MemoryPlanRepository:
    repo = MemoryPlanRepository()
    repo.create('alice', make_plan(progress, candidates, result))
    return repo


def stub_review_needs_rework(monkeypatch) -> None:
    '''用 needs_rework 结论替换审核入口；审核内部规则不在本测试范围内。'''

    async def rework(plan, **kwargs):
        return FinalReview(
            review_id='review-1', plan_id=plan.plan_id, plan_version=plan.content_version,
            proposal_tag='综合最佳', verdict=ReviewVerdict.FAIL, needs_rework=True,
            objective=ReviewResult(source=ReviewSource.OBJECTIVE, verdict=ReviewVerdict.FAIL),
            arbitration=ReviewResult(source=ReviewSource.ARBITRATOR, verdict=ReviewVerdict.FAIL),
            reviewed_at=NOW,
        )

    monkeypatch.setattr(service_module, 'review_plan', rework)


def test_086_weather_failure_retries_from_compute_step():
    '''天气失败（推断为计算步骤失败）：从计算步骤重做；恢复后向前推进，不回到搜索。'''
    progress = progress_through(COLLECT, SEARCH)
    assert steps.next_step(progress) is COMPUTE  # 前置：计算是唯一的重做起点

    failed = steps.fail(progress, COMPUTE, '天气服务超时')
    assert steps.next_step(failed) is COMPUTE
    assert state_of(failed, COMPUTE).status is PlanStepStatus.FAILED
    assert (state_of(failed, COMPUTE).attempts, state_of(failed, COMPUTE).error) == (1, '天气服务超时')
    assert failed.terminated is False

    recovered = steps.complete(failed, COMPUTE, data_version=2)
    assert steps.next_step(recovered) is REVIEW  # 恢复后向前推进，不回到搜索
    assert state_of(recovered, COMPUTE).error is None


def test_086_no_candidates_fails_compute_step_on_real_service_path():
    repo = MemoryPlanRepository()
    service = make_service(repo)
    created = service.create('alice', COND, session_id='s-1')
    searched = service.attach_candidates('alice', created.plan_id, EMPTY)
    assert steps.next_step(searched.progress) is COMPUTE  # 前置：搜索已完成，计算是下一步

    with pytest.raises(PlanError) as raised:
        service.compute('alice', created.plan_id)
    assert raised.value.code == 'NO_CANDIDATES'

    after = service.get('alice', created.plan_id)
    assert state_of(after.progress, COMPUTE).status is PlanStepStatus.FAILED
    assert state_of(after.progress, COMPUTE).attempts == 1
    assert steps.next_step(after.progress) is COMPUTE and after.progress.terminated is False


@pytest.mark.asyncio
async def test_086_review_not_passed_is_kept_as_evidence(monkeypatch):
    repo = seeded_repo(progress_through(COLLECT, SEARCH, COMPUTE))
    assert steps.next_step(repo.get('alice', 'plan-1').progress) is REVIEW  # 前置：计算完成，审核是下一步
    stub_review_needs_rework(monkeypatch)

    final = await make_service(repo).review('alice', 'plan-1', proposal_tag='综合最佳', assessors=[])
    saved = repo.get('alice', 'plan-1')
    assert final.needs_rework is True and saved.last_review == final  # 不通过的结论保存为证据
    assert len(saved.review_history) == 1 and saved.progress.terminated is False


@pytest.mark.asyncio
async def test_086_review_not_passed_should_restart_at_compute(monkeypatch):
    repo = seeded_repo(progress_through(COLLECT, SEARCH, COMPUTE))
    stub_review_needs_rework(monkeypatch)
    await make_service(repo).review('alice', 'plan-1', proposal_tag='综合最佳', assessors=[])
    assert steps.next_step(repo.get('alice', 'plan-1').progress) is COMPUTE


def test_086_rework_branch_restarts_at_compute_after_review_is_invalidated():
    '''状态机约定：审核后要求整改时，调用方让计算及之后的步骤失效，重做起点才是计算。'''
    progress = progress_through(COLLECT, SEARCH, COMPUTE, REVIEW)
    assert steps.next_step(progress) is CONFIRM  # 前置：审核已记录完成
    rework = steps.invalidate_from(progress, COMPUTE)
    assert steps.next_step(rework) is COMPUTE
    assert state_of(rework, SEARCH).status is PlanStepStatus.COMPLETED  # 更早的步骤保持有效
    assert state_of(rework, COMPUTE).status is PlanStepStatus.STALE
    assert state_of(rework, REVIEW).status is PlanStepStatus.STALE
    assert rework.terminated is False


def test_086_budget_change_restarts_from_search_and_clears_stale_results():
    repo = seeded_repo(progress_through(COLLECT, SEARCH, COMPUTE, REVIEW), candidates=EMPTY, result=PLAN_RESULT)
    service = make_service(repo)
    plan = repo.get('alice', 'plan-1')
    assert plan.conditions.budget == 3000 and plan.result is not None  # 前置：存在旧方案结果
    assert steps.next_step(plan.progress) is CONFIRM

    unchanged = service.update_conditions('alice', 'plan-1', COND)
    assert unchanged.result is not None and steps.next_step(unchanged.progress) is CONFIRM  # 对照：条件未变则不失效

    changed = service.update_conditions('alice', 'plan-1', COND.model_copy(update={'budget': 2000}))
    assert changed.conditions.budget == 2000
    assert steps.next_step(changed.progress) is SEARCH  # 预算变化：从搜索重做
    assert state_of(changed.progress, COLLECT).status is PlanStepStatus.COMPLETED
    assert state_of(changed.progress, SEARCH).status is PlanStepStatus.STALE
    assert changed.candidates is None and changed.result is None  # 旧候选与旧方案被清空
    assert changed.progress.terminated is False


def test_086_missing_input_pauses_same_step_and_redoes_it_after_answer():
    progress = progress_through(COLLECT, SEARCH)
    assert steps.next_step(progress) is COMPUTE  # 前置：计算是下一步
    with pytest.raises(StepTransitionError) as skipped:
        steps.complete(progress, REVIEW, data_version=1)
    assert skipped.value.code == 'STEP_ORDER'  # 前置：不能跳过计算去审核

    waiting = steps.need_input(progress, COMPUTE, '缺少预算上限')
    assert state_of(waiting, COMPUTE).status is PlanStepStatus.NEEDS_INPUT
    assert state_of(waiting, COMPUTE).error == '缺少预算上限'
    assert steps.next_step(waiting) is COMPUTE and state_of(waiting, COMPUTE).attempts == 0  # 同一步重做，且不计入失败
    assert waiting.terminated is False

    answered = steps.complete(waiting, COMPUTE, data_version=2)
    assert state_of(answered, COMPUTE).status is PlanStepStatus.COMPLETED
    assert steps.next_step(answered) is REVIEW


def test_086_consecutive_failures_terminate_the_plan_at_limit():
    progress = progress_through(COLLECT, SEARCH)
    assert steps.next_step(progress) is COMPUTE  # 前置：计算是下一步

    for attempt in range(1, MAX_STEP_ATTEMPTS):
        progress = steps.fail(progress, COMPUTE, f'第 {attempt} 次失败')
        assert state_of(progress, COMPUTE).attempts == attempt and progress.terminated is False

    final = steps.fail(progress, COMPUTE, f'第 {MAX_STEP_ATTEMPTS} 次失败')
    assert final.terminated is True and state_of(final, COMPUTE).attempts == MAX_STEP_ATTEMPTS
    assert f'连续失败 {MAX_STEP_ATTEMPTS} 次' in final.termination_reason
    assert steps.next_step(final) is COMPUTE  # 终止后不前移，重做起点保留
    with pytest.raises(StepTransitionError) as fail_after:
        steps.fail(final, COMPUTE, '再试')
    with pytest.raises(StepTransitionError) as complete_after:
        steps.complete(final, COMPUTE, data_version=3)
    assert fail_after.value.code == complete_after.value.code == 'PLAN_TERMINATED'


def test_086_failures_separated_by_success_should_not_accumulate():
    progress = progress_through(COLLECT, SEARCH)
    progress = steps.fail(progress, COMPUTE, '第 1 次')
    progress = steps.fail(progress, COMPUTE, '第 2 次')
    progress = steps.complete(progress, COMPUTE, data_version=2)
    assert state_of(progress, COMPUTE).status is PlanStepStatus.COMPLETED  # 前置：中间成功过一次
    progress = steps.invalidate_from(progress, COMPUTE)
    progress = steps.fail(progress, COMPUTE, '第 3 次（已非连续）')
    assert progress.terminated is False


def test_086_committed_write_is_reused_and_conflicting_reference_is_rejected():
    assert steps.committed_write(PlanProgress(), 'book-key-1') is None  # 前置：尚未确认写入

    recorded = steps.record_write(PlanProgress(), 'book-key-1', 'ext-1')
    assert steps.committed_write(recorded, 'book-key-1') == 'ext-1'  # 复用同一引用
    assert steps.record_write(recorded, 'book-key-1', 'ext-1') == recorded  # 同一引用重放是幂等的
    with pytest.raises(StepTransitionError) as conflict:
        steps.record_write(recorded, 'book-key-1', 'ext-2')
    assert conflict.value.code == 'WRITE_CONFLICT'

    done = steps.record_write(progress_through(COLLECT, SEARCH), 'book-key-1', 'ext-1')
    assert state_of(done, SEARCH).status is PlanStepStatus.COMPLETED  # 前置：已有完成步骤与写入记录
    replanned = steps.invalidate_from(done, COLLECT)  # 重规划使步骤失效
    assert state_of(replanned, SEARCH).status is PlanStepStatus.STALE
    cancelled = steps.terminate(replanned, '用户取消')
    assert steps.committed_write(cancelled, 'book-key-1') == 'ext-1' and cancelled.terminated is True  # 写入记录保留
    assert steps.committed_write(recorded, 'other-key') is None
