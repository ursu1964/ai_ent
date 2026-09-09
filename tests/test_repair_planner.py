from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.bootstrap.codex import CodexConfig
from ai_ent.bootstrap.models import BootstrapTask, VerificationSpec
from ai_ent.persistence.models import Base, Execution
from ai_ent.persistence.repositories import CheckpointRepository, ProjectRepository, TaskRepository
from ai_ent.persistence.repositories.executions import ExecutionRepository
from ai_ent.persistence.repositories.leases import LeaseRepository
from ai_ent.scheduler.execution import ClaimedExecutionRunner
from ai_ent.scheduler.finalization import ExecutionFinalizer
from ai_ent.scheduler.repair import (
    FailureClassifier,
    RepairExecutionService,
    RepairPlanner,
    RepairPolicy,
)


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert completed.returncode == 0, completed.stdout
    return completed.stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.local")
    git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-m", "seed")
    return tmp_path


def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def manifest_task(task_id: str = "TASK-REPAIR") -> BootstrapTask:
    return BootstrapTask(
        id=task_id,
        stage="T",
        title=f"Task {task_id}",
        executor="codex",
        objective="Create the requested fixture.",
        allowed_paths=("tests/fixtures/repair.txt",),
        outputs=("tests/fixtures/repair.txt",),
        verification=VerificationSpec(commands=('test "$(cat tests/fixtures/repair.txt)" = "fixed"',)),
    )


def seed_task(session: Session, task: BootstrapTask, *, status: str = "blocked") -> None:
    ProjectRepository().create(session, project_id="project-1", name="Project 1")
    TaskRepository().create(
        session,
        task_id=task.id,
        project_id="project-1",
        title=task.title,
        status=status,  # type: ignore[arg-type]
    )


def seed_failed_execution(
    session: Session,
    task: BootstrapTask,
    *,
    execution_id: str = "execution-1",
    attempt: int = 1,
    status: str = "failed",
    terminal_state: str = "failure",
    error_classification: str = "verification_failed",
    checkpoint_status: str = "verification_failed",
    findings: tuple[str, ...] = ("missing expected output",),
) -> Execution:
    now = datetime.now(UTC)
    execution = ExecutionRepository().create(
        session,
        execution_id=execution_id,
        task_id=task.id,
        executor_type="codex",
        status=status,  # type: ignore[arg-type]
        attempt=attempt,
        started_at=now,
        finished_at=now,
        terminal_state=terminal_state,
        error_classification=error_classification,
    )
    CheckpointRepository().create(
        session,
        checkpoint_id=f"checkpoint-{execution_id}",
        task_id=task.id,
        execution_id=execution.id,
        checkpoint_type="execution",
        state=json.dumps({"status": checkpoint_status, "findings": list(findings)}, sort_keys=True),
    )
    return execution


def test_verifier_failure_classified_repairable_and_creates_new_execution() -> None:
    task = manifest_task()
    factory = session_factory()
    with factory() as session:
        seed_task(session, task)
        failed = seed_failed_execution(session, task)

        decision = RepairPlanner(manifest_tasks={task.id: task}).plan(session, execution_id=failed.id)

        assert decision.classification.category == "VERIFICATION_FAILURE"
        assert decision.classification.stage == "VERIFICATION"
        assert decision.classification.retryability == "REPAIRABLE"
        assert decision.action == "REPAIR_WITH_NEW_EXECUTION"
        assert decision.next_execution is not None
        assert decision.next_execution.attempt == 2
        assert decision.next_package is not None
        assert "Previous execution: execution-1" in decision.next_package.instructions
        assert "Finding: missing expected output" in decision.next_package.instructions
        assert decision.next_package.allowed_paths == task.allowed_paths
        assert session.get(Execution, failed.id).status == "failed"  # type: ignore[union-attr]


def test_scope_violation_is_policy_security_and_not_auto_repaired() -> None:
    task = manifest_task()
    factory = session_factory()
    with factory() as session:
        seed_task(session, task)
        failed = seed_failed_execution(
            session,
            task,
            error_classification="scope_failed",
            checkpoint_status="scope_failed",
        )

        decision = RepairPlanner(manifest_tasks={task.id: task}).plan(session, execution_id=failed.id)

        assert decision.classification.category == "POLICY_SECURITY_FAILURE"
        assert decision.classification.stage == "SCOPE_VALIDATION"
        assert decision.action == "ESCALATE_HUMAN"
        assert not decision.next_execution_allowed


def test_executor_timeout_and_not_configured_are_distinct() -> None:
    classifier = FailureClassifier()
    timeout = Execution(
        id="timeout",
        task_id="task",
        executor_type="codex",
        status="timeout",
        attempt=1,
        terminal_state="timeout",
        error_classification="executor_timeout",
    )
    not_configured = Execution(
        id="not-configured",
        task_id="task",
        executor_type="codex",
        status="failed",
        attempt=1,
        terminal_state="not_configured",
        error_classification="codex_not_configured",
    )

    assert classifier.classify(timeout).category == "TIMEOUT"
    assert classifier.classify(timeout).retryability == "TRANSIENT"
    assert classifier.classify(not_configured).category == "CONFIGURATION_FAILURE"
    assert classifier.classify(not_configured).retryability == "HUMAN_REQUIRED"


def test_environment_and_db_pending_classification() -> None:
    classifier = FailureClassifier()
    environment = Execution(
        id="env",
        task_id="task",
        executor_type="codex",
        status="failed",
        attempt=1,
        terminal_state="failure",
        error_classification="postgres_unavailable",
    )
    db_pending = Execution(
        id="db",
        task_id="task",
        executor_type="codex",
        status="failed",
        attempt=1,
        terminal_state="failure",
        error_classification="db_pending",
    )

    assert classifier.classify(environment).category == "ENVIRONMENT_FAILURE"
    assert classifier.classify(environment).retryability == "TRANSIENT"
    assert classifier.classify(db_pending).category == "DB_FINALIZATION_FAILURE"
    assert classifier.classify(db_pending).retryability == "RECONCILIATION_REQUIRED"


def test_repair_limit_escalates_without_new_execution() -> None:
    task = manifest_task()
    factory = session_factory()
    with factory() as session:
        seed_task(session, task)
        failed = seed_failed_execution(session, task, execution_id="execution-1", attempt=1)
        seed_failed_execution(session, task, execution_id="execution-2", attempt=2)
        seed_failed_execution(session, task, execution_id="execution-3", attempt=3)

        decision = RepairPlanner(
            manifest_tasks={task.id: task},
            policy=RepairPolicy(max_autonomous_repair_attempts=2),
        ).plan(session, execution_id=failed.id)

        assert decision.repair_attempt_count == 2
        assert decision.action == "ESCALATE_HUMAN"
        assert not decision.next_execution_allowed


def test_old_active_lease_is_released_before_repair_requires_fresh_claim() -> None:
    task = manifest_task()
    factory = session_factory()
    with factory() as session:
        seed_task(session, task)
        failed = seed_failed_execution(session, task)
        now = datetime.now(UTC)
        LeaseRepository().create(
            session,
            lease_id="old-lease",
            task_id=task.id,
            execution_id=failed.id,
            owner_id="old-worker",
            acquired_at=now,
            expires_at=now + timedelta(minutes=10),
        )

        decision = RepairPlanner(manifest_tasks={task.id: task}).plan(session, execution_id=failed.id)

        assert decision.action == "REPAIR_WITH_NEW_EXECUTION"
        assert session.get(Execution, "execution-1").status == "failed"  # type: ignore[union-attr]
        assert LeaseRepository().get(session, "old-lease").status == "released"  # type: ignore[union-attr]
        assert decision.next_execution is not None
        assert decision.next_execution.id != failed.id


def test_classifier_and_planner_do_not_invoke_codex_verifier_or_commit() -> None:
    task = manifest_task()
    factory = session_factory()
    with factory() as session:
        seed_task(session, task)
        failed = seed_failed_execution(session, task)
        with (
            patch("ai_ent.bootstrap.codex.CodexExecutor.execute_package", side_effect=AssertionError("codex invoked")),
            patch("ai_ent.bootstrap.verifier.verify_task", side_effect=AssertionError("verifier invoked")),
            patch("ai_ent.bootstrap.git.commit_verified_candidate", side_effect=AssertionError("commit invoked")),
        ):
            decision = RepairPlanner(manifest_tasks={task.id: task}).plan(session, execution_id=failed.id)

        assert decision.action == "REPAIR_WITH_NEW_EXECUTION"


def test_repair_execution_service_runs_one_successful_repair(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    task = manifest_task()
    factory = session_factory()
    repair_writer = (
        sys.executable,
        "-c",
        (
            "from pathlib import Path; p=Path('tests/fixtures/repair.txt'); "
            "p.parent.mkdir(parents=True, exist_ok=True); p.write_text('fixed\\n', encoding='utf-8')"
        ),
    )
    with factory() as session:
        seed_task(session, task)
        failed = seed_failed_execution(session, task)
        before_head = git(repo, "rev-parse", "HEAD")

        result = RepairExecutionService(
            planner=RepairPlanner(manifest_tasks={task.id: task}),
            runner=ClaimedExecutionRunner(
                config=CodexConfig(command=repair_writer, default_timeout_seconds=30),
                repository_path=repo,
                worktree_root=tmp_path / "worktrees",
            ),
            finalizer=ExecutionFinalizer(manifest_tasks={task.id: task}),
            repository_path=repo,
            worktree_root=tmp_path / "worktrees",
        ).run_once(session, failed_execution_id=failed.id)

        assert result.status == "REPAIRED"
        assert result.execution_result is not None
        assert result.execution_result.status == "EXECUTED"
        assert result.finalization is not None
        assert result.finalization.status == "COMPLETED"
        assert result.decision.next_execution is not None
        assert result.decision.next_execution.attempt == 2
        assert session.get(Execution, failed.id).status == "failed"  # type: ignore[union-attr]
        repair_execution = session.get(Execution, result.decision.next_execution.id)
        assert repair_execution is not None
        assert repair_execution.status == "succeeded"
        assert git(repo, "rev-parse", "HEAD") == before_head
        assert git(result.execution_result.worktree_path, "rev-parse", "HEAD") != before_head  # type: ignore[arg-type]


def test_repair_execution_service_failed_repair_stops_without_third_execution(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    task = manifest_task()
    factory = session_factory()
    wrong_writer = (
        sys.executable,
        "-c",
        (
            "from pathlib import Path; p=Path('tests/fixtures/repair.txt'); "
            "p.parent.mkdir(parents=True, exist_ok=True); p.write_text('', encoding='utf-8')"
        ),
    )
    with factory() as session:
        seed_task(session, task)
        failed = seed_failed_execution(session, task)

        result = RepairExecutionService(
            planner=RepairPlanner(manifest_tasks={task.id: task}),
            runner=ClaimedExecutionRunner(
                config=CodexConfig(command=wrong_writer, default_timeout_seconds=30),
                repository_path=repo,
                worktree_root=tmp_path / "worktrees",
            ),
            finalizer=ExecutionFinalizer(manifest_tasks={task.id: task}),
            repository_path=repo,
            worktree_root=tmp_path / "worktrees",
        ).run_once(session, failed_execution_id=failed.id)

        executions = ExecutionRepository().list_by_task(session, task.id)
        assert result.status == "REPAIR_FAILED"
        assert result.followup_classification is not None
        assert result.followup_classification.category == "VERIFICATION_FAILURE"
        assert len(executions) == 2
        assert executions[0].status == "failed"
        assert executions[1].status == "failed"


def test_repair_execution_service_budget_exhausted_does_not_call_codex(tmp_path: Path) -> None:
    task = manifest_task()
    factory = session_factory()
    with factory() as session:
        seed_task(session, task)
        failed = seed_failed_execution(session, task, execution_id="execution-1", attempt=1)
        seed_failed_execution(session, task, execution_id="execution-2", attempt=2)
        seed_failed_execution(session, task, execution_id="execution-3", attempt=3)
        with patch("ai_ent.bootstrap.codex.CodexExecutor.execute_package", side_effect=AssertionError("codex invoked")):
            result = RepairExecutionService(
                planner=RepairPlanner(
                    manifest_tasks={task.id: task},
                    policy=RepairPolicy(max_autonomous_repair_attempts=2),
                ),
                repository_path=tmp_path,
            ).run_once(session, failed_execution_id=failed.id)

        assert result.status == "REPAIR_NOT_ALLOWED"
        assert result.decision.action == "ESCALATE_HUMAN"


def test_repair_execution_service_rerun_does_not_duplicate_existing_repair(tmp_path: Path) -> None:
    task = manifest_task()
    factory = session_factory()
    with factory() as session:
        seed_task(session, task)
        failed = seed_failed_execution(session, task)
        ExecutionRepository().create(
            session,
            execution_id="existing-repair",
            task_id=task.id,
            executor_type="codex",
            status="running",
            attempt=2,
        )

        result = RepairExecutionService(
            planner=RepairPlanner(manifest_tasks={task.id: task}),
            repository_path=tmp_path,
        ).run_once(session, failed_execution_id=failed.id)

        assert result.status == "REPAIR_ALREADY_EXISTS"
        assert len(ExecutionRepository().list_by_task(session, task.id)) == 2
