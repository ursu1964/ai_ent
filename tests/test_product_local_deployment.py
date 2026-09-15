from __future__ import annotations

from pathlib import Path

import pytest

from ai_ent_product_deployment.config import (
    LocalDeploymentConfigError,
    load_local_deployment_config,
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
