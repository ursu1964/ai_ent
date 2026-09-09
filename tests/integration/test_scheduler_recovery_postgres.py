from __future__ import annotations

import os
import shlex
import subprocess
import sys
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from sqlalchemy import select

from ai_ent.bootstrap.codex import CodexConfig
from ai_ent.bootstrap.git import require_git
from ai_ent.bootstrap.models import BootstrapTask, VerificationSpec
from ai_ent.persistence.config import DatabaseConfigError, load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.persistence.migrations import build_alembic_config
from ai_ent.persistence.models import Execution, Task, TaskLease
from ai_ent.persistence.repositories import ProjectRepository, TaskRepository
from ai_ent.persistence.repositories.executions import ExecutionRepository
from ai_ent.persistence.repositories.leases import LeaseRepository
from ai_ent.scheduler.bounded import BoundedRunLimits, BoundedSchedulerRunner
from ai_ent.scheduler.execution import ClaimedExecutionRunner
from ai_ent.scheduler.finalization import ExecutionFinalizer
from ai_ent.scheduler.iteration import ExecutionPackageFactory, SchedulerIterationService
from ai_ent.scheduler.recovery import SchedulerRecoveryService
from tests.integration.helpers import clean_test_tables, make_test_project_id, make_test_suffix


def integration_database() -> Database:
    try:
        config = build_alembic_config(Path("alembic.ini"), Path(".env"))
        settings = load_database_settings(Path(".env"))
    except DatabaseConfigError as exc:
        raise unittest.SkipTest(f"scheduler recovery integration blocked: {exc}") from exc
    command.upgrade(config, "head")
    return Database(settings)


def proof_task(task_id: str, filename: str, marker: str, *, depends_on: tuple[str, ...] = ()) -> BootstrapTask:
    output = f"tests/fixtures/{filename}"
    return BootstrapTask(
        id=task_id,
        stage="T",
        title=f"Task {task_id}",
        executor="codex",
        depends_on=depends_on,
        objective=f"Create {output} containing exactly {marker}",
        allowed_paths=(output,),
        outputs=(output,),
        verification=VerificationSpec(commands=(f'test "$(cat {output})" = "{marker}"',)),
    )


def remove_task_worktrees(root: Path, task_ids: tuple[str, ...], executions: list[Execution]) -> None:
    output = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ).stdout
    for execution in executions:
        if execution.task_id not in task_ids:
            continue
        branch = f"task/{execution.task_id}/{execution.id}"
        current: Path | None = None
        for line in output.splitlines():
            if line.startswith("worktree "):
                current = Path(line.split(" ", 1)[1])
            elif line == f"branch refs/heads/{branch}" and current is not None and current.exists():
                require_git(["worktree", "remove", "--force", str(current)], cwd=root)
                break
        subprocess.run(["git", "branch", "-D", branch], cwd=root, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def test_postgres_recovery_after_executor_resumes_finalization_once(tmp_path: Path) -> None:
    raw_command = os.environ.get("AIENT_CODEX_COMMAND")
    if not raw_command:
        raise unittest.SkipTest("AIENT_CODEX_COMMAND is not configured for real recovery proof A")

    database = integration_database()
    clean_test_tables(database)
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id = make_test_project_id(suffix)
    task = proof_task(f"TEST-RECOVER-A-{suffix}", "recover_a.txt", "AIENT_RECOVER_A=1")
    root = Path.cwd()
    executions_for_cleanup: list[Execution] = []

    try:
        with database.session() as session:
            ProjectRepository().create(session, project_id=project_id, name=f"Project {suffix}")
            TaskRepository().create(session, task_id=task.id, project_id=project_id, title=task.title)
            scheduled = SchedulerIterationService(
                package_factory=ExecutionPackageFactory(manifest_tasks={task.id: task}, timeout_seconds=60),
                owner_id="worker-1",
                lease_duration=timedelta(minutes=10),
            ).run_once(session, project_id=project_id)
            assert scheduled.package is not None
            executed = ClaimedExecutionRunner(
                config=CodexConfig(command=tuple(shlex.split(raw_command)), default_timeout_seconds=60),
                repository_path=root,
                worktree_root=tmp_path / "worktrees",
            ).run_claimed(session, package=scheduled.package, owner_id="worker-1")
            assert executed.status == "EXECUTED"
            assert executed.package is not None

            recovery = SchedulerRecoveryService(finalizer=ExecutionFinalizer(manifest_tasks={task.id: task})).recover_execution(
                session,
                package=executed.package,
                owner_id="worker-1",
            )
            assert recovery.action == "RESUME_FINALIZATION"
            assert recovery.action_performed
            assert recovery.safe_to_continue

        with database.session() as session:
            executions_for_cleanup = list(session.scalars(select(Execution).where(Execution.task_id == task.id)).all())
            assert len(executions_for_cleanup) == 1
            assert executions_for_cleanup[0].commit_hash is not None
            assert session.get(Task, task.id).status == "passed"  # type: ignore[union-attr]
    finally:
        remove_task_worktrees(root, (task.id,), executions_for_cleanup)
        clean_test_tables(database)
        database.dispose()


def test_postgres_recovery_reconciles_commit_db_gap_without_duplicate_commit(tmp_path: Path) -> None:
    database = integration_database()
    clean_test_tables(database)
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id = make_test_project_id(suffix)
    task = proof_task(f"TEST-RECOVER-B-{suffix}", "recover_b.txt", "AIENT_RECOVER_B=1")
    root = tmp_path / "repo"
    root.mkdir()
    require_git(["init"], cwd=root)
    require_git(["config", "user.email", "test@example.local"], cwd=root)
    require_git(["config", "user.name", "Test User"], cwd=root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    require_git(["add", "README.md"], cwd=root)
    require_git(["commit", "-m", "seed"], cwd=root)
    output = root / "tests/fixtures/recover_b.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("AIENT_RECOVER_B=1\n", encoding="utf-8")
    require_git(["add", "tests/fixtures/recover_b.txt"], cwd=root)
    require_git(["commit", "-m", "complete task"], cwd=root)
    commit = require_git(["rev-parse", "--short", "HEAD"], cwd=root)
    tree = require_git(["rev-parse", "HEAD^{tree}"], cwd=root)

    try:
        with database.session() as session:
            ProjectRepository().create(session, project_id=project_id, name=f"Project {suffix}")
            TaskRepository().create(session, task_id=task.id, project_id=project_id, title=task.title, status="running")
            execution = ExecutionRepository().create(
                session,
                execution_id=f"execution-{suffix}",
                task_id=task.id,
                executor_type="codex",
                status="failed",
                started_at=datetime.now(UTC),
                terminal_state="failure",
                error_classification="db_pending",
                candidate_tree_hash=tree,
                commit_hash=commit,
            )
            now = datetime.now(UTC)
            LeaseRepository().create(
                session,
                lease_id=f"lease-{suffix}",
                task_id=task.id,
                execution_id=execution.id,
                owner_id="worker-1",
                acquired_at=now,
                expires_at=now + timedelta(minutes=10),
            )
            package = __import__("ai_ent.bootstrap.codex", fromlist=["build_execution_package"]).build_execution_package(
                task,
                repository_path=root,
                worktree_path=root,
                execution_id=execution.id,
            )
            recovery = SchedulerRecoveryService().recover_execution(session, package=package, owner_id="worker-1")
            second = SchedulerRecoveryService().recover_execution(session, package=package, owner_id="worker-1")

            assert recovery.action == "COMPLETE_DB_RECONCILIATION"
            assert second.action == "READY_TO_CONTINUE"
            assert session.get(Task, task.id).status == "passed"  # type: ignore[union-attr]
            assert session.get(TaskLease, f"lease-{suffix}").status == "completed"  # type: ignore[union-attr]
            assert require_git(["rev-list", "--count", "HEAD"], cwd=root) == "2"
    finally:
        clean_test_tables(database)
        database.dispose()


def test_postgres_recovery_between_tasks_then_bounded_runner_processes_next(tmp_path: Path) -> None:
    database = integration_database()
    clean_test_tables(database)
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id = make_test_project_id(suffix)
    task_a = proof_task(f"TEST-RECOVER-C-A-{suffix}", "recover_c_a.txt", "AIENT_RECOVER_C_A=1")
    task_b = proof_task(
        f"TEST-RECOVER-C-B-{suffix}",
        "recover_c_b.txt",
        "AIENT_RECOVER_C_B=1",
        depends_on=(task_a.id,),
    )
    writer = (
        sys.executable,
        "-c",
        (
            "import os; from pathlib import Path; "
            "task=os.environ['AIENT_TASK_ID']; "
            "name='recover_c_b.txt'; marker='AIENT_RECOVER_C_B=1'; "
            "p=Path('tests/fixtures')/name; p.parent.mkdir(parents=True, exist_ok=True); "
            "p.write_text(marker+'\\n', encoding='utf-8')"
        ),
    )
    root = Path.cwd()
    executions_for_cleanup: list[Execution] = []

    try:
        with database.session() as session:
            ProjectRepository().create(session, project_id=project_id, name=f"Project {suffix}")
            tasks = TaskRepository()
            tasks.create(session, task_id=task_a.id, project_id=project_id, title=task_a.title, status="passed")
            tasks.create(session, task_id=task_b.id, project_id=project_id, title=task_b.title)
            tasks.add_dependency(session, task_id=task_b.id, depends_on_task_id=task_a.id)

            recovery = SchedulerRecoveryService().recover_project(session, project_id=project_id)
            runner = BoundedSchedulerRunner(
                scheduler=SchedulerIterationService(
                    package_factory=ExecutionPackageFactory(manifest_tasks={task_a.id: task_a, task_b.id: task_b}, timeout_seconds=30),
                    owner_id="worker-1",
                    lease_duration=timedelta(minutes=5),
                ),
                execution_runner=ClaimedExecutionRunner(
                    config=CodexConfig(command=writer, default_timeout_seconds=30),
                    repository_path=root,
                    worktree_root=tmp_path / "worktrees",
                ),
                finalizer=ExecutionFinalizer(manifest_tasks={task_a.id: task_a, task_b.id: task_b}),
                limits=BoundedRunLimits(max_tasks_per_run=1),
            )
            bounded = runner.run(session, project_id=project_id)

            assert recovery.action == "READY_TO_CONTINUE"
            assert bounded.tasks_completed == 1
            assert session.get(Task, task_b.id).status == "passed"  # type: ignore[union-attr]

        with database.session() as session:
            executions_for_cleanup = list(session.scalars(select(Execution).where(Execution.task_id.in_([task_a.id, task_b.id]))).all())
    finally:
        remove_task_worktrees(root, (task_a.id, task_b.id), executions_for_cleanup)
        clean_test_tables(database)
        database.dispose()
