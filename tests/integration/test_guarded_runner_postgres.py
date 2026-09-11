from __future__ import annotations

import os
import shlex
import sys
import unittest
import uuid
from pathlib import Path

from sqlalchemy import select

from ai_ent.bootstrap.codex import CodexConfig
from ai_ent.persistence.models import Execution, Task, TaskLease
from ai_ent.scheduler.guarded import (
    GuardedAutonomousRunner,
    GuardedPreflightResult,
    GuardedRunConfig,
    PostgresAuthorityResult,
)
from tests.integration.helpers import clean_test_tables, make_test_project_id, make_test_suffix
from tests.integration.test_bounded_scheduler_runner_postgres import (
    integration_database,
    make_runner,
    proof_task,
    remove_task_worktrees,
    seed_project_tasks,
)


def ok_preflight() -> GuardedPreflightResult:
    return GuardedPreflightResult(True, "test preflight")


def ok_authority(_: object, project_id: str) -> PostgresAuthorityResult:
    return PostgresAuthorityResult(True, f"isolated test authority for {project_id}")


def writer_command(filename: str, marker: str) -> tuple[str, ...]:
    return (
        sys.executable,
        "-c",
        (
            "from pathlib import Path; "
            f"p=Path('tests/fixtures/{filename}'); "
            "p.parent.mkdir(parents=True, exist_ok=True); "
            f"p.write_text('{marker}\\n', encoding='utf-8')"
        ),
    )


def guarded_runner(*, bounded_runner: object) -> GuardedAutonomousRunner:
    return GuardedAutonomousRunner(
        bounded_runner=bounded_runner,  # type: ignore[arg-type]
        codex_config=CodexConfig(command=(sys.executable,), default_timeout_seconds=30),
        preflight=ok_preflight,
        authority_check=ok_authority,
    )


def test_postgres_guarded_run_real_codex_two_task_chain(tmp_path: Path) -> None:
    raw_command = os.environ.get("AIENT_CODEX_COMMAND")
    if not raw_command:
        raise unittest.SkipTest("AIENT_CODEX_COMMAND is not configured for real TASK-0030 guarded proof")

    database = integration_database()
    clean_test_tables(database)
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id = make_test_project_id(suffix)
    task_a = proof_task(f"TEST-GUARD-A-{suffix}", "guarded_a.txt", "AIENT_GUARDED_A=1")
    task_b = proof_task(
        f"TEST-GUARD-B-{suffix}",
        "guarded_b.txt",
        "AIENT_GUARDED_B=1",
        depends_on=(task_a.id,),
    )
    manifest_tasks = {task_a.id: task_a, task_b.id: task_b}
    root = Path.cwd()

    try:
        seed_project_tasks(database, project_id, task_a, task_b)
        with database.session() as session:
            result = guarded_runner(
                bounded_runner=make_runner(
                    manifest_tasks=manifest_tasks,
                    command=tuple(shlex.split(raw_command)),
                    worktree_root=tmp_path / "worktrees",
                    max_tasks=2,
                )
            ).run(
                session,
                GuardedRunConfig(project_id=project_id, max_tasks_per_run=2, max_wall_clock_seconds=240),
            )

            assert result.stop_reason == "NO_READY_TASK"
            assert result.tasks_completed == 2
            assert len(result.commits_created) == 2
            assert result.remaining_ready_tasks == ()

        with database.session() as session:
            executions = session.scalars(select(Execution).where(Execution.task_id.in_([task_a.id, task_b.id]))).all()
            leases = session.scalars(select(TaskLease).where(TaskLease.task_id.in_([task_a.id, task_b.id]))).all()
            assert len(executions) == 2
            assert len(leases) == 2
            assert all(lease.status == "completed" for lease in leases)
            assert session.get(Task, task_a.id).status == "passed"  # type: ignore[union-attr]
            assert session.get(Task, task_b.id).status == "passed"  # type: ignore[union-attr]
            remove_task_worktrees(root, (task_a.id, task_b.id), executions)
    finally:
        with database.session() as session:
            executions = session.scalars(select(Execution).where(Execution.task_id.in_([task_a.id, task_b.id]))).all()
            remove_task_worktrees(root, (task_a.id, task_b.id), executions)
        clean_test_tables(database)
        database.dispose()


def test_postgres_guarded_max_tasks_one_leaves_next_ready(tmp_path: Path) -> None:
    database = integration_database()
    clean_test_tables(database)
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id = make_test_project_id(suffix)
    task_a = proof_task(f"TEST-GUARD-MAX-A-{suffix}", "guarded_max_a.txt", "AIENT_GUARDED_MAX_A=1")
    task_b = proof_task(
        f"TEST-GUARD-MAX-B-{suffix}",
        "guarded_max_b.txt",
        "AIENT_GUARDED_MAX_B=1",
        depends_on=(task_a.id,),
    )
    manifest_tasks = {task_a.id: task_a, task_b.id: task_b}
    root = Path.cwd()

    try:
        seed_project_tasks(database, project_id, task_a, task_b)
        with database.session() as session:
            result = guarded_runner(
                bounded_runner=make_runner(
                    manifest_tasks=manifest_tasks,
                    command=writer_command("guarded_max_a.txt", "AIENT_GUARDED_MAX_A=1"),
                    worktree_root=tmp_path / "worktrees",
                    max_tasks=1,
                )
            ).run(session, GuardedRunConfig(project_id=project_id, max_tasks_per_run=1))

            assert result.stop_reason == "TASK_LIMIT"
            assert result.tasks_completed == 1
            assert result.likely_next_task_id == task_b.id
            assert result.remaining_ready_tasks == (task_b.id,)

        with database.session() as session:
            assert session.get(Task, task_a.id).status == "passed"  # type: ignore[union-attr]
            assert session.get(Task, task_b.id).status == "pending"  # type: ignore[union-attr]
            executions = session.scalars(select(Execution).where(Execution.task_id.in_([task_a.id, task_b.id]))).all()
            remove_task_worktrees(root, (task_a.id, task_b.id), executions)
    finally:
        with database.session() as session:
            executions = session.scalars(select(Execution).where(Execution.task_id.in_([task_a.id, task_b.id]))).all()
            remove_task_worktrees(root, (task_a.id, task_b.id), executions)
        clean_test_tables(database)
        database.dispose()


def test_postgres_guarded_failure_stops_before_dependent_task(tmp_path: Path) -> None:
    database = integration_database()
    clean_test_tables(database)
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id = make_test_project_id(suffix)
    task_a = proof_task(f"TEST-GUARD-FAIL-A-{suffix}", "guarded_fail_a.txt", "AIENT_GUARDED_FAIL_A=1")
    task_b = proof_task(
        f"TEST-GUARD-FAIL-B-{suffix}",
        "guarded_fail_b.txt",
        "AIENT_GUARDED_FAIL_B=1",
        depends_on=(task_a.id,),
    )
    manifest_tasks = {task_a.id: task_a, task_b.id: task_b}
    root = Path.cwd()

    try:
        seed_project_tasks(database, project_id, task_a, task_b)
        with database.session() as session:
            result = guarded_runner(
                bounded_runner=make_runner(
                    manifest_tasks=manifest_tasks,
                    command=writer_command("guarded_fail_a.txt", "wrong"),
                    worktree_root=tmp_path / "worktrees",
                    max_tasks=2,
                    max_repairs=1,
                )
            ).run(session, GuardedRunConfig(project_id=project_id, max_tasks_per_run=2, max_repairs_per_task=1))

            assert result.stop_reason == "FAILURE_LIMIT"
            assert result.tasks_failed == 1
            assert result.tasks_completed == 0

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
