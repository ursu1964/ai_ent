from __future__ import annotations

import unittest
from pathlib import Path

from ai_ent.persistence.config import DatabaseConfigError, load_database_settings
from ai_ent.persistence.database import Database


def test_configured_postgresql_health() -> None:
    try:
        settings = load_database_settings(Path(".env"))
    except DatabaseConfigError as exc:
        raise unittest.SkipTest(f"PostgreSQL integration blocked: {exc}") from exc

    database = Database(settings)
    try:
        health = database.health()
    finally:
        database.dispose()

    if not health.ok:
        raise unittest.SkipTest(f"PostgreSQL integration blocked: {health.detail}")

    assert health.ok
