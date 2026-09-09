from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.persistence.config import DatabaseSettings


class PersistenceError(RuntimeError):
    """Base persistence error that must not expose credentials."""


class PersistenceUnavailable(PersistenceError):
    """Raised when the configured database cannot be reached."""


@dataclass(frozen=True)
class PersistenceHealth:
    ok: bool
    detail: str


class Database:
    def __init__(self, settings: DatabaseSettings, engine: Engine | None = None) -> None:
        self.settings = settings
        self.engine = engine or create_engine(
            settings.url,
            pool_pre_ping=True,
            future=True,
        )
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
            future=True,
        )

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def health(self) -> PersistenceHealth:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            return PersistenceHealth(False, _sanitize_error(exc))
        return PersistenceHealth(True, "database connection healthy")

    def require_healthy(self) -> None:
        health = self.health()
        if not health.ok:
            raise PersistenceUnavailable(health.detail)

    def dispose(self) -> None:
        self.engine.dispose()


def _sanitize_error(exc: BaseException) -> str:
    message = str(exc)
    if "password" in message.lower():
        return "database connection failed"
    return message.splitlines()[0] if message else "database connection failed"
