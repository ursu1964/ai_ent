from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError

from ai_ent.persistence.config import DatabaseConfigError, load_database_settings


class MigrationError(RuntimeError):
    """Raised when Alembic cannot be configured or executed safely."""


def build_alembic_config(
    config_path: Path = Path("alembic.ini"),
    env_file: Path = Path(".env"),
) -> Config:
    settings = load_database_settings(env_file)
    config = Config(str(config_path))
    config.attributes["aient_env_file"] = env_file
    config.set_main_option("sqlalchemy.url", settings.safe_url)
    return config


def upgrade_head(config: Config) -> None:
    try:
        command.upgrade(config, "head")
    except (CommandError, DatabaseConfigError) as exc:
        raise MigrationError(_sanitize_migration_error(exc)) from exc


def current_revision(config: Config) -> str:
    try:
        command.current(config)
    except (CommandError, DatabaseConfigError) as exc:
        raise MigrationError(_sanitize_migration_error(exc)) from exc
    return "reported"


def _sanitize_migration_error(exc: BaseException) -> str:
    message = str(exc)
    if "password" in message.lower():
        return "migration failed"
    return message.splitlines()[0] if message else "migration failed"
