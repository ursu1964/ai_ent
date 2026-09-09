from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.bootstrap.codex import build_execution_package
from ai_ent.bootstrap.models import BootstrapTask, ExecutionPackage, VerificationSpec
from ai_ent.persistence.models import Base, Checkpoint, Execution, Task, TaskLease
from ai_ent.persistence.repositories import ProjectRepository, TaskRepository
from ai_ent.persistence.repositories.executions import ExecutionRepository
from ai_ent.persistence.repositories.leases import LeaseRepository
from ai_ent.scheduler.finalization import ExecutionFinalizer


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


def task_manifest(
    task_id: str = "TASK-FINALIZE",
    *,
    allowed_paths: tuple[str, ...] = ("tests/fixtures/proof.txt",),
    command: str = 'test "$(cat tests/fixtures/proof.txt)" = "AIENT_FINALIZER_PROOF=1"',
) -> BootstrapTask:
    return BootstrapTask(
        id=task_id,
        stage="T",
        title=f"Task {task_id}",
        executor="codex",
        objective="Create the finalizer proof fixture.",
        allowed_paths=allowed_paths,
        outputs=("tests/fixtures/proof.txt",),
        verification=VerificationSpec(commands=(command,)),
    )


def seed_claimed_execution(session: Session, task: BootstrapTask) -> tuple[Task, Execution, TaskLease]:
    ProjectRepository().create(session, project_id="project-1", name="Project 1")
    persisted_task = TaskRepository().create(
        session,
        task_id=task.id,
        project_id="project-1",
        title=task.title,
        status="running",
    )
    now = datetime.now(UTC)
    execution = ExecutionRepository().create(
        session,
        execution_id="execution-1",
        task_id=task.id,
        executor_type="codex",
        status="succeeded",
        started_at=now,
        finished_at=now,
        terminal_state="success",
    )
    lease = LeaseRepository().create(
        session,
        lease_id="lease-1",
        task_id=task.id,
        execution_id=execution.id,
        owner_id="worker-1",
        acquired_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    return persisted_task, execution, lease


def package_for(task: BootstrapTask, repo: Path) -> ExecutionPackage:
    return build_execution_package(
        task,
        repository_path=repo,
        worktree_path=repo,
        execution_id="execution-1",
        timeout_seconds=30,
    )


def write_proof(repo: Path, content: str = "AIENT_FINALIZER_PROOF=1\n") -> None:
    path = repo / "tests/fixtures/proof.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_successful_finalization_commits_and_updates_database(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = task_manifest()
    write_proof(repo)
    before_head = git(repo, "rev-parse", "HEAD")
    factory = session_factory()
    with factory() as session:
        seed_claimed_execution(session, task)

        result = ExecutionFinalizer(manifest_tasks={task.id: task}).finalize(
            session,
            package=package_for(task, repo),
            owner_id="worker-1",
        )

        assert result.status == "COMPLETED"
        assert result.candidate is not None
        assert result.commit_id is not None
        assert git(repo, "rev-parse", "HEAD") != before_head
        assert result.committed_tree_hash == result.candidate.tree_hash
        assert session.get(Task, task.id).status == "passed"  # type: ignore[union-attr]
        execution = session.get(Execution, "execution-1")
        assert execution is not None
        assert execution.status == "succeeded"
        assert execution.candidate_tree_hash == result.candidate.tree_hash
        assert execution.commit_hash == result.commit_id
        assert session.get(TaskLease, "lease-1").status == "completed"  # type: ignore[union-attr]
        assert session.scalars(select(Checkpoint).where(Checkpoint.execution_id == "execution-1")).one()


def test_scope_failure_blocks_without_commit_and_preserves_worktree(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = task_manifest(allowed_paths=("tests/fixtures/allowed.txt",))
    (repo / "src").mkdir()
    (repo / "src/bad.py").write_text("bad = True\n", encoding="utf-8")
    before_head = git(repo, "rev-parse", "HEAD")
    factory = session_factory()
    with factory() as session:
        seed_claimed_execution(session, task)

        result = ExecutionFinalizer(manifest_tasks={task.id: task}).finalize(
            session,
            package=package_for(task, repo),
            owner_id="worker-1",
        )

        assert result.status == "SCOPE_FAILED"
        assert "outside task scope" in (result.reason or "")
        assert git(repo, "rev-parse", "HEAD") == before_head
        assert session.get(Task, task.id).status == "blocked"  # type: ignore[union-attr]
        assert session.get(TaskLease, "lease-1").status == "released"  # type: ignore[union-attr]
        assert (repo / "src/bad.py").exists()


def test_verifier_failure_does_not_commit_or_complete(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = task_manifest()
    write_proof(repo, "wrong\n")
    before_head = git(repo, "rev-parse", "HEAD")
    factory = session_factory()
    with factory() as session:
        seed_claimed_execution(session, task)

        result = ExecutionFinalizer(manifest_tasks={task.id: task}).finalize(
            session,
            package=package_for(task, repo),
            owner_id="worker-1",
        )

        assert result.status == "VERIFICATION_FAILED"
        assert git(repo, "rev-parse", "HEAD") == before_head
        assert session.get(Task, task.id).status == "blocked"  # type: ignore[union-attr]
        assert session.get(Execution, "execution-1").commit_hash is None  # type: ignore[union-attr]
        assert (repo / "tests/fixtures/proof.txt").exists()


def test_mutation_after_verification_blocks_commit(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = task_manifest()
    write_proof(repo)
    before_head = git(repo, "rev-parse", "HEAD")

    def mutate() -> None:
        write_proof(repo, "AIENT_FINALIZER_PROOF=1\nmutated\n")

    factory = session_factory()
    with factory() as session:
        seed_claimed_execution(session, task)

        result = ExecutionFinalizer(
            manifest_tasks={task.id: task},
            after_verification=mutate,
        ).finalize(session, package=package_for(task, repo), owner_id="worker-1")

        assert result.status == "COMMIT_DENIED"
        assert "stale" in (result.reason or "")
        assert git(repo, "rev-parse", "HEAD") == before_head
        assert session.get(Task, task.id).status == "blocked"  # type: ignore[union-attr]


def test_db_failure_after_commit_is_reconcilable_and_retry_does_not_duplicate_commit(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = task_manifest()
    write_proof(repo)
    factory = session_factory()
    with factory() as session:
        seed_claimed_execution(session, task)
        finalizer = ExecutionFinalizer(manifest_tasks={task.id: task})

        def fail_record_success(*_: object, **__: object) -> Checkpoint:
            raise RuntimeError("database unavailable after commit")

        finalizer._record_success = fail_record_success  # type: ignore[method-assign]
        result = finalizer.finalize(session, package=package_for(task, repo), owner_id="worker-1")

        assert result.status == "COMMIT_CREATED_DB_PENDING"
        committed_head = git(repo, "rev-parse", "HEAD")

        retry = ExecutionFinalizer(manifest_tasks={task.id: task}).finalize(
            session,
            package=package_for(task, repo),
            owner_id="worker-1",
        )

        assert retry.status == "COMPLETED"
        assert git(repo, "rev-parse", "HEAD") == committed_head
        assert session.get(Task, task.id).status == "passed"  # type: ignore[union-attr]


def test_finalizer_requires_owned_active_lease_and_finished_execution(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = task_manifest()
    write_proof(repo)
    factory = session_factory()
    with factory() as session:
        seed_claimed_execution(session, task)

        wrong_owner = ExecutionFinalizer(manifest_tasks={task.id: task}).finalize(
            session,
            package=package_for(task, repo),
            owner_id="worker-2",
        )

        assert wrong_owner.status == "PRECONDITION_FAILED"
        assert wrong_owner.reason == "lease_not_owned_or_expired"


def test_already_finalized_execution_is_idempotent(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = task_manifest()
    write_proof(repo)
    factory = session_factory()
    with factory() as session:
        seed_claimed_execution(session, task)
        finalizer = ExecutionFinalizer(manifest_tasks={task.id: task})
        first = finalizer.finalize(session, package=package_for(task, repo), owner_id="worker-1")

        second = finalizer.finalize(session, package=package_for(task, repo), owner_id="worker-1")

        assert first.status == "COMPLETED"
        assert second.status == "IDEMPOTENT_COMPLETED"
        assert second.commit_id == first.commit_id


def test_finalizer_does_not_invoke_codex_or_schedule_next_task(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = task_manifest()
    write_proof(repo)
    factory = session_factory()
    with factory() as session:
        seed_claimed_execution(session, task)
        with (
            patch("ai_ent.bootstrap.codex.CodexExecutor.execute_package", side_effect=AssertionError("codex invoked")),
            patch("ai_ent.scheduler.iteration.SchedulerIterationService.run_once", side_effect=AssertionError("scheduler invoked")),
        ):

            result = ExecutionFinalizer(manifest_tasks={task.id: task}).finalize(
                session,
                package=package_for(task, repo),
                owner_id="worker-1",
            )

        assert result.status == "COMPLETED"
