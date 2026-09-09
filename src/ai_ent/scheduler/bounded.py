from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.orm import Session

from ai_ent.scheduler.execution import ClaimedExecutionRunner
from ai_ent.scheduler.finalization import ExecutionFinalizer, SuccessfulFinalizationKind
from ai_ent.scheduler.iteration import SchedulerIterationResult, SchedulerIterationService
from ai_ent.scheduler.recovery import RecoveryResult, SchedulerRecoveryService
from ai_ent.scheduler.repair import (
    FailureClassification,
    RepairExecutionResult,
    RepairExecutionService,
    RepairPlanner,
    RepairPolicy,
)

StopReason = Literal[
    "NO_READY_TASK",
    "MAX_TASKS_PER_RUN",
    "MAX_WALL_CLOCK_SECONDS",
    "MAX_FAILURES_PER_RUN",
    "HUMAN_REQUIRED",
    "BLOCK",
    "REPLAN",
    "RECONCILIATION_REQUIRED",
    "RUNTIME_ERROR",
]
TaskOutcomeStatus = Literal[
    "COMPLETED",
    "FAILED",
    "REPAIRED",
    "REPAIR_FAILED",
    "CONTENTION",
    "BLOCKED",
]


@dataclass(frozen=True)
class BoundedRunLimits:
    max_tasks_per_run: int = 1
    max_wall_clock_seconds: float = 900.0
    max_failures_per_run: int = 1
    max_repairs_per_task: int = 2
    repair_policy: RepairPolicy = field(default_factory=RepairPolicy)


@dataclass(frozen=True)
class TaskRunOutcome:
    task_id: str | None
    execution_id: str | None
    status: TaskOutcomeStatus
    commit_id: str | None = None
    success_kind: SuccessfulFinalizationKind | None = None
    repair_execution_id: str | None = None
    classification: FailureClassification | None = None
    reason: str | None = None


@dataclass(frozen=True)
class BoundedRunResult:
    run_id: str
    project_id: str
    started_at: datetime
    finished_at: datetime
    tasks_attempted: int
    tasks_completed: int
    tasks_failed: int
    repairs_attempted: int
    commits_created: tuple[str, ...]
    stop_reason: StopReason
    last_task_id: str | None = None
    last_execution_id: str | None = None
    outcomes: tuple[TaskRunOutcome, ...] = ()


Clock = Callable[[], float]


class BoundedSchedulerRunner:
    def __init__(
        self,
        *,
        scheduler: SchedulerIterationService | None = None,
        execution_runner: ClaimedExecutionRunner | None = None,
        finalizer: ExecutionFinalizer | None = None,
        repair_runner: RepairExecutionService | None = None,
        recovery: SchedulerRecoveryService | None = None,
        limits: BoundedRunLimits | None = None,
        run_id_factory: Callable[[], str] | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self.limits = limits or BoundedRunLimits()
        repair_policy = RepairPolicy(
            max_autonomous_repair_attempts=self.limits.max_repairs_per_task,
            repair_lease_duration=self.limits.repair_policy.repair_lease_duration,
            repair_owner_id=self.limits.repair_policy.repair_owner_id,
        )
        self.scheduler = scheduler or SchedulerIterationService()
        self.execution_runner = execution_runner or ClaimedExecutionRunner()
        self.finalizer = finalizer or ExecutionFinalizer()
        self.repair_runner = repair_runner or RepairExecutionService(
            planner=RepairPlanner(policy=repair_policy)
        )
        self.recovery = recovery or SchedulerRecoveryService()
        self.run_id_factory = run_id_factory or (lambda: f"run-{uuid.uuid4().hex}")
        self.clock = clock

    def recover_then_run(self, session: Session, *, project_id: str) -> tuple[RecoveryResult, BoundedRunResult | None]:
        recovery = self.recovery.recover_project(session, project_id=project_id)
        if not recovery.safe_to_continue:
            return recovery, None
        return recovery, self.run(session, project_id=project_id)

    def run(self, session: Session, *, project_id: str) -> BoundedRunResult:
        run_id = self.run_id_factory()
        started_at = datetime.now(UTC)
        started_monotonic = self.clock()
        outcomes: list[TaskRunOutcome] = []
        commits: list[str] = []
        tasks_attempted = 0
        tasks_completed = 0
        tasks_failed = 0
        repairs_attempted = 0
        last_task_id: str | None = None
        last_execution_id: str | None = None
        stop_reason: StopReason = "NO_READY_TASK"

        while True:
            if tasks_attempted >= self.limits.max_tasks_per_run:
                stop_reason = "MAX_TASKS_PER_RUN"
                break
            if self._elapsed(started_monotonic) >= self.limits.max_wall_clock_seconds:
                stop_reason = "MAX_WALL_CLOCK_SECONDS"
                break
            if tasks_failed >= self.limits.max_failures_per_run:
                stop_reason = "MAX_FAILURES_PER_RUN"
                break

            scheduled = self.scheduler.run_once(session, project_id=project_id)
            if scheduled.status == "NO_READY_TASK":
                stop_reason = "NO_READY_TASK"
                break
            if scheduled.status == "CLAIM_CONTENTION":
                outcomes.append(_scheduler_outcome(scheduled, "CONTENTION"))
                continue
            if scheduled.status != "PACKAGE_READY" or scheduled.package is None:
                outcomes.append(_scheduler_outcome(scheduled, "BLOCKED"))
                stop_reason = "RUNTIME_ERROR"
                break

            tasks_attempted += 1
            last_task_id = scheduled.package.task_id
            last_execution_id = scheduled.package.execution_id
            outcome = self._run_task(session, scheduled)
            outcomes.append(outcome.outcome)
            commits.extend(outcome.commits)
            repairs_attempted += outcome.repairs_attempted
            if outcome.outcome.status in {"COMPLETED", "REPAIRED"}:
                tasks_completed += 1
                if tasks_attempted >= self.limits.max_tasks_per_run:
                    stop_reason = self._max_task_stop_reason(session, project_id)
                    break
            else:
                tasks_failed += 1
                stop_reason = _stop_reason_for_outcome(outcome.outcome)
                break

        return BoundedRunResult(
            run_id=run_id,
            project_id=project_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            tasks_attempted=tasks_attempted,
            tasks_completed=tasks_completed,
            tasks_failed=tasks_failed,
            repairs_attempted=repairs_attempted,
            commits_created=tuple(commits),
            stop_reason=stop_reason,
            last_task_id=last_task_id,
            last_execution_id=last_execution_id,
            outcomes=tuple(outcomes),
        )

    def _elapsed(self, started: float) -> float:
        return self.clock() - started

    def _run_task(self, session: Session, scheduled: SchedulerIterationResult) -> _InternalTaskResult:
        assert scheduled.package is not None
        owner_id = self.scheduler.owner_id
        executed = self.execution_runner.run_claimed(
            session,
            package=scheduled.package,
            owner_id=owner_id,
        )
        if executed.status != "EXECUTED" or executed.package is None:
            return _InternalTaskResult(
                outcome=TaskRunOutcome(
                    task_id=scheduled.package.task_id,
                    execution_id=scheduled.package.execution_id,
                    status="FAILED",
                    reason=executed.status,
                )
            )

        finalization = self.finalizer.finalize(
            session,
            package=executed.package,
            owner_id=owner_id,
        )
        if finalization.completed:
            return _InternalTaskResult(
                outcome=TaskRunOutcome(
                    task_id=executed.package.task_id,
                    execution_id=executed.package.execution_id,
                    status="COMPLETED",
                    commit_id=finalization.commit_id,
                    success_kind=finalization.success_kind,
                ),
                commits=(finalization.commit_id,) if finalization.commit_id else (),
            )

        failure_execution_id = finalization.execution.id if finalization.execution is not None else scheduled.package.execution_id
        repairs_attempted = 0
        repair_result: RepairExecutionResult | None = None
        while repairs_attempted < self.limits.max_repairs_per_task:
            repair_result = self.repair_runner.run_once(session, failed_execution_id=failure_execution_id)
            if repair_result.status == "REPAIR_NOT_ALLOWED":
                break
            if repair_result.decision.next_execution is not None:
                repairs_attempted += 1
            if repair_result.status == "REPAIRED" and repair_result.finalization is not None:
                commit = repair_result.finalization.commit_id
                repair_execution_id = repair_result.decision.next_execution.id if repair_result.decision.next_execution else None
                return _InternalTaskResult(
                    outcome=TaskRunOutcome(
                        task_id=executed.package.task_id,
                        execution_id=executed.package.execution_id,
                        status="REPAIRED",
                        commit_id=commit,
                        success_kind=repair_result.finalization.success_kind,
                        repair_execution_id=repair_execution_id,
                        classification=repair_result.decision.classification,
                    ),
                    commits=(commit,) if commit else (),
                    repairs_attempted=repairs_attempted,
                )
            if repair_result.finalization is None or repair_result.finalization.execution is None:
                break
            failure_execution_id = repair_result.finalization.execution.id

        assert repair_result is not None
        repair_execution_id = repair_result.decision.next_execution.id if repair_result.decision.next_execution else None
        return _InternalTaskResult(
            outcome=TaskRunOutcome(
                task_id=executed.package.task_id,
                execution_id=executed.package.execution_id,
                status="REPAIR_FAILED",
                repair_execution_id=repair_execution_id,
                classification=repair_result.followup_classification or repair_result.decision.classification,
                reason=repair_result.status,
            ),
            repairs_attempted=repairs_attempted,
        )

    def _max_task_stop_reason(self, session: Session, project_id: str) -> StopReason:
        readiness = getattr(self.scheduler, "readiness", None)
        if readiness is not None and not readiness.list_ready_tasks(session, project_id=project_id, limit=1):
            return "NO_READY_TASK"
        return "MAX_TASKS_PER_RUN"


@dataclass(frozen=True)
class _InternalTaskResult:
    outcome: TaskRunOutcome
    commits: tuple[str, ...] = ()
    repairs_attempted: int = 0


def _scheduler_outcome(result: SchedulerIterationResult, status: TaskOutcomeStatus) -> TaskRunOutcome:
    return TaskRunOutcome(
        task_id=result.task.id if result.task is not None else None,
        execution_id=result.execution.id if result.execution is not None else None,
        status=status,
        reason=result.reason or result.status,
    )


def _stop_reason_for_outcome(outcome: TaskRunOutcome) -> StopReason:
    classification = outcome.classification
    if classification is None:
        return "RUNTIME_ERROR"
    if classification.retryability == "RECONCILIATION_REQUIRED":
        return "RECONCILIATION_REQUIRED"
    if classification.retryability == "HUMAN_REQUIRED":
        return "HUMAN_REQUIRED"
    if outcome.status == "BLOCKED":
        return "BLOCK"
    return "MAX_FAILURES_PER_RUN"
