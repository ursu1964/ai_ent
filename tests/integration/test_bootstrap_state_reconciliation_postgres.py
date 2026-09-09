from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from alembic import command
from sqlalchemy import delete

from ai_ent.bootstrap.state_reconciliation import BootstrapStateReconciler
from ai_ent.persistence.config import DatabaseConfigError, load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.persistence.migrations import build_alembic_config
from ai_ent.persistence.models import (
    BootstrapCheckpoint,
    BootstrapRun,
    BootstrapStateAuthority,
    Checkpoint,
    Execution,
    Project,
    Task,
    TaskDependency,
    TaskLease,
)


def integration_database() -> Database:
    try:
        config = build_alembic_config(Path("alembic.ini"), Path(".env"))
        settings = load_database_settings(Path(".env"))
    except DatabaseConfigError as exc:
        raise unittest.SkipTest(f"bootstrap state reconciliation integration blocked: {exc}") from exc
    command.upgrade(config, "head")
    return Database(settings)


def clean_tables(database: Database) -> None:
    with database.session() as session:
        session.execute(delete(BootstrapStateAuthority))
        session.execute(delete(BootstrapCheckpoint))
        session.execute(delete(BootstrapRun))
        session.execute(delete(TaskLease))
        session.execute(delete(Checkpoint))
        session.execute(delete(Execution))
        session.execute(delete(TaskDependency))
        session.execute(delete(Task))
        session.execute(delete(Project))


def write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_postgres_current_history_migrates_reloads_and_ignores_local_as_authority(tmp_path: Path) -> None:
    database = integration_database()
    clean_tables(database)
    state_file = tmp_path / ".bootstrap" / "state.json"
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    write_state(
        state_file,
        {
            "run_id": run_id,
            "completed_tasks": ["TASK-0001", "TASK-0002", "TASK-0003"],
            "blocked_tasks": {},
            "last_completed_task": "TASK-0003",
        },
    )
    reconciler = BootstrapStateReconciler(state_file=state_file, database=database, run_id=run_id)

    try:
        migrated = reconciler.migrate()
        assert migrated.outcome == "IN_SYNC"
        assert migrated.backup_path is not None
        assert migrated.backup_path.exists()

        reloaded = BootstrapStateReconciler(state_file=state_file, database=database, run_id=run_id)
        authoritative = reloaded.load_authoritative_state()
        assert authoritative is not None
        assert authoritative["state_backend"] == "postgresql"
        assert authoritative["completed_tasks"] == ["TASK-0001", "TASK-0002", "TASK-0003"]

        write_state(
            state_file,
            {
                "run_id": run_id,
                "completed_tasks": ["TASK-0001"],
                "blocked_tasks": {},
                "last_completed_task": "TASK-0001",
            },
        )
        assert reloaded.load_authoritative_state()["completed_tasks"] == [  # type: ignore[index]
            "TASK-0001",
            "TASK-0002",
            "TASK-0003",
        ]
    finally:
        clean_tables(database)
        database.dispose()
