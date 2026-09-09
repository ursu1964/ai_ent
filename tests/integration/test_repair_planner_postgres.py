from __future__ import annotations

import subprocess
import sys
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from alembic import command

from ai_ent.bootstrap.codex import CodexConfig
from ai_ent.bootstrap.git import require_git
from ai_ent.bootstrap.models import BootstrapTask, VerificationSpec
from ai_ent.persistence.config import DatabaseConfigError, load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.persistence.migrations import build_alembic_config
from ai_ent.persistence.repositories import ProjectRepository, TaskRepository
from ai_ent.persistence.repositories.executions import ExecutionRepository
from ai_ent.scheduler.execution import ClaimedExecutionRunner
from ai_ent.scheduler.finalization import ExecutionFinalizer
from ai_ent.scheduler.iteration import ExecutionPackageFactory, SchedulerIterationService
from ai_ent.scheduler.repair import RepairPlanner, RepairPolicy
from tests.integration.helpers import clean_test_tables, make_test_project_id, make_test_suffix


def integration_database() -> Database:
    try:
        config = build_alembic_config(Path("alembic.ini"), Path(".env"))
        settings = load_database_settings(Path(".env"))
    except DatabaseConfigError as exc:
        raise unittest.SkipTest(f"repair planner integration blocked: {exc}") from exc
    command.upgrade(config, "head")
    return Database(settings)


def proof_task(task_id: str) -> BootstrapTask:
    return BootstrapTask(
        id=task_id,
        stage="T",
        title=f"Task {task_id}",
        executor="codex",
        objective="Create tests/fixtures/repair_plan.txt containing the requested marker.",
        allowed_paths=("tests/fixtures/repair_plan.txt",),
        outputs=("tests/fixtures/repair_plan.txt",),
        verification=VerificationSpec(
            commands=('test "$(cat tests/fixtures/repair_plan.txt)" = "AIENT_REPAIR_PLAN=correct"',)
        ),
    )


def remove_execution_worktree(root: Path, task_id: str, execution_id: str | None, path: Path | None) -> None:
    if path is not None and path.exists():
        require_git(["worktree", "remove", "--force", str(path)], cwd=root)
    if execution_id is not None:
        branch = f"task/{task_id}/{execution_id}"
        subprocess.run(["git", "branch", "-D", branch], cwd=root, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def test_postgres_repair_plan_created_after_verification_failure(tmp_path: Path) -> None:
    database = integration_database()
    clean_test_tables(database)
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id = make_test_project_id(suffix)
    task = proof_task(f"TEST-REPAIR-{suffix}")
    scheduler = SchedulerIterationService(
        package_factory=ExecutionPackageFactory(manifest_tasks={task.id: task}, timeout_seconds=30),
        owner_id="worker-1",
        lease_duration=timedelta(minutes=5),
    )
    wrong_writer = (
        sys.executable,
        "-c",
        (
            "from pathlib import Path; "
            "p=Path('tests/fixtures/repair_plan.txt'); "
            "p.parent.mkdir(parents=True, exist_ok=True); "
            "p.write_text('AIENT_REPAIR_PLAN=wrong\\n', encoding='utf-8')"
        ),
    )
    worktree_path: Path | None = None
    execution_id: str | None = None

    try:
        with database.session() as session:
            ProjectRepository().create(session, project_id=project_id, name=f"Project {suffix}")
            TaskRepository().create(session, task_id=task.id, project_id=project_id, title=task.title)

        with database.session() as session:
            scheduled = scheduler.run_once(session, project_id=project_id)
            assert scheduled.status == "PACKAGE_READY"
            assert scheduled.package is not None
            execution_id = scheduled.package.execution_id
            executed = ClaimedExecutionRunner(
                config=CodexConfig(command=wrong_writer, default_timeout_seconds=30),
                repository_path=Path.cwd(),
                worktree_root=tmp_path / "worktrees",
            ).run_claimed(session, package=scheduled.package, owner_id="worker-1")
            assert executed.status == "EXECUTED"
            assert executed.package is not None
            worktree_path = executed.worktree_path

            finalized = ExecutionFinalizer(manifest_tasks={task.id: task}).finalize(
                session,
                package=executed.package,
                owner_id="worker-1",
            )
            assert finalized.status == "VERIFICATION_FAILED"
            assert finalized.execution is not None

            decision = RepairPlanner(manifest_tasks={task.id: task}).plan(
                session,
                execution_id=finalized.execution.id,
            )

            assert decision.classification.category == "VERIFICATION_FAILURE"
            assert decision.action == "REPAIR_WITH_NEW_EXECUTION"
            assert decision.next_execution is not None
            assert decision.next_execution.attempt == 2
            assert decision.next_package is not None
            assert "Changed file: tests/fixtures/repair_plan.txt" in decision.next_package.instructions
            assert decision.next_package.allowed_paths == task.allowed_paths
    finally:
        remove_execution_worktree(Path.cwd(), task.id, execution_id, worktree_path)
        clean_test_tables(database)
        database.dispose()


def test_postgres_repair_limit_blocks_additional_execution() -> None:
    database = integration_database()
    clean_test_tables(database)
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id = make_test_project_id(suffix)
    task = proof_task(f"TEST-REPAIR-LIMIT-{suffix}")
    executions = ExecutionRepository()

    try:
        with database.session() as session:
            ProjectRepository().create(session, project_id=project_id, name=f"Project {suffix}")
            TaskRepository().create(
                session,
                task_id=task.id,
                project_id=project_id,
                title=task.title,
                status="blocked",
            )
            for attempt in (1, 2, 3):
                executions.create(
                    session,
                    execution_id=f"execution-{suffix}-{attempt}",
                    task_id=task.id,
                    executor_type="codex",
                    status="failed",
                    attempt=attempt,
                    terminal_state="failure",
                    error_classification="verification_failed",
                )

            decision = RepairPlanner(
                manifest_tasks={task.id: task},
                policy=RepairPolicy(max_autonomous_repair_attempts=2),
            ).plan(session, execution_id=f"execution-{suffix}-1")

            assert decision.action == "ESCALATE_HUMAN"
            assert decision.repair_attempt_count == 2
            assert decision.next_execution is None
    finally:
        clean_test_tables(database)
        database.dispose()
