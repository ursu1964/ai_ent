from __future__ import annotations

from pathlib import Path
from typing import Any, Self

import pytest

from ai_ent.persistence.config import DatabaseSettings, load_database_settings
from ai_ent_product_deployment.config import (
    LocalDeploymentConfigError,
    load_local_deployment_config,
    validate_postgresql_authority,
)
from ai_ent_product_ui.services import hash_password


def deployment_env() -> dict[str, str]:
    return {
        "AIENT_USE_EXISTING_POSTGRES": "true",
        "AIENT_PRODUCT_BIND_HOST": "127.0.0.1",
        "AIENT_PRODUCT_PORT": "8000",
        "AIENT_DB_HOST": "127.0.0.1",
        "AIENT_DB_PORT": "5432",
        "AIENT_DB_NAME": "ai_ent",
        "AIENT_DB_USER": "ai_ent",
        "AIENT_DB_PASSWORD": "private-db-password",
        "AIENT_OPERATOR_USERNAME": "operator",
        "AIENT_OPERATOR_DISPLAY_NAME": "Local Operator",
        "AIENT_OPERATOR_PASSWORD_HASH": hash_password("operator-password"),
        "AIENT_OPERATOR_ROLES": "authenticated_user,project_operator",
    }


def test_local_deployment_config_uses_existing_postgres_and_loopback() -> None:
    config = load_local_deployment_config(None, environ=deployment_env())

    assert config.use_existing_postgres is True
    assert config.bind_host == "127.0.0.1"
    assert config.port == 8000
    assert config.database.host == "127.0.0.1"
    assert config.operator.username == "operator"
    summary = config.safe_summary()
    assert summary["network_scope"] == "loopback"
    assert "private-db-password" not in str(summary)
    assert "operator-password" not in str(summary)


def test_local_deployment_config_rejects_wildcard_bind() -> None:
    values = deployment_env()
    values["AIENT_PRODUCT_BIND_HOST"] = "0.0.0.0"

    with pytest.raises(LocalDeploymentConfigError, match="loopback"):
        load_local_deployment_config(None, environ=values)


def test_local_deployment_config_requires_existing_postgres_mode() -> None:
    values = deployment_env()
    values["AIENT_USE_EXISTING_POSTGRES"] = "false"

    with pytest.raises(LocalDeploymentConfigError, match="USE_EXISTING_POSTGRES"):
        load_local_deployment_config(None, environ=values)


def test_local_deployment_config_requires_operator_bootstrap() -> None:
    values = deployment_env()
    values.pop("AIENT_OPERATOR_PASSWORD_HASH")

    with pytest.raises(LocalDeploymentConfigError, match="AIENT_OPERATOR_PASSWORD_HASH"):
        load_local_deployment_config(None, environ=values)


def test_operator_environment_template_is_private_boundary_documentation() -> None:
    template = Path("deployment/local-docker/operator-env.template").read_text(encoding="utf-8")

    assert "AIENT_USE_EXISTING_POSTGRES=true" in template
    assert "AIENT_PRODUCT_BIND_HOST=127.0.0.1" in template
    assert "AIENT_OPERATOR_PASSWORD_HASH=CHANGE_ME" in template
    assert "operator-password" not in template


def test_postgresql_authority_validation_passes_for_expected_database_and_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine(
        {
            "SELECT 1": 1,
            "SELECT current_database()": "ai_ent",
            "SELECT version_num FROM alembic_version": "0020",
        }
    )
    monkeypatch.setattr("ai_ent_product_deployment.config.create_engine", lambda *args, **kwargs: engine)

    result = validate_postgresql_authority(_database_settings(), expected_head="0020")

    assert result.current_revision == "0020"
    assert result.expected_head == "0020"
    assert result.safe_summary()["select_1"] == "pass"
    assert engine.disposed is True


def test_postgresql_authority_validation_rejects_wrong_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine(
        {
            "SELECT 1": 1,
            "SELECT current_database()": "wrong_database",
            "SELECT version_num FROM alembic_version": "0020",
        }
    )
    monkeypatch.setattr("ai_ent_product_deployment.config.create_engine", lambda *args, **kwargs: engine)

    with pytest.raises(LocalDeploymentConfigError, match="does not match configured database"):
        validate_postgresql_authority(_database_settings(), expected_head="0020")


def test_postgresql_authority_validation_rejects_stale_alembic_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine(
        {
            "SELECT 1": 1,
            "SELECT current_database()": "ai_ent",
            "SELECT version_num FROM alembic_version": "0019",
        }
    )
    monkeypatch.setattr("ai_ent_product_deployment.config.create_engine", lambda *args, **kwargs: engine)

    with pytest.raises(LocalDeploymentConfigError, match="migration revision mismatch"):
        validate_postgresql_authority(_database_settings(), expected_head="0020")


def test_postgresql_authority_validation_rejects_missing_alembic_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine(
        {
            "SELECT 1": 1,
            "SELECT current_database()": "ai_ent",
            "SELECT version_num FROM alembic_version": RuntimeError("missing table"),
        }
    )
    monkeypatch.setattr("ai_ent_product_deployment.config.create_engine", lambda *args, **kwargs: engine)

    with pytest.raises(LocalDeploymentConfigError, match="migration revision probe failed"):
        validate_postgresql_authority(_database_settings(), expected_head="0020")


def test_postgresql_authority_validation_rejects_select_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine({"SELECT 1": RuntimeError("probe unavailable")})
    monkeypatch.setattr("ai_ent_product_deployment.config.create_engine", lambda *args, **kwargs: engine)

    with pytest.raises(LocalDeploymentConfigError, match="SELECT 1 probe failed"):
        validate_postgresql_authority(_database_settings(), expected_head="0020")


def test_postgresql_authority_validation_rejects_unreachable_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine({}, connect_error=RuntimeError("connection refused"))
    monkeypatch.setattr("ai_ent_product_deployment.config.create_engine", lambda *args, **kwargs: engine)

    with pytest.raises(LocalDeploymentConfigError, match="connection failed"):
        validate_postgresql_authority(_database_settings(), expected_head="0020")
    assert "private-db-password" not in str(engine.connect_error)


def test_postgresql_authority_validation_rejects_invalid_authentication_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine({}, connect_error=RuntimeError("password authentication failed"))
    monkeypatch.setattr("ai_ent_product_deployment.config.create_engine", lambda *args, **kwargs: engine)

    with pytest.raises(LocalDeploymentConfigError) as exc_info:
        validate_postgresql_authority(_database_settings(), expected_head="0020")

    message = str(exc_info.value)
    assert "authentication failed" in message
    assert "private-db-password" not in message


def test_postgresql_authority_validation_against_existing_authority() -> None:
    settings = load_database_settings(Path(".env"))

    result = validate_postgresql_authority(settings)

    assert result.selected_database == settings.name
    assert result.current_revision == result.expected_head


def _database_settings() -> DatabaseSettings:
    return DatabaseSettings(
        host="127.0.0.1",
        port=5432,
        name="ai_ent",
        user="ai_ent",
        password="private-db-password",
    )


class FakeEngine:
    def __init__(
        self,
        responses: dict[str, object],
        *,
        connect_error: Exception | None = None,
    ) -> None:
        self.responses = responses
        self.connect_error = connect_error
        self.disposed = False

    def connect(self) -> FakeConnection:
        if self.connect_error is not None:
            raise self.connect_error
        return FakeConnection(self.responses)

    def dispose(self) -> None:
        self.disposed = True


class FakeConnection:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: Any) -> FakeResult:
        key = str(statement)
        response = self.responses.get(key, RuntimeError(f"unexpected query: {key}"))
        if isinstance(response, Exception):
            raise response
        return FakeResult(response)


class FakeResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one(self) -> object:
        return self.value
