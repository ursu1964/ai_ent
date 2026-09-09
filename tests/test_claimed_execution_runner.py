from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.bootstrap.codex import CodexConfig
from ai_ent.bootstrap.models import BootstrapTask, ExecutionPackage
from ai_ent.bootstrap.worktree import execution_worktree_path
from ai_ent.persistence.models import Base, Execution, Task, TaskLease
from ai_ent.persistence.repositories import ProjectRepository, TaskRepository
from ai_ent.scheduler.execution import ClaimedExecutionRunner
from ai_ent.scheduler.iteration import (
    ExecutionPackageFactory,
    SchedulerIterationResult,
    SchedulerIterationService,
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
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.local")
    git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "seed")
    return repo


def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def manifest_task(task_id: str = "TASK-A") -> BootstrapTask:
    return BootstrapTask(
        id=task_id,
        stage="T",
        title=f"Task {task_id}",
        executor="codex",
        objective=f"Create proof for {task_id}",
        allowed_paths=("tests/fixtures/runner_proof.txt",),
        outputs=("tests/fixtures/runner_proof.txt",),
    )


def seed_claim(session: Session, task: BootstrapTask, repo: Path):
    ProjectRepository().create(session, project_id="project-1", name="Project 1")
    TaskRepository().create(session, task_id=task.id, project_id="project-1", title=task.title)
    scheduler = SchedulerIterationService(
        package_factory=ExecutionPackageFactory(
            repository_path=repo,
            manifest_tasks={task.id: task},
            timeout_seconds=5,
        ),
        owner_id="worker-1",
        lease_duration=timedelta(minutes=5),
    )
    result = scheduler.run_once(session, project_id="project-1")
    assert result.status == "PACKAGE_READY"
    assert result.package is not None
    assert result.execution is not None
    assert result.lease is not None
    return result


def require_package(result: SchedulerIterationResult) -> ExecutionPackage:
    assert result.package is not None
    return result.package


def require_execution(result: SchedulerIterationResult) -> Execution:
    assert result.execution is not None
    return result.execution


def require_lease(result: SchedulerIterationResult) -> TaskLease:
    assert result.lease is not None
    return result.lease


def test_valid_claimed_execution_creates_isolated_worktree_and_persists_success(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = manifest_task()
    factory = session_factory()
    command = (
        sys.executable,
        "-c",
        (
            "from pathlib import Path; Path('tests/fixtures').mkdir(parents=True, exist_ok=True); "
            "Path('tests/fixtures/runner_proof.txt').write_text('proof\\n', encoding='utf-8')"
        ),
    )
    with factory() as session:
        scheduled = seed_claim(session, task, repo)
        head_before = git(repo, "rev-parse", "HEAD")
        runner = ClaimedExecutionRunner(
            config=CodexConfig(command=command, default_timeout_seconds=5),
            repository_path=repo,
            worktree_root=tmp_path / "worktrees",
        )

        package = require_package(scheduled)
        execution = require_execution(scheduled)
        result = runner.run_claimed(session, package=package, owner_id="worker-1")

        assert result.status == "EXECUTED"
        assert result.execution_result is not None
        assert result.execution_result.ok
        assert result.worktree_path is not None
        assert result.worktree_path != repo
        assert result.changed_files == ("tests/fixtures/runner_proof.txt",)
        assert (result.worktree_path / "tests/fixtures/runner_proof.txt").read_text(encoding="utf-8") == "proof\n"
        assert not (repo / "tests/fixtures/runner_proof.txt").exists()
        assert git(repo, "rev-parse", "HEAD") == head_before
        persisted = session.get(Execution, execution.id)
        assert persisted is not None
        assert persisted.status == "succeeded"
        assert persisted.finished_at is not None
        assert persisted.commit_hash is None
        assert session.get(Task, task.id).status == "running"  # type: ignore[union-attr]


def test_worktree_path_is_execution_scoped(tmp_path: Path) -> None:
    path = execution_worktree_path("TASK-A", "execution-1")
    assert path.name == "execution-1"
    assert path.parent.name == "TASK-A"


def test_valid_lease_and_owner_are_required(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = manifest_task()
    factory = session_factory()
    runner = ClaimedExecutionRunner(
        config=CodexConfig(command=(sys.executable, "-c", "raise SystemExit(0)")),
        repository_path=repo,
        worktree_root=tmp_path / "worktrees",
    )
    with factory() as session:
        scheduled = seed_claim(session, task, repo)

        package = require_package(scheduled)
        lease = require_lease(scheduled)
        wrong_owner = runner.run_claimed(session, package=package, owner_id="wrong")
        assert wrong_owner.status == "OWNERSHIP_LOST"

        lease.acquired_at = datetime(2000, 1, 1, tzinfo=UTC)
        lease.renewed_at = lease.acquired_at
        lease.expires_at = datetime(2000, 1, 2, tzinfo=UTC)
        session.flush()
        expired = runner.run_claimed(session, package=package, owner_id="worker-1")
        assert expired.status == "OWNERSHIP_LOST"


def test_not_configured_is_persisted_without_worktree(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = manifest_task()
    factory = session_factory()
    with factory() as session:
        scheduled = seed_claim(session, task, repo)
        runner = ClaimedExecutionRunner(
            config=CodexConfig(command=()),
            repository_path=repo,
            worktree_root=tmp_path / "worktrees",
        )

        package = require_package(scheduled)
        execution = require_execution(scheduled)
        result = runner.run_claimed(session, package=package, owner_id="worker-1")

        assert result.status == "NOT_CONFIGURED"
        assert result.worktree_path is None
        persisted = session.get(Execution, execution.id)
        assert persisted is not None
        assert persisted.status == "failed"
        assert persisted.terminal_state == "not_configured"


def test_non_zero_exit_and_timeout_are_persisted(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    factory = session_factory()
    with factory() as session:
        task = manifest_task("TASK-FAIL")
        scheduled = seed_claim(session, task, repo)
        failing = ClaimedExecutionRunner(
            config=CodexConfig(command=(sys.executable, "-c", "raise SystemExit(7)"), default_timeout_seconds=5),
            repository_path=repo,
            worktree_root=tmp_path / "worktrees-fail",
        )
        package = require_package(scheduled)
        execution = require_execution(scheduled)
        failed = failing.run_claimed(session, package=package, owner_id="worker-1")
        assert failed.status == "EXECUTOR_FAILED"
        assert session.get(Execution, execution.id).status == "failed"  # type: ignore[union-attr]

    with factory() as session:
        task = manifest_task("TASK-TIMEOUT")
        ProjectRepository().create(session, project_id="project-timeout", name="Project timeout")
        TaskRepository().create(session, task_id=task.id, project_id="project-timeout", title=task.title)
        scheduled = SchedulerIterationService(
            package_factory=ExecutionPackageFactory(repository_path=repo, manifest_tasks={task.id: task}, timeout_seconds=1),
            owner_id="worker-1",
            lease_duration=timedelta(minutes=5),
        ).run_once(session, project_id="project-timeout")
        assert scheduled.package is not None
        timeout_runner = ClaimedExecutionRunner(
            config=CodexConfig(command=(sys.executable, "-c", "import time; time.sleep(5)"), default_timeout_seconds=1),
            repository_path=repo,
            worktree_root=tmp_path / "worktrees-timeout",
        )
        package = require_package(scheduled)
        execution = require_execution(scheduled)
        timed_out = timeout_runner.run_claimed(session, package=package, owner_id="worker-1")
        assert timed_out.status == "EXECUTOR_FAILED"
        assert session.get(Execution, execution.id).status == "timeout"  # type: ignore[union-attr]


def test_worktree_creation_failure_leaves_recoverable_execution(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = manifest_task()
    factory = session_factory()
    worktree_root = tmp_path / "worktrees"
    with factory() as session:
        scheduled = seed_claim(session, task, repo)
        package = require_package(scheduled)
        execution = require_execution(scheduled)
        conflict = worktree_root / task.id / execution.id
        conflict.mkdir(parents=True)
        runner = ClaimedExecutionRunner(
            config=CodexConfig(command=(sys.executable, "-c", "raise SystemExit(0)")),
            repository_path=repo,
            worktree_root=worktree_root,
        )

        result = runner.run_claimed(session, package=package, owner_id="worker-1")

        assert result.status == "WORKTREE_ERROR"
        assert session.get(Execution, execution.id).status == "failed"  # type: ignore[union-attr]


def test_runner_processes_only_supplied_claimed_execution(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = manifest_task()
    factory = session_factory()
    with factory() as session:
        scheduled = seed_claim(session, task, repo)
        runner = ClaimedExecutionRunner(
            config=CodexConfig(command=(sys.executable, "-c", "raise SystemExit(0)")),
            repository_path=repo,
            worktree_root=tmp_path / "worktrees",
        )
        package = require_package(scheduled)
        result = runner.run_claimed(session, package=package, owner_id="worker-1")

        assert result.execution is scheduled.execution
        assert len(session.scalars(select(Execution)).all()) == 1
        assert len(session.scalars(select(TaskLease)).all()) == 1
