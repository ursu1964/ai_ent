from __future__ import annotations

import unittest
from pathlib import Path

from alembic import command
from sqlalchemy import inspect

from ai_ent.persistence.config import DatabaseConfigError, load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.persistence.migrations import build_alembic_config


def test_runtime_schema_migrates_from_0015_to_head() -> None:
    try:
        config = build_alembic_config(Path("alembic.ini"), Path(".env"))
        settings = load_database_settings(Path(".env"))
    except DatabaseConfigError as exc:
        raise unittest.SkipTest(f"runtime schema integration blocked: {exc}") from exc

    command.upgrade(config, "head")
    database = Database(settings)
    try:
        inspector = inspect(database.engine)
        tables = set(inspector.get_table_names())
    finally:
        database.dispose()

    assert {
        "projects",
        "tasks",
        "task_dependencies",
        "executions",
        "checkpoints",
        "task_leases",
        "bootstrap_runs",
        "bootstrap_checkpoints",
        "bootstrap_state_authority",
        "alembic_version",
    }.issubset(tables)
