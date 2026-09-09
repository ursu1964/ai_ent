from __future__ import annotations

import os
import shlex
import subprocess
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from alembic import command
from sqlalchemy import select

from ai_ent.bootstrap.git import require_git
from ai_ent.bootstrap.models import BootstrapTask
from ai_ent.persistence.config import DatabaseConfigError, load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.persistence.migrations import build_alembic_config
from ai_ent.persistence.models import Execution, Task, TaskLease
from ai_ent.persistence.repositories import ProjectRepository, TaskRepository
from ai_ent.scheduler.execution import ClaimedExecutionRunner
from ai_ent.scheduler.iteration import ExecutionPackageFactory, SchedulerIterationService
from tests.integration.helpers import clean_test_tables, make_test_project_id, make_test_suffix


def integration_database() -> Database:
    try:
        config = build_alembic_config(Path("alembic.ini"), Path(".env"))
        settings = load_database_settings(Path(".env"))
    except DatabaseConfigError as exc:
        raise unittest.SkipTest(f"claimed execution integration blocked: {exc}") from exc
    command.upgrade(config, "head")
    return Database(settings)


def proof_task(task_id: str) -> BootstrapTask:
    return BootstrapTask(
        id=task_id,
        stage="T",
        title=f"Task {task_id}",
        executor="codex",
        objective="Create tests/fixtures/claimed_execution_proof.txt with exactly AIENT_CLAIMED_EXECUTION_PROOF=1",
        allowed_paths=("tests/fixtures/claimed_execution_proof.txt",),
        outputs=("tests/fixtures/claimed_execution_proof.txt",),
    )


def remove_execution_worktree(root: Path, task_id: str, execution_id: str, path: Path | None) -> None:
    if path is not None and path.exists():
        require_git(["worktree", "remove", "--force", str(path)], cwd=root)
    branch = f"task/{task_id}/{execution_id}"
    subprocess.run(["git", "branch", "-D", branch], cwd=root, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def test_postgres_claimed_execution_runs_configured_codex_without_verification_or_commit(tmp_path: Path) -> None:
    raw_command = os.environ.get("AIENT_CODEX_COMMAND")
    if not raw_command:
        raise unittest.SkipTest("AIENT_CODEX_COMMAND is not configured for real Codex execution proof")

    database = integration_database()
    clean_test_tables(database)
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id = make_test_project_id(suffix)
    task = proof_task(f"TEST-CODEX-{suffix}")
    projects = ProjectRepository()
    tasks = TaskRepository()
    scheduler = SchedulerIterationService(
        package_factory=ExecutionPackageFactory(manifest_tasks={task.id: task}, timeout_seconds=60),
        owner_id="worker-1",
        lease_duration=timedelta(minutes=5),
    )
    runner = ClaimedExecutionRunner(
        repository_path=Path.cwd(),
        worktree_root=tmp_path / "worktrees",
    )
    worktree_path: Path | None = None
    execution_id: str | None = None

    try:
        with database.session() as session:
            projects.create(session, project_id=project_id, name=f"Project {suffix}")
            tasks.create(session, task_id=task.id, project_id=project_id, title=task.title)

        with database.session() as session:
            scheduled = scheduler.run_once(session, project_id=project_id)
            assert scheduled.status == "PACKAGE_READY"
            assert scheduled.package is not None
            execution_id = scheduled.package.execution_id
            result = ClaimedExecutionRunner(
                config=runner.config.__class__(
                    command=tuple(shlex.split(raw_command)),
                    default_timeout_seconds=60,
                ),
                repository_path=Path.cwd(),
                worktree_root=tmp_path / "worktrees",
            ).run_claimed(session, package=scheduled.package, owner_id="worker-1")
            worktree_path = result.worktree_path
            assert result.status == "EXECUTED", result.execution_result
            assert result.worktree_path is not None
            assert result.changed_files == ("tests/fixtures/claimed_execution_proof.txt",)
            proof_file = result.worktree_path / "tests/fixtures/claimed_execution_proof.txt"
            assert proof_file.read_text(encoding="utf-8").strip() == "AIENT_CLAIMED_EXECUTION_PROOF=1"

        with database.session() as session:
            persisted_task = session.get(Task, task.id)
            executions = session.scalars(select(Execution).where(Execution.task_id == task.id)).all()
            leases = session.scalars(select(TaskLease).where(TaskLease.task_id == task.id)).all()
            assert persisted_task is not None
            assert persisted_task.status == "running"
            assert len(executions) == 1
            assert executions[0].commit_hash is None
            assert len(leases) == 1
            assert leases[0].status == "active"
    finally:
        if execution_id is not None:
            remove_execution_worktree(Path.cwd(), task.id, execution_id, worktree_path)
        clean_test_tables(database)
        database.dispose()
