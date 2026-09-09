from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_ent.bootstrap.models import ExecutionPackage
from ai_ent.persistence.models import Execution, Task
from ai_ent.scheduler.bounded import BoundedRunLimits, BoundedSchedulerRunner
from ai_ent.scheduler.execution import ClaimedExecutionResult, ClaimedExecutionStatus
from ai_ent.scheduler.finalization import ExecutionFinalizationResult
from ai_ent.scheduler.iteration import SchedulerIterationResult
from ai_ent.scheduler.repair import (
    FailureClassification,
    RepairDecision,
    RepairExecutionResult,
)


def package(task_id: str, execution_id: str) -> ExecutionPackage:
    return ExecutionPackage(
        repository_path=Path("/repo"),
        worktree_path=Path("/worktree") / task_id / execution_id,
        task_id=task_id,
        execution_id=execution_id,
        instructions=f"Do {task_id}",
        allowed_paths=("tests/fixtures/**",),
        prohibited_paths=(".env",),
        python_path=Path("/python"),
        timeout_seconds=30,
    )


class FakeScheduler:
    owner_id = "worker-1"

    def __init__(self, results: list[SchedulerIterationResult]) -> None:
        self.results = results
        self.calls = 0

    def run_once(self, _: Any, *, project_id: str) -> SchedulerIterationResult:
        self.calls += 1
        if self.results:
            return self.results.pop(0)
        return SchedulerIterationResult(status="NO_READY_TASK", project_id=project_id)


class FakeExecutionRunner:
    def __init__(self, statuses: list[ClaimedExecutionStatus] | None = None) -> None:
        self.statuses = statuses or []
        self.calls = 0

    def run_claimed(self, _: Any, *, package: ExecutionPackage, owner_id: str) -> ClaimedExecutionResult:
        self.calls += 1
        status = self.statuses.pop(0) if self.statuses else "EXECUTED"
        return ClaimedExecutionResult(status=status, package=package, execution=Execution(id=package.execution_id, task_id=package.task_id, executor_type="codex"))


class FakeFinalizer:
    def __init__(self, results: list[ExecutionFinalizationResult]) -> None:
        self.results = results
        self.calls = 0

    def finalize(self, _: Any, *, package: ExecutionPackage, owner_id: str) -> ExecutionFinalizationResult:
        self.calls += 1
        return self.results.pop(0)


class FakeRepairRunner:
    def __init__(self, results: list[RepairExecutionResult]) -> None:
        self.results = results
        self.calls = 0

    def run_once(self, _: Any, *, failed_execution_id: str) -> RepairExecutionResult:
        self.calls += 1
        return self.results.pop(0)


class FakeClock:
    def __init__(self, values: list[float]) -> None:
        self.values = values

    def __call__(self) -> float:
        return self.values.pop(0) if self.values else 999.0


def scheduled(task_id: str) -> SchedulerIterationResult:
    pkg = package(task_id, f"execution-{task_id}")
    return SchedulerIterationResult(
        status="PACKAGE_READY",
        project_id="project-1",
        task=Task(id=task_id, project_id="project-1", title=f"Task {task_id}"),
        execution=Execution(id=pkg.execution_id, task_id=task_id, executor_type="codex"),
        package=pkg,
    )


def completed(task_id: str, execution_id: str, commit: str) -> ExecutionFinalizationResult:
    return ExecutionFinalizationResult(
        status="COMPLETED",
        execution=Execution(id=execution_id, task_id=task_id, executor_type="codex", commit_hash=commit),
        task=Task(id=task_id, project_id="project-1", title=f"Task {task_id}", status="passed"),
        commit_id=commit,
        success_kind="NEW_VERIFIED_CHANGE",
    )


def noop_completed(task_id: str, execution_id: str) -> ExecutionFinalizationResult:
    return ExecutionFinalizationResult(
        status="COMPLETED",
        execution=Execution(id=execution_id, task_id=task_id, executor_type="codex"),
        task=Task(id=task_id, project_id="project-1", title=f"Task {task_id}", status="passed"),
        commit_id=None,
        success_kind="ALREADY_SATISFIED_NOOP",
    )


def failed(task_id: str, execution_id: str) -> ExecutionFinalizationResult:
    return ExecutionFinalizationResult(
        status="VERIFICATION_FAILED",
        execution=Execution(id=execution_id, task_id=task_id, executor_type="codex", status="failed", error_classification="verification_failed"),
        task=Task(id=task_id, project_id="project-1", title=f"Task {task_id}", status="blocked"),
    )


def repair_result(status: str, *, repaired: bool = False) -> RepairExecutionResult:
    classification = FailureClassification("VERIFICATION_FAILURE", "VERIFICATION", "REPAIRABLE", "verification failed")
    task = Task(id="TASK-A", project_id="project-1", title="Task A")
    execution = Execution(id="execution-TASK-A", task_id="TASK-A", executor_type="codex", status="failed")
    repair_execution = Execution(id="repair-1", task_id="TASK-A", executor_type="codex", attempt=2)
    decision = RepairDecision(
        classification=classification,
        action="REPAIR_WITH_NEW_EXECUTION",
        failed_execution=execution,
        task=task,
        repair_attempt_count=0,
        max_attempts=2,
        next_execution=repair_execution,
        next_package=package("TASK-A", "repair-1"),
    )
    finalization = completed("TASK-A", "repair-1", "abc123") if repaired else failed("TASK-A", "repair-1")
    return RepairExecutionResult(
        status=status,  # type: ignore[arg-type]
        decision=decision,
        finalization=finalization,
        followup_classification=classification,
    )


def test_zero_ready_tasks_stops_immediately() -> None:
    runner = BoundedSchedulerRunner(
        scheduler=FakeScheduler([SchedulerIterationResult(status="NO_READY_TASK", project_id="project-1")]),  # type: ignore[arg-type]
        limits=BoundedRunLimits(max_tasks_per_run=3),
    )

    result = runner.run(None, project_id="project-1")  # type: ignore[arg-type]

    assert result.stop_reason == "NO_READY_TASK"
    assert result.tasks_attempted == 0
    assert result.tasks_completed == 0


def test_two_ready_tasks_complete_sequentially() -> None:
    scheduler = FakeScheduler([scheduled("TASK-A"), scheduled("TASK-B")])
    execution_runner = FakeExecutionRunner()
    finalizer = FakeFinalizer([
        completed("TASK-A", "execution-TASK-A", "aaa111"),
        completed("TASK-B", "execution-TASK-B", "bbb222"),
    ])
    runner = BoundedSchedulerRunner(
        scheduler=scheduler,  # type: ignore[arg-type]
        execution_runner=execution_runner,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
        limits=BoundedRunLimits(max_tasks_per_run=2),
    )

    result = runner.run(None, project_id="project-1")  # type: ignore[arg-type]

    assert result.stop_reason == "MAX_TASKS_PER_RUN"
    assert result.tasks_attempted == 2
    assert result.tasks_completed == 2
    assert result.commits_created == ("aaa111", "bbb222")
    assert execution_runner.calls == 2
    assert finalizer.calls == 2


def test_max_tasks_one_stops_after_first_task() -> None:
    scheduler = FakeScheduler([scheduled("TASK-A"), scheduled("TASK-B")])
    runner = BoundedSchedulerRunner(
        scheduler=scheduler,  # type: ignore[arg-type]
        execution_runner=FakeExecutionRunner(),  # type: ignore[arg-type]
        finalizer=FakeFinalizer([completed("TASK-A", "execution-TASK-A", "aaa111")]),  # type: ignore[arg-type]
        limits=BoundedRunLimits(max_tasks_per_run=1),
    )

    result = runner.run(None, project_id="project-1")  # type: ignore[arg-type]

    assert result.stop_reason == "MAX_TASKS_PER_RUN"
    assert result.tasks_attempted == 1
    assert scheduler.calls == 1


def test_noop_success_counts_completed_without_counting_commit() -> None:
    runner = BoundedSchedulerRunner(
        scheduler=FakeScheduler([scheduled("TASK-A")]),  # type: ignore[arg-type]
        execution_runner=FakeExecutionRunner(),  # type: ignore[arg-type]
        finalizer=FakeFinalizer([noop_completed("TASK-A", "execution-TASK-A")]),  # type: ignore[arg-type]
        limits=BoundedRunLimits(max_tasks_per_run=1),
    )

    result = runner.run(None, project_id="project-1")  # type: ignore[arg-type]

    assert result.tasks_completed == 1
    assert result.commits_created == ()
    assert result.outcomes[0].success_kind == "ALREADY_SATISFIED_NOOP"


def test_wall_clock_budget_prevents_starting_additional_work() -> None:
    runner = BoundedSchedulerRunner(
        scheduler=FakeScheduler([scheduled("TASK-A")]),  # type: ignore[arg-type]
        limits=BoundedRunLimits(max_tasks_per_run=3, max_wall_clock_seconds=1),
        clock=FakeClock([10.0, 12.0]),
    )

    result = runner.run(None, project_id="project-1")  # type: ignore[arg-type]

    assert result.stop_reason == "MAX_WALL_CLOCK_SECONDS"
    assert result.tasks_attempted == 0


def test_repairable_failure_repairs_then_continues() -> None:
    scheduler = FakeScheduler([scheduled("TASK-A"), scheduled("TASK-B")])
    runner = BoundedSchedulerRunner(
        scheduler=scheduler,  # type: ignore[arg-type]
        execution_runner=FakeExecutionRunner(),  # type: ignore[arg-type]
        finalizer=FakeFinalizer([
            failed("TASK-A", "execution-TASK-A"),
            completed("TASK-B", "execution-TASK-B", "bbb222"),
        ]),  # type: ignore[arg-type]
        repair_runner=FakeRepairRunner([repair_result("REPAIRED", repaired=True)]),  # type: ignore[arg-type]
        limits=BoundedRunLimits(max_tasks_per_run=2, max_repairs_per_task=1),
    )

    result = runner.run(None, project_id="project-1")  # type: ignore[arg-type]

    assert result.tasks_attempted == 2
    assert result.tasks_completed == 2
    assert result.repairs_attempted == 1
    assert result.commits_created == ("abc123", "bbb222")


def test_failed_repair_stops_without_next_task() -> None:
    scheduler = FakeScheduler([scheduled("TASK-A"), scheduled("TASK-B")])
    runner = BoundedSchedulerRunner(
        scheduler=scheduler,  # type: ignore[arg-type]
        execution_runner=FakeExecutionRunner(),  # type: ignore[arg-type]
        finalizer=FakeFinalizer([failed("TASK-A", "execution-TASK-A")]),  # type: ignore[arg-type]
        repair_runner=FakeRepairRunner([repair_result("REPAIR_FAILED")]),  # type: ignore[arg-type]
        limits=BoundedRunLimits(max_tasks_per_run=2, max_repairs_per_task=1),
    )

    result = runner.run(None, project_id="project-1")  # type: ignore[arg-type]

    assert result.tasks_attempted == 1
    assert result.tasks_failed == 1
    assert result.stop_reason == "MAX_FAILURES_PER_RUN"
    assert scheduler.calls == 1


def test_human_required_and_reconciliation_stop_run() -> None:
    human = FailureClassification("POLICY_SECURITY_FAILURE", "SCOPE_VALIDATION", "HUMAN_REQUIRED", "scope")
    db = FailureClassification("DB_FINALIZATION_FAILURE", "DB_FINALIZATION", "RECONCILIATION_REQUIRED", "db")
    for classification, expected in ((human, "HUMAN_REQUIRED"), (db, "RECONCILIATION_REQUIRED")):
        task = Task(id="TASK-A", project_id="project-1", title="Task A")
        execution = Execution(id="execution-TASK-A", task_id="TASK-A", executor_type="codex", status="failed")
        decision = RepairDecision(
            classification=classification,
            action="ESCALATE_HUMAN" if expected == "HUMAN_REQUIRED" else "RECONCILE",
            failed_execution=execution,
            task=task,
            repair_attempt_count=0,
            max_attempts=1,
        )
        runner = BoundedSchedulerRunner(
            scheduler=FakeScheduler([scheduled("TASK-A")]),  # type: ignore[arg-type]
            execution_runner=FakeExecutionRunner(),  # type: ignore[arg-type]
            finalizer=FakeFinalizer([failed("TASK-A", "execution-TASK-A")]),  # type: ignore[arg-type]
            repair_runner=FakeRepairRunner([RepairExecutionResult(status="REPAIR_NOT_ALLOWED", decision=decision)]),  # type: ignore[arg-type]
        )

        result = runner.run(None, project_id="project-1")  # type: ignore[arg-type]

        assert result.stop_reason == expected


def test_claim_contention_does_not_count_as_failure() -> None:
    scheduler = FakeScheduler(
        [
            SchedulerIterationResult(status="CLAIM_CONTENTION", project_id="project-1", reason="ALREADY_LEASED"),
            SchedulerIterationResult(status="NO_READY_TASK", project_id="project-1"),
        ]
    )
    runner = BoundedSchedulerRunner(scheduler=scheduler, limits=BoundedRunLimits(max_tasks_per_run=2))  # type: ignore[arg-type]

    result = runner.run(None, project_id="project-1")  # type: ignore[arg-type]

    assert result.tasks_failed == 0
    assert result.tasks_attempted == 0
    assert result.stop_reason == "NO_READY_TASK"
    assert len(result.outcomes) == 1
