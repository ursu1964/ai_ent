from __future__ import annotations

import unittest
from pathlib import Path

from alembic import command

from ai_ent.persistence.config import DatabaseConfigError
from ai_ent.persistence.migrations import build_alembic_config


def test_alembic_upgrade_and_current_with_configured_postgresql() -> None:
    try:
        config = build_alembic_config(Path("alembic.ini"), Path(".env"))
    except DatabaseConfigError as exc:
        raise unittest.SkipTest(f"Alembic integration blocked: {exc}") from exc

    try:
        command.upgrade(config, "head")
        command.current(config)
    except Exception as exc:
        message = str(exc)
        if "password" in message.lower():
            message = "alembic integration failed"
        raise AssertionError(message) from exc
