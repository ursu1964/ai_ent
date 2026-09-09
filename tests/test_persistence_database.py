from __future__ import annotations

from sqlalchemy import Integer, String, create_engine, select
from sqlalchemy.orm import Mapped, mapped_column

from ai_ent.persistence.config import DatabaseSettings
from ai_ent.persistence.database import Database, PersistenceHealth, PersistenceUnavailable
from ai_ent.persistence.models import Base


class ExampleModel(Base):
    __tablename__ = "example_model"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(40), nullable=False)


def settings() -> DatabaseSettings:
    return DatabaseSettings.from_mapping(
        {
            "AIENT_DB_HOST": "localhost",
            "AIENT_DB_PORT": "5432",
            "AIENT_DB_NAME": "ai_ent",
            "AIENT_DB_USER": "ai_ent",
            "AIENT_DB_PASSWORD": "secret-password",
        }
    )


def sqlite_database() -> Database:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Database(settings(), engine=engine)


def test_engine_and_session_factory_construction() -> None:
    database = sqlite_database()

    assert database.engine is not None
    assert database.session_factory is not None

    database.dispose()


def test_transaction_commit() -> None:
    database = sqlite_database()

    with database.session() as session:
        session.add(ExampleModel(name="committed"))

    with database.session() as session:
        names = session.scalars(select(ExampleModel.name)).all()

    assert names == ["committed"]


def test_transaction_rollback_on_exception() -> None:
    database = sqlite_database()

    try:
        with database.session() as session:
            session.add(ExampleModel(name="rolled-back"))
            raise RuntimeError("stop")
    except RuntimeError:
        pass

    with database.session() as session:
        names = session.scalars(select(ExampleModel.name)).all()

    assert names == []


def test_resource_disposal() -> None:
    database = sqlite_database()

    database.dispose()

    assert database.engine.pool is not None


def test_health_check_success() -> None:
    database = sqlite_database()

    health = database.health()

    assert health == PersistenceHealth(True, "database connection healthy")


def test_health_check_failure_classification() -> None:
    database = Database(settings())
    database.engine.dispose()
    database.engine = create_engine("postgresql+psycopg://bad:bad@127.0.0.1:1/bad", future=True)

    health = database.health()

    assert not health.ok
    assert "bad" not in health.detail
    assert "password" not in health.detail.lower()


def test_require_healthy_raises_sanitized_error() -> None:
    database = Database(settings())
    database.engine.dispose()
    database.engine = create_engine("postgresql+psycopg://bad:bad@127.0.0.1:1/bad", future=True)

    try:
        database.require_healthy()
    except PersistenceUnavailable as exc:
        assert "bad" not in str(exc)
    else:
        raise AssertionError("PersistenceUnavailable was not raised")
