from __future__ import annotations

import os
import shlex
import subprocess
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from sqlalchemy import select

from ai_ent.bootstrap.codex import CodexConfig, build_execution_package
from ai_ent.bootstrap.git import require_git
from ai_ent.bootstrap.models import BootstrapTask, VerificationSpec
from ai_ent.persistence.config import DatabaseConfigError, load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.persistence.migrations import build_alembic_config
from ai_ent.persistence.models import Checkpoint, Execution, Task, TaskLease
from ai_ent.persistence.repositories import ProjectRepository, TaskRepository
from ai_ent.persistence.repositories.executions import ExecutionRepository
from ai_ent.persistence.repositories.leases import LeaseRepository
from ai_ent.scheduler.execution import ClaimedExecutionRunner
from ai_ent.scheduler.finalization import ExecutionFinalizer
from ai_ent.scheduler.iteration import ExecutionPackageFactory, SchedulerIterationService
from tests.integration.helpers import clean_test_tables, make_test_project_id, make_test_suffix


def integration_database() -> Database:
    try:
        config = build_alembic_config(Path("alembic.ini"), Path(".env"))
        settings = load_database_settings(Path(".env"))
    except DatabaseConfigError as exc:
        raise unittest.SkipTest(f"execution finalizer integration blocked: {exc}") from exc
    command.upgrade(config, "head")
    return Database(settings)


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


def proof_task(task_id: str, *, output_path: str, marker: str) -> BootstrapTask:
    return BootstrapTask(
        id=task_id,
        stage="T",
        title=f"Task {task_id}",
        executor="codex",
        objective=f"Create {output_path} containing exactly {marker}",
        allowed_paths=(output_path,),
        outputs=(output_path,),
        verification=VerificationSpec(commands=(f'test "$(cat {output_path})" = "{marker}"',)),
    )


def remove_execution_worktree(root: Path, task_id: str, execution_id: str, path: Path | None) -> None:
    if path is not None and path.exists():
        require_git(["worktree", "remove", "--force", str(path)], cwd=root)
    branch = f"task/{task_id}/{execution_id}"
    subprocess.run(["git", "branch", "-D", branch], cwd=root, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def test_postgres_real_codex_execution_finalizes_verified_commit(tmp_path: Path) -> None:
    raw_command = os.environ.get("AIENT_CODEX_COMMAND")
    if not raw_command:
        raise unittest.SkipTest("AIENT_CODEX_COMMAND is not configured for real TASK-0025 proof")

    database = integration_database()
    clean_test_tables(database)
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id = make_test_project_id(suffix)
    output_path = "tests/fixtures/finalizer_e2e.txt"
    marker = "AIENT_FINALIZER_E2E=1"
    task = proof_task(f"TEST-FINALIZE-{suffix}", output_path=output_path, marker=marker)
    scheduler = SchedulerIterationService(
        package_factory=ExecutionPackageFactory(manifest_tasks={task.id: task}, timeout_seconds=60),
        owner_id="worker-1",
        lease_duration=timedelta(minutes=10),
    )
    root = Path.cwd()
    main_head = require_git(["rev-parse", "HEAD"], cwd=root)
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
                config=CodexConfig(command=tuple(shlex.split(raw_command)), default_timeout_seconds=60),
                repository_path=root,
                worktree_root=tmp_path / "worktrees",
            ).run_claimed(session, package=scheduled.package, owner_id="worker-1")
            assert executed.status == "EXECUTED"
            assert executed.worktree_path is not None
            worktree_path = executed.worktree_path

            finalized = ExecutionFinalizer(manifest_tasks={task.id: task}).finalize(
                session,
                package=executed.package or scheduled.package,
                owner_id="worker-1",
            )
            assert finalized.status == "COMPLETED"
            assert finalized.candidate is not None
            assert finalized.committed_tree_hash == finalized.candidate.tree_hash
            assert finalized.commit_id is not None

        assert require_git(["rev-parse", "HEAD"], cwd=root) == main_head
        with database.session() as session:
            persisted_task = session.get(Task, task.id)
            executions = session.scalars(select(Execution).where(Execution.task_id == task.id)).all()
            leases = session.scalars(select(TaskLease).where(TaskLease.task_id == task.id)).all()
            checkpoints = session.scalars(select(Checkpoint).where(Checkpoint.task_id == task.id)).all()
            assert persisted_task is not None
            assert persisted_task.status == "passed"
            assert len(executions) == 1
            assert executions[0].status == "succeeded"
            assert executions[0].commit_hash is not None
            assert len(leases) == 1
            assert leases[0].status == "completed"
            assert len(checkpoints) == 1
    finally:
        if execution_id is not None:
            remove_execution_worktree(root, task.id, execution_id, worktree_path)
        clean_test_tables(database)
        database.dispose()


def test_postgres_verification_failure_preserves_uncommitted_worktree(tmp_path: Path) -> None:
    database = integration_database()
    clean_test_tables(database)
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id = make_test_project_id(suffix)
    repo = make_repo(tmp_path / "repo")
    task = proof_task(
        f"TEST-FINALIZE-NEG-{suffix}",
        output_path="tests/fixtures/finalizer_negative.txt",
        marker="AIENT_FINALIZER_NEGATIVE=1",
    )
    wrong_file = repo / "tests/fixtures/finalizer_negative.txt"
    wrong_file.parent.mkdir(parents=True, exist_ok=True)
    wrong_file.write_text("wrong\n", encoding="utf-8")
    before_head = git(repo, "rev-parse", "HEAD")

    try:
        with database.session() as session:
            ProjectRepository().create(session, project_id=project_id, name=f"Project {suffix}")
            TaskRepository().create(
                session,
                task_id=task.id,
                project_id=project_id,
                title=task.title,
                status="running",
            )
            now = datetime.now(UTC)
            ExecutionRepository().create(
                session,
                execution_id="execution-negative",
                task_id=task.id,
                executor_type="codex",
                status="succeeded",
                started_at=now,
                finished_at=now,
                terminal_state="success",
            )
            LeaseRepository().create(
                session,
                lease_id="lease-negative",
                task_id=task.id,
                execution_id="execution-negative",
                owner_id="worker-1",
                acquired_at=now,
                expires_at=now + timedelta(minutes=10),
            )

            result = ExecutionFinalizer(manifest_tasks={task.id: task}).finalize(
                session,
                package=build_execution_package(
                    task,
                    repository_path=repo,
                    worktree_path=repo,
                    execution_id="execution-negative",
                ),
                owner_id="worker-1",
            )

            assert result.status == "VERIFICATION_FAILED"
            assert git(repo, "rev-parse", "HEAD") == before_head
            assert wrong_file.exists()
            assert session.get(Task, task.id).status == "blocked"  # type: ignore[union-attr]
            assert session.get(TaskLease, "lease-negative").status == "released"  # type: ignore[union-attr]
    finally:
        clean_test_tables(database)
        database.dispose()
