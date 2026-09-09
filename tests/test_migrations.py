from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config

from ai_ent.persistence.config import DatabaseConfigError
from ai_ent.persistence.migrations import build_alembic_config
from ai_ent.persistence.models import Base


def write_env(path: Path, password: str = "secret-password") -> None:
    path.write_text(
        "\n".join(
            [
                "AIENT_DB_HOST=localhost",
                "AIENT_DB_PORT=5432",
                "AIENT_DB_NAME=ai_ent",
                "AIENT_DB_USER=ai_ent",
                f"AIENT_DB_PASSWORD={password}",
            ]
        ),
        encoding="utf-8",
    )


def test_alembic_config_construction(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    write_env(env_file)

    config = build_alembic_config(Path("alembic.ini"), env_file)

    assert isinstance(config, Config)
    assert config.get_main_option("script_location") == "migrations"
    assert config.attributes["aient_env_file"] == env_file
    url = config.get_main_option("sqlalchemy.url")
    assert url is not None
    assert "secret-password" not in url
    assert "***" in url


def test_metadata_linkage() -> None:
    assert Base.metadata is not None
    assert Base.metadata.tables == {}


def test_migration_environment_imports_cleanly() -> None:
    spec = importlib.util.spec_from_file_location("aient_alembic_env", "migrations/env.py")

    assert spec is not None
    assert spec.loader is not None


def test_missing_db_config_fails_safely(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    write_env(env_file, password="CHANGE_ME")

    try:
        build_alembic_config(Path("alembic.ini"), env_file)
    except DatabaseConfigError as exc:
        assert "AIENT_DB_PASSWORD" in str(exc)
        assert "CHANGE_ME" not in str(exc)
    else:
        raise AssertionError("DatabaseConfigError was not raised")
