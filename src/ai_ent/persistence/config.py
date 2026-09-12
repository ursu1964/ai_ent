from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import URL

PLACEHOLDERS = {"", "CHANGE_ME"}


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    name: str
    user: str
    password: str
    driver: str = "postgresql+psycopg"

    @classmethod
    def from_mapping(cls, values: dict[str, str]) -> DatabaseSettings:
        missing = [
            key
            for key in (
                "AIENT_DB_HOST",
                "AIENT_DB_PORT",
                "AIENT_DB_NAME",
                "AIENT_DB_USER",
                "AIENT_DB_PASSWORD",
            )
            if values.get(key, "") in PLACEHOLDERS
        ]
        if missing:
            raise DatabaseConfigError(f"missing database configuration: {', '.join(missing)}")
        try:
            port = int(values["AIENT_DB_PORT"])
        except ValueError as exc:
            raise DatabaseConfigError("invalid database configuration: AIENT_DB_PORT") from exc
        if not 1 <= port <= 65535:
            raise DatabaseConfigError("invalid database configuration: AIENT_DB_PORT")
        return cls(
            host=values["AIENT_DB_HOST"],
            port=port,
            name=values["AIENT_DB_NAME"],
            user=values["AIENT_DB_USER"],
            password=values["AIENT_DB_PASSWORD"],
        )

    @property
    def url(self) -> URL:
        return URL.create(
            self.driver,
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.name,
        )

    @property
    def safe_url(self) -> str:
        return self.url.render_as_string(hide_password=True)

    def __repr__(self) -> str:
        return (
            "DatabaseSettings("
            f"host={self.host!r}, port={self.port!r}, name={self.name!r}, "
            f"user={self.user!r}, password='[REDACTED]', driver={self.driver!r})"
        )


class DatabaseConfigError(ValueError):
    """Raised when database configuration is missing or invalid."""


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_database_settings(env_file: Path | None = None) -> DatabaseSettings:
    values = dict(os.environ)
    if env_file:
        values.update(parse_env_file(env_file))
    return DatabaseSettings.from_mapping(values)
