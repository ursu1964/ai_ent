from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ai_ent.bootstrap.codex import CodexConfig
from ai_ent.persistence.models import Task
from ai_ent.scheduler.bounded import BoundedRunResult
from ai_ent.scheduler.guarded import (
    GuardedAutonomousRunner,
    GuardedPreflightResult,
    GuardedRunConfig,
    PostgresAuthorityResult,
)
from ai_ent.scheduler.recovery import RecoveryResult


def ok_preflight() -> GuardedPreflightResult:
    return GuardedPreflightResult(True, "ok")


def failing_preflight() -> GuardedPreflightResult:
    return GuardedPreflightResult(False, "docker unavailable")


def ok_authority(_: Any, project_id: str) -> PostgresAuthorityResult:
    return PostgresAuthorityResult(True, f"authority {project_id}")


def missing_authority(_: Any, project_id: str) -> PostgresAuthorityResult:
    return PostgresAuthorityResult(False, f"missing {project_id}")


class FakeRecovery:
    def __init__(self, result: RecoveryResult) -> None:
        self.result = result
        self.calls = 0

    def recover_project(self, _: Any, *, project_id: str) -> RecoveryResult:
        self.calls += 1
        return self.result


class FakeReadiness:
    def __init__(self, task_ids: tuple[str, ...] = ()) -> None:
        self.task_ids = task_ids
        self.calls = 0

    def list_ready_tasks(self, _: Any, *, project_id: str, limit: int | None = None) -> list[Task]:
        self.calls += 1
        selected = self.task_ids if limit is None else self.task_ids[:limit]
        return [Task(id=task_id, project_id=project_id, title=task_id) for task_id in selected]


class FakeBoundedRunner:
    def __init__(self, result: BoundedRunResult) -> None:
        self.result = result
        self.calls = 0

    def run(self, _: Any, *, project_id: str) -> BoundedRunResult:
        self.calls += 1
        return self.result


def clean_recovery() -> RecoveryResult:
    return RecoveryResult(
        interrupted_task_id=None,
        execution_id=None,
        detected_stage="BETWEEN_TASKS",
        evidence=("clean",),
        action="READY_TO_CONTINUE",
        safe_to_continue=True,
    )


def blocked_recovery() -> RecoveryResult:
    return RecoveryResult(
        interrupted_task_id="TASK-A",
        execution_id="execution-a",
        detected_stage="DURING_EXECUTOR",
        evidence=("uncertain",),
        action="HUMAN_REQUIRED",
        remaining_blocker="uncertain_executor_process_state",
    )


def bounded_result(stop_reason: str, *, completed: int = 0, failed: int = 0) -> BoundedRunResult:
    now = datetime.now(UTC)
    return BoundedRunResult(
        run_id="bounded-1",
        project_id="project-1",
        started_at=now,
        finished_at=now,
        tasks_attempted=completed + failed,
        tasks_completed=completed,
        tasks_failed=failed,
        repairs_attempted=0,
        commits_created=("abc123",) if completed else (),
        stop_reason=stop_reason,  # type: ignore[arg-type]
    )


def make_runner(
    *,
    bounded: FakeBoundedRunner | None = None,
    recovery: FakeRecovery | None = None,
    readiness: FakeReadiness | None = None,
    codex_config: CodexConfig | None = None,
    preflight=ok_preflight,
    authority=ok_authority,
) -> GuardedAutonomousRunner:
    return GuardedAutonomousRunner(
        bounded_runner=bounded,  # type: ignore[arg-type]
        recovery=recovery or FakeRecovery(clean_recovery()),  # type: ignore[arg-type]
        readiness=readiness or FakeReadiness(),  # type: ignore[arg-type]
        codex_config=codex_config or CodexConfig(command=("codex",), default_timeout_seconds=30),
        preflight=preflight,
        authority_check=authority,
        run_id_factory=lambda: "guarded-1",
    )


def test_dry_run_performs_no_claims_or_codex() -> None:
    bounded = FakeBoundedRunner(bounded_result("NO_READY_TASK"))
    readiness = FakeReadiness(("TASK-A",))
    runner = make_runner(bounded=bounded, readiness=readiness)

    result = runner.run(None, GuardedRunConfig(project_id="project-1", dry_run=True))  # type: ignore[arg-type]

    assert result.dry_run
    assert result.stop_reason == "COMPLETED_BOUND"
    assert result.likely_next_task_id == "TASK-A"
    assert result.remaining_ready_tasks == ("TASK-A",)
    assert bounded.calls == 0


def test_postgresql_authority_required_after_cutover() -> None:
    runner = make_runner(authority=missing_authority)

    result = runner.run(None, GuardedRunConfig(project_id="project-1"))  # type: ignore[arg-type]

    assert result.stop_reason == "RUNTIME_ERROR"
    assert result.blockers == ("postgresql_authority_required:missing project-1",)


def test_preflight_failure_stops_before_recovery_or_scheduling() -> None:
    recovery = FakeRecovery(clean_recovery())
    bounded = FakeBoundedRunner(bounded_result("NO_READY_TASK"))
    runner = make_runner(preflight=failing_preflight, recovery=recovery, bounded=bounded)

    result = runner.run(None, GuardedRunConfig(project_id="project-1"))  # type: ignore[arg-type]

    assert result.stop_reason == "RUNTIME_ERROR"
    assert result.blockers == ("preflight_failed:docker unavailable",)
    assert recovery.calls == 0
    assert bounded.calls == 0


def test_codex_not_configured_stops_safely_without_scheduling() -> None:
    bounded = FakeBoundedRunner(bounded_result("NO_READY_TASK"))
    runner = make_runner(
        bounded=bounded,
        readiness=FakeReadiness(("TASK-A",)),
        codex_config=CodexConfig(command=(), default_timeout_seconds=30),
    )

    result = runner.run(None, GuardedRunConfig(project_id="project-1"))  # type: ignore[arg-type]

    assert result.stop_reason == "BLOCKED"
    assert result.human_action_required
    assert result.blockers == ("codex_not_configured:set AIENT_CODEX_COMMAND",)
    assert bounded.calls == 0


def test_recovery_runs_before_scheduling_and_unresolved_work_blocks() -> None:
    recovery = FakeRecovery(blocked_recovery())
    bounded = FakeBoundedRunner(bounded_result("NO_READY_TASK"))
    runner = make_runner(recovery=recovery, bounded=bounded)

    result = runner.run(None, GuardedRunConfig(project_id="project-1"))  # type: ignore[arg-type]

    assert recovery.calls == 1
    assert bounded.calls == 0
    assert result.stop_reason == "HUMAN_REQUIRED"
    assert result.human_action_required
    assert result.blockers == ("uncertain_executor_process_state",)


def test_bounded_run_summary_and_stop_reason_are_reported() -> None:
    bounded = FakeBoundedRunner(bounded_result("MAX_TASKS_PER_RUN", completed=1))
    runner = make_runner(bounded=bounded)

    result = runner.run(None, GuardedRunConfig(project_id="project-1"))  # type: ignore[arg-type]

    assert result.stop_reason == "TASK_LIMIT"
    assert result.tasks_attempted == 1
    assert result.tasks_completed == 1
    assert result.commits_created == ("abc123",)
    assert bounded.calls == 1


def test_human_and_reconciliation_stop_reasons_are_preserved() -> None:
    for bounded_stop, guarded_stop in (
        ("HUMAN_REQUIRED", "HUMAN_REQUIRED"),
        ("RECONCILIATION_REQUIRED", "RECONCILIATION_REQUIRED"),
        ("MAX_FAILURES_PER_RUN", "FAILURE_LIMIT"),
        ("MAX_WALL_CLOCK_SECONDS", "TIME_LIMIT"),
    ):
        runner = make_runner(bounded=FakeBoundedRunner(bounded_result(bounded_stop, failed=1)))

        result = runner.run(None, GuardedRunConfig(project_id="project-1"))  # type: ignore[arg-type]

        assert result.stop_reason == guarded_stop
