from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.bootstrap.codex import build_execution_package
from ai_ent.bootstrap.models import BootstrapTask, VerificationSpec
from ai_ent.persistence.models import Base, Execution, Task, TaskLease
from ai_ent.persistence.repositories import CheckpointRepository, ProjectRepository, TaskRepository
from ai_ent.persistence.repositories.executions import ExecutionRepository
from ai_ent.persistence.repositories.leases import LeaseRepository
from ai_ent.scheduler.bounded import BoundedSchedulerRunner
from ai_ent.scheduler.finalization import ExecutionFinalizer
from ai_ent.scheduler.recovery import SchedulerRecoveryService


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


def task_manifest(task_id: str = "TASK-RECOVER") -> BootstrapTask:
    return BootstrapTask(
        id=task_id,
        stage="T",
        title=f"Task {task_id}",
        executor="codex",
        objective="Create recovery proof fixture.",
        allowed_paths=("tests/fixtures/recovery.txt",),
        outputs=("tests/fixtures/recovery.txt",),
        verification=VerificationSpec(commands=('test "$(cat tests/fixtures/recovery.txt)" = "AIENT_RECOVERY=1"',)),
    )


def seed_task(session: Session, task: BootstrapTask, *, status: str = "running") -> Task:
    ProjectRepository().create(session, project_id="project-1", name="Project 1")
    return TaskRepository().create(
        session,
        task_id=task.id,
        project_id="project-1",
        title=task.title,
        status=status,  # type: ignore[arg-type]
    )


def seed_execution(
    session: Session,
    task: BootstrapTask,
    *,
    status: str = "running",
    commit_hash: str | None = None,
    candidate_tree_hash: str | None = None,
    attempt: int = 1,
) -> Execution:
    return ExecutionRepository().create(
        session,
        execution_id=f"execution-{attempt}",
        task_id=task.id,
        executor_type="codex",
        status=status,  # type: ignore[arg-type]
        attempt=attempt,
        started_at=datetime.now(UTC),
        terminal_state="success" if status == "succeeded" else None,
        commit_hash=commit_hash,
        candidate_tree_hash=candidate_tree_hash,
    )


def seed_lease(session: Session, task: BootstrapTask, execution: Execution) -> TaskLease:
    now = datetime.now(UTC)
    return LeaseRepository().create(
        session,
        lease_id=f"lease-{execution.id}",
        task_id=task.id,
        execution_id=execution.id,
        owner_id="worker-1",
        acquired_at=now,
        expires_at=now + timedelta(minutes=10),
    )


def package_for(task: BootstrapTask, repo: Path, execution: Execution) -> object:
    return build_execution_package(task, repository_path=repo, worktree_path=repo, execution_id=execution.id)


def write_recovery_file(repo: Path, content: str = "AIENT_RECOVERY=1\n") -> None:
    path = repo / "tests/fixtures/recovery.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_crash_after_claim_recovers_without_duplicate_execution(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    task = task_manifest()
    factory = session_factory()
    with factory() as session:
        seed_task(session, task)
        execution = seed_execution(session, task, status="running")
        seed_lease(session, task, execution)

        result = SchedulerRecoveryService().recover_execution(
            session,
            package=build_execution_package(task, repository_path=repo, worktree_path=tmp_path / "missing", execution_id=execution.id),
            owner_id="worker-1",
        )

        assert result.detected_stage == "AFTER_CLAIM"
        assert result.action == "RESUME_EXECUTION"
        assert result.safe_to_continue
        assert len(ExecutionRepository().list_by_task(session, task.id)) == 1


def test_crash_after_worktree_creation_can_resume_execution(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    task = task_manifest()
    factory = session_factory()
    with factory() as session:
        seed_task(session, task)
        execution = seed_execution(session, task, status="running")
        seed_lease(session, task, execution)

        result = SchedulerRecoveryService().recover_execution(
            session,
            package=package_for(task, repo, execution),  # type: ignore[arg-type]
            owner_id="worker-1",
        )

        assert result.detected_stage == "AFTER_WORKTREE_CREATED"
        assert result.action == "RESUME_EXECUTION"


def test_uncertain_running_codex_does_not_start_duplicate_codex(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    task = task_manifest()
    write_recovery_file(repo)
    factory = session_factory()
    with factory() as session:
        seed_task(session, task)
        execution = seed_execution(session, task, status="running")
        seed_lease(session, task, execution)

        result = SchedulerRecoveryService().recover_execution(
            session,
            package=package_for(task, repo, execution),  # type: ignore[arg-type]
            owner_id="worker-1",
        )

        assert result.detected_stage == "DURING_EXECUTOR"
        assert result.action == "HUMAN_REQUIRED"
        assert result.remaining_blocker == "uncertain_executor_process_state"


def test_executor_completed_restart_resumes_finalization_not_executor(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    task = task_manifest()
    write_recovery_file(repo)
    factory = session_factory()
    with factory() as session:
        seed_task(session, task)
        execution = seed_execution(session, task, status="succeeded")
        seed_lease(session, task, execution)

        result = SchedulerRecoveryService(finalizer=ExecutionFinalizer(manifest_tasks={task.id: task})).recover_execution(
            session,
            package=package_for(task, repo, execution),  # type: ignore[arg-type]
            owner_id="worker-1",
        )

        assert result.detected_stage == "AFTER_EXECUTOR_BEFORE_VERIFICATION"
        assert result.action == "RESUME_FINALIZATION"
        assert result.action_performed
        assert result.safe_to_continue
        assert session.get(Task, task.id).status == "passed"  # type: ignore[union-attr]
        assert session.get(Execution, execution.id).commit_hash is not None  # type: ignore[union-attr]


def test_verification_pass_with_changed_candidate_blocks_commit(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    task = task_manifest()
    write_recovery_file(repo)
    factory = session_factory()
    with factory() as session:
        seed_task(session, task)
        execution = seed_execution(session, task, status="succeeded")
        seed_lease(session, task, execution)
        CheckpointRepository().create(
            session,
            checkpoint_id="checkpoint-pass",
            task_id=task.id,
            execution_id=execution.id,
            checkpoint_type="execution",
            state=json.dumps({"status": "verification_pass"}),
            tree_hash="not-current-tree",
        )

        result = SchedulerRecoveryService().recover_execution(
            session,
            package=package_for(task, repo, execution),  # type: ignore[arg-type]
            owner_id="worker-1",
        )

        assert result.detected_stage == "AFTER_VERIFICATION_BEFORE_COMMIT"
        assert result.action == "BLOCK"
        assert result.remaining_blocker == "stale_verified_candidate"


def test_commit_db_gap_reconciles_without_duplicate_commit(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    task = task_manifest()
    write_recovery_file(repo)
    git(repo, "add", "tests/fixtures/recovery.txt")
    git(repo, "commit", "-m", "complete task")
    commit = git(repo, "rev-parse", "--short", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    factory = session_factory()
    with factory() as session:
        seed_task(session, task)
        execution = seed_execution(session, task, status="failed", commit_hash=commit, candidate_tree_hash=tree)
        seed_lease(session, task, execution)

        result = SchedulerRecoveryService().recover_execution(
            session,
            package=package_for(task, repo, execution),  # type: ignore[arg-type]
            owner_id="worker-1",
        )
        second = SchedulerRecoveryService().recover_execution(
            session,
            package=package_for(task, repo, execution),  # type: ignore[arg-type]
            owner_id="worker-1",
        )

        assert result.action == "COMPLETE_DB_RECONCILIATION"
        assert result.action_performed
        assert second.action == "READY_TO_CONTINUE"
        assert session.get(Task, task.id).status == "passed"  # type: ignore[union-attr]
        assert session.get(TaskLease, f"lease-{execution.id}").status == "completed"  # type: ignore[union-attr]
        assert git(repo, "rev-list", "--count", "HEAD") == "2"


def test_repair_interruption_is_classified_without_new_attempt(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    task = task_manifest()
    factory = session_factory()
    with factory() as session:
        seed_task(session, task)
        execution = seed_execution(session, task, status="failed", attempt=2)

        result = SchedulerRecoveryService().recover_execution(
            session,
            package=package_for(task, repo, execution),  # type: ignore[arg-type]
            owner_id="worker-1",
        )

        assert result.detected_stage == "DURING_REPAIR"
        assert result.action == "REPAIR_RECONCILIATION"
        assert len(ExecutionRepository().list_by_task(session, task.id)) == 1


def test_crash_between_tasks_is_ready_to_continue() -> None:
    factory = session_factory()
    task = task_manifest()
    with factory() as session:
        ProjectRepository().create(session, project_id="project-1", name="Project 1")
        TaskRepository().create(session, task_id=task.id, project_id="project-1", title=task.title, status="pending")

        result = SchedulerRecoveryService().recover_project(session, project_id="project-1")

        assert result.detected_stage == "BETWEEN_TASKS"
        assert result.action == "READY_TO_CONTINUE"
        assert result.safe_to_continue


def test_recover_project_blocks_orphan_execution_worktree(tmp_path: Path) -> None:
    factory = session_factory()
    task = task_manifest("IMPL-ORPHAN")
    worktree_root = tmp_path / "worktrees"
    orphan = worktree_root / task.id / "execution-orphan"
    orphan.mkdir(parents=True)
    (orphan / "src").mkdir()
    (orphan / "src/orphan.py").write_text("# orphaned partial task output\n", encoding="utf-8")

    with factory() as session:
        ProjectRepository().create(session, project_id="project-1", name="Project 1")
        TaskRepository().create(session, task_id=task.id, project_id="project-1", title=task.title, status="pending")

        result = SchedulerRecoveryService(worktree_root=worktree_root).recover_project(session, project_id="project-1")

        assert result.detected_stage == "UNKNOWN"
        assert result.action == "BLOCK"
        assert result.remaining_blocker == "orphan_execution_worktree"
        assert result.interrupted_task_id == task.id
        assert result.execution_id == "execution-orphan"


def test_recover_then_run_stops_when_recovery_not_safe(tmp_path: Path) -> None:
    class BlockRecovery:
        def recover_project(self, _: object, *, project_id: str) -> object:
            return type(
                "Blocked",
                (),
                {"safe_to_continue": False},
            )()

    runner = BoundedSchedulerRunner(recovery=BlockRecovery())  # type: ignore[arg-type]

    recovery, run = runner.recover_then_run(None, project_id="project-1")  # type: ignore[arg-type]

    assert not recovery.safe_to_continue
    assert run is None
