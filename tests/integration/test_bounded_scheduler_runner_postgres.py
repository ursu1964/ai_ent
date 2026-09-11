from __future__ import annotations

import os
import shlex
import subprocess
import sys
import unittest
import uuid
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from alembic import command
from sqlalchemy import select

from ai_ent.bootstrap.codex import CodexConfig
from ai_ent.bootstrap.git import require_git
from ai_ent.bootstrap.models import BootstrapTask, VerificationSpec
from ai_ent.persistence.config import DatabaseConfigError, load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.persistence.migrations import build_alembic_config
from ai_ent.persistence.models import Checkpoint, Execution, Task, TaskLease
from ai_ent.persistence.repositories import ProjectRepository, TaskRepository
from ai_ent.scheduler.bounded import BoundedRunLimits, BoundedSchedulerRunner
from ai_ent.scheduler.execution import ClaimedExecutionRunner
from ai_ent.scheduler.finalization import ExecutionFinalizer
from ai_ent.scheduler.iteration import ExecutionPackageFactory, SchedulerIterationService
from ai_ent.scheduler.repair import RepairExecutionService, RepairPlanner, RepairPolicy
from tests.integration.helpers import clean_test_tables, make_test_project_id, make_test_suffix


def integration_database() -> Database:
    try:
        config = build_alembic_config(Path("alembic.ini"), Path(".env"))
        settings = load_database_settings(Path(".env"))
    except DatabaseConfigError as exc:
        raise unittest.SkipTest(f"bounded scheduler integration blocked: {exc}") from exc
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


def remove_task_worktrees(root: Path, task_ids: tuple[str, ...], executions: Sequence[Execution]) -> None:
    for execution in executions:
        if execution.task_id not in task_ids:
            continue
        branch = f"task/{execution.task_id}/{execution.id}"
        output = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=root,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ).stdout
        current: Path | None = None
        for line in output.splitlines():
            if line.startswith("worktree "):
                current = Path(line.split(" ", 1)[1])
            elif line == f"branch refs/heads/{branch}" and current is not None and current.exists():
                require_git(["worktree", "remove", "--force", str(current)], cwd=root)
                break
        subprocess.run(["git", "branch", "-D", branch], cwd=root, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def seed_project_tasks(database: Database, project_id: str, task_a: BootstrapTask, task_b: BootstrapTask) -> None:
    with database.session() as session:
        ProjectRepository().create(session, project_id=project_id, name=f"Project {project_id}")
        tasks = TaskRepository()
        tasks.create(session, task_id=task_a.id, project_id=project_id, title=task_a.title)
        tasks.create(session, task_id=task_b.id, project_id=project_id, title=task_b.title)
        for dependency in task_b.depends_on:
            tasks.add_dependency(session, task_id=task_b.id, depends_on_task_id=dependency)


def make_runner(
    *,
    manifest_tasks: dict[str, BootstrapTask],
    command: tuple[str, ...],
    worktree_root: Path,
    max_tasks: int,
    max_repairs: int = 1,
) -> BoundedSchedulerRunner:
    scheduler = SchedulerIterationService(
        package_factory=ExecutionPackageFactory(manifest_tasks=manifest_tasks, timeout_seconds=60),
        owner_id="worker-1",
        lease_duration=timedelta(minutes=10),
    )
    repair_policy = RepairPolicy(max_autonomous_repair_attempts=max_repairs)
    return BoundedSchedulerRunner(
        scheduler=scheduler,
        execution_runner=ClaimedExecutionRunner(
            config=CodexConfig(command=command, default_timeout_seconds=60),
            repository_path=Path.cwd(),
            worktree_root=worktree_root,
        ),
        finalizer=ExecutionFinalizer(manifest_tasks=manifest_tasks),
        repair_runner=RepairExecutionService(
            planner=RepairPlanner(manifest_tasks=manifest_tasks, policy=repair_policy),
            runner=ClaimedExecutionRunner(
                config=CodexConfig(command=command, default_timeout_seconds=60),
                repository_path=Path.cwd(),
                worktree_root=worktree_root,
            ),
            finalizer=ExecutionFinalizer(manifest_tasks=manifest_tasks),
            repository_path=Path.cwd(),
            worktree_root=worktree_root,
        ),
        limits=BoundedRunLimits(
            max_tasks_per_run=max_tasks,
            max_failures_per_run=1,
            max_repairs_per_task=max_repairs,
            repair_policy=repair_policy,
        ),
    )


def test_postgres_real_codex_two_task_chain_completes_and_stops(tmp_path: Path) -> None:
    raw_command = os.environ.get("AIENT_CODEX_COMMAND")
    if not raw_command:
        raise unittest.SkipTest("AIENT_CODEX_COMMAND is not configured for real TASK-0028 bounded proof")

    database = integration_database()
    clean_test_tables(database)
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id = make_test_project_id(suffix)
    task_a = proof_task(f"TEST-BOUND-A-{suffix}", "bounded_a.txt", "AIENT_BOUNDED_A=1")
    task_b = proof_task(
        f"TEST-BOUND-B-{suffix}",
        "bounded_b.txt",
        "AIENT_BOUNDED_B=1",
        depends_on=(task_a.id,),
    )
    manifest_tasks = {task_a.id: task_a, task_b.id: task_b}
    root = Path.cwd()
    main_head = require_git(["rev-parse", "HEAD"], cwd=root)

    try:
        seed_project_tasks(database, project_id, task_a, task_b)
        with database.session() as session:
            result = make_runner(
                manifest_tasks=manifest_tasks,
                command=tuple(shlex.split(raw_command)),
                worktree_root=tmp_path / "worktrees",
                max_tasks=2,
            ).run(session, project_id=project_id)

            assert result.stop_reason == "NO_READY_TASK"
            assert result.tasks_attempted == 2
            assert result.tasks_completed == 2
            assert len(result.commits_created) == 2

        assert require_git(["rev-parse", "HEAD"], cwd=root) == main_head
        with database.session() as session:
            executions = session.scalars(select(Execution).where(Execution.task_id.in_([task_a.id, task_b.id]))).all()
            leases = session.scalars(select(TaskLease).where(TaskLease.task_id.in_([task_a.id, task_b.id]))).all()
            assert session.get(Task, task_a.id).status == "passed"  # type: ignore[union-attr]
            assert session.get(Task, task_b.id).status == "passed"  # type: ignore[union-attr]
            assert len(executions) == 2
            assert all(execution.commit_hash for execution in executions)
            assert len(leases) == 2
            assert all(lease.status == "completed" for lease in leases)
            remove_task_worktrees(root, (task_a.id, task_b.id), executions)
    finally:
        with database.session() as session:
            executions = session.scalars(select(Execution).where(Execution.task_id.in_([task_a.id, task_b.id]))).all()
            remove_task_worktrees(root, (task_a.id, task_b.id), executions)
        clean_test_tables(database)
        database.dispose()


def test_postgres_max_tasks_one_leaves_dependent_ready_for_next_run(tmp_path: Path) -> None:
    database = integration_database()
    clean_test_tables(database)
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id = make_test_project_id(suffix)
    task_a = proof_task(f"TEST-BOUND-MAX-A-{suffix}", "bounded_max_a.txt", "AIENT_BOUNDED_MAX_A=1")
    task_b = proof_task(
        f"TEST-BOUND-MAX-B-{suffix}",
        "bounded_max_b.txt",
        "AIENT_BOUNDED_MAX_B=1",
        depends_on=(task_a.id,),
    )
    manifest_tasks = {task_a.id: task_a, task_b.id: task_b}
    writer = (
        sys.executable,
        "-c",
        (
            "import os; from pathlib import Path; "
            "task=os.environ['AIENT_TASK_ID']; "
            "data={'TEST-BOUND-MAX-A':'bounded_max_a.txt:AIENT_BOUNDED_MAX_A=1',"
            "'TEST-BOUND-MAX-B':'bounded_max_b.txt:AIENT_BOUNDED_MAX_B=1'}; "
            "key='TEST-BOUND-MAX-A' if 'MAX-A' in task else 'TEST-BOUND-MAX-B'; "
            "name,marker=data[key].split(':'); "
            "p=Path('tests/fixtures')/name; p.parent.mkdir(parents=True, exist_ok=True); "
            "p.write_text(marker+'\\n', encoding='utf-8')"
        ),
    )
    root = Path.cwd()

    try:
        seed_project_tasks(database, project_id, task_a, task_b)
        with database.session() as session:
            result = make_runner(
                manifest_tasks=manifest_tasks,
                command=writer,
                worktree_root=tmp_path / "worktrees",
                max_tasks=1,
            ).run(session, project_id=project_id)

            assert result.stop_reason == "MAX_TASKS_PER_RUN"
            assert result.tasks_completed == 1

        with database.session() as session:
            assert session.get(Task, task_a.id).status == "passed"  # type: ignore[union-attr]
            assert session.get(Task, task_b.id).status == "pending"  # type: ignore[union-attr]
            executions = session.scalars(select(Execution).where(Execution.task_id.in_([task_a.id, task_b.id]))).all()
            assert len(executions) == 1
            remove_task_worktrees(root, (task_a.id, task_b.id), executions)
    finally:
        with database.session() as session:
            executions = session.scalars(select(Execution).where(Execution.task_id.in_([task_a.id, task_b.id]))).all()
            remove_task_worktrees(root, (task_a.id, task_b.id), executions)
        clean_test_tables(database)
        database.dispose()


def test_postgres_failure_stop_does_not_start_dependent_task(tmp_path: Path) -> None:
    database = integration_database()
    clean_test_tables(database)
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id = make_test_project_id(suffix)
    task_a = proof_task(f"TEST-BOUND-FAIL-A-{suffix}", "bounded_fail_a.txt", "AIENT_BOUNDED_FAIL_A=1")
    task_b = proof_task(
        f"TEST-BOUND-FAIL-B-{suffix}",
        "bounded_fail_b.txt",
        "AIENT_BOUNDED_FAIL_B=1",
        depends_on=(task_a.id,),
    )
    manifest_tasks = {task_a.id: task_a, task_b.id: task_b}
    wrong_writer = (
        sys.executable,
        "-c",
        (
            "from pathlib import Path; "
            "p=Path('tests/fixtures/bounded_fail_a.txt'); "
            "p.parent.mkdir(parents=True, exist_ok=True); "
            "p.write_text('wrong\\n', encoding='utf-8')"
        ),
    )
    root = Path.cwd()

    try:
        seed_project_tasks(database, project_id, task_a, task_b)
        with database.session() as session:
            result = make_runner(
                manifest_tasks=manifest_tasks,
                command=wrong_writer,
                worktree_root=tmp_path / "worktrees",
                max_tasks=2,
                max_repairs=1,
            ).run(session, project_id=project_id)

            assert result.tasks_attempted == 1
            assert result.tasks_failed == 1
            assert result.repairs_attempted == 1
            assert result.stop_reason == "MAX_FAILURES_PER_RUN"

        with database.session() as session:
            assert session.get(Task, task_a.id).status == "blocked"  # type: ignore[union-attr]
            assert session.get(Task, task_b.id).status == "pending"  # type: ignore[union-attr]
            executions = session.scalars(select(Execution).where(Execution.task_id.in_([task_a.id, task_b.id]))).all()
            assert len(executions) == 2
            assert all(execution.commit_hash is None for execution in executions)
            remove_task_worktrees(root, (task_a.id, task_b.id), executions)
    finally:
        with database.session() as session:
            executions = session.scalars(select(Execution).where(Execution.task_id.in_([task_a.id, task_b.id]))).all()
            remove_task_worktrees(root, (task_a.id, task_b.id), executions)
        clean_test_tables(database)
        database.dispose()


def test_postgres_terminal_failure_survives_outer_stop_path_rollback(tmp_path: Path) -> None:
    database = integration_database()
    clean_test_tables(database)
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id = make_test_project_id(suffix)
    task_a = proof_task(f"TEST-BOUND-ROLLBACK-A-{suffix}", "bounded_rollback_a.txt", "AIENT_BOUNDED_ROLLBACK_A=1")
    task_b = proof_task(
        f"TEST-BOUND-ROLLBACK-B-{suffix}",
        "bounded_rollback_b.txt",
        "AIENT_BOUNDED_ROLLBACK_B=1",
        depends_on=(task_a.id,),
    )
    manifest_tasks = {task_a.id: task_a, task_b.id: task_b}
    wrong_writer = (
        sys.executable,
        "-c",
        (
            "from pathlib import Path; "
            "p=Path('tests/fixtures/bounded_rollback_a.txt'); "
            "p.parent.mkdir(parents=True, exist_ok=True); "
            "p.write_text('wrong\\n', encoding='utf-8')"
        ),
    )
    root = Path.cwd()

    try:
        seed_project_tasks(database, project_id, task_a, task_b)
        try:
            with database.session() as session:
                result = make_runner(
                    manifest_tasks=manifest_tasks,
                    command=wrong_writer,
                    worktree_root=tmp_path / "worktrees",
                    max_tasks=2,
                    max_repairs=1,
                ).run(session, project_id=project_id)

                assert result.tasks_attempted == 1
                assert result.tasks_failed == 1
                assert result.repairs_attempted == 1
                assert result.stop_reason == "MAX_FAILURES_PER_RUN"
                raise RuntimeError("outer bounded stop wrapper failed after terminal outcome")
        except RuntimeError as exc:
            assert str(exc) == "outer bounded stop wrapper failed after terminal outcome"

        with database.session() as session:
            executions = session.scalars(
                select(Execution).where(Execution.task_id == task_a.id).order_by(Execution.attempt)
            ).all()
            leases = session.scalars(select(TaskLease).where(TaskLease.task_id == task_a.id)).all()
            checkpoints = session.scalars(select(Checkpoint).where(Checkpoint.task_id == task_a.id)).all()

            assert session.get(Task, task_a.id).status == "blocked"  # type: ignore[union-attr]
            assert session.get(Task, task_b.id).status == "pending"  # type: ignore[union-attr]
            assert [execution.attempt for execution in executions] == [1, 2]
            assert all(execution.status == "failed" for execution in executions)
            assert all(execution.commit_hash is None for execution in executions)
            assert all(lease.status == "released" for lease in leases)
            assert len(checkpoints) == 2
            remove_task_worktrees(root, (task_a.id, task_b.id), executions)
    finally:
        with database.session() as session:
            executions = session.scalars(select(Execution).where(Execution.task_id.in_([task_a.id, task_b.id]))).all()
            remove_task_worktrees(root, (task_a.id, task_b.id), executions)
        clean_test_tables(database)
        database.dispose()
