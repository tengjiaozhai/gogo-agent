"""042 Plan-and-Execute 的步骤状态机：只做状态转移，不执行搜索、计算或写操作。

AgentScope 2.0.8 没有 Java `PlanNotebook` 的等价模块；这里用可序列化的进度模型承接，
并随计划记录持久化，使重启、续跑和多实例都能从同一份状态判断“下一步从哪里重做”。
"""

from __future__ import annotations

from .models import PlanProgress, PlanStepName, PlanStepState, PlanStepStatus

MAX_STEP_ATTEMPTS = 3
STEP_ORDER = tuple(PlanStepName)


class StepTransitionError(ValueError):
    """违反步骤顺序或在终止后继续推进时抛出，带稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _index(step: PlanStepName) -> int:
    return STEP_ORDER.index(step)


def _state(progress: PlanProgress, step: PlanStepName) -> PlanStepState:
    return progress.steps[_index(step)]


def _require_active(progress: PlanProgress) -> None:
    if progress.terminated:
        raise StepTransitionError("PLAN_TERMINATED", progress.termination_reason or "计划已终止")


def next_step(progress: PlanProgress) -> PlanStepName | None:
    """第一个尚未完成的步骤；全部完成返回 None。重新执行从这里开始。"""
    for state in progress.steps:
        if state.status != PlanStepStatus.COMPLETED:
            return state.name
    return None


def complete(progress: PlanProgress, step: PlanStepName, data_version: int) -> PlanProgress:
    """标记步骤完成；前序步骤必须已完成，并记录其所基于的计划版本。"""
    _require_active(progress)
    updated = progress.model_copy(deep=True)
    for earlier in STEP_ORDER[: _index(step)]:
        if _state(updated, earlier).status != PlanStepStatus.COMPLETED:
            raise StepTransitionError("STEP_ORDER", f"步骤 {earlier.value} 尚未完成，不能完成 {step.value}")
    target = _state(updated, step)
    target.status = PlanStepStatus.COMPLETED
    target.error = None
    target.data_version = data_version
    return updated


def fail(progress: PlanProgress, step: PlanStepName, error: str) -> PlanProgress:
    """记录失败；达到最大重试次数后计划终止，不再自动重试。"""
    _require_active(progress)
    updated = progress.model_copy(deep=True)
    target = _state(updated, step)
    target.status = PlanStepStatus.FAILED
    target.attempts += 1
    target.error = error
    if target.attempts >= MAX_STEP_ATTEMPTS:
        updated.terminated = True
        updated.termination_reason = f"步骤 {step.value} 连续失败 {target.attempts} 次：{error}"
    return updated


def need_input(progress: PlanProgress, step: PlanStepName, message: str) -> PlanProgress:
    """步骤因缺少用户输入暂停；补齐后重新执行同一步。"""
    _require_active(progress)
    updated = progress.model_copy(deep=True)
    target = _state(updated, step)
    target.status = PlanStepStatus.NEEDS_INPUT
    target.error = message
    return updated


def invalidate_from(progress: PlanProgress, step: PlanStepName) -> PlanProgress:
    """条件变化后，从受影响步骤起把已完成步骤置为 stale；更早的步骤保持有效。"""
    _require_active(progress)
    updated = progress.model_copy(deep=True)
    for later in STEP_ORDER[_index(step):]:
        state = _state(updated, later)
        if state.status != PlanStepStatus.PENDING:
            state.status = PlanStepStatus.STALE
            state.error = None
            state.data_version = None
    return updated


def terminate(progress: PlanProgress, reason: str) -> PlanProgress:
    """用户取消或外部终止；已提交写操作记录保留。"""
    updated = progress.model_copy(deep=True)
    updated.terminated = True
    updated.termination_reason = reason
    return updated


def committed_write(progress: PlanProgress, idempotency_key: str) -> str | None:
    """若同一幂等键的外部写操作已确认，返回其引用，调用方必须复用而非重复执行。"""
    return progress.committed_writes.get(idempotency_key)


def record_write(progress: PlanProgress, idempotency_key: str, reference: str) -> PlanProgress:
    """记录已确认的外部写操作；重规划、重试和回放都不会清除它。"""
    updated = progress.model_copy(deep=True)
    existing = updated.committed_writes.get(idempotency_key)
    if existing is not None and existing != reference:
        raise StepTransitionError("WRITE_CONFLICT", "同一幂等键已绑定到另一个外部引用")
    updated.committed_writes[idempotency_key] = reference
    return updated
