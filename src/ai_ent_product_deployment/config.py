from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ai_ent.persistence.config import DatabaseConfigError, DatabaseSettings, parse_env_file

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


class LocalDeploymentConfigError(ValueError):
    """Raised when local deployment configuration is missing or unsafe."""


@dataclass(frozen=True)
class LocalOperatorConfig:
    username: str
    display_name: str
    password_hash: str
    roles: tuple[str, ...]

    def safe_summary(self) -> dict[str, object]:
        return {
            "username": self.username,
            "display_name": self.display_name,
            "roles": list(self.roles),
            "password_hash_configured": True,
        }


@dataclass(frozen=True)
class LocalDeploymentConfig:
    bind_host: str
    port: int
    use_existing_postgres: bool
    database: DatabaseSettings
    operator: LocalOperatorConfig
    private_env_file: Path | None = None

    def safe_summary(self) -> dict[str, object]:
        return {
            "bind_host": self.bind_host,
            "port": self.port,
            "network_scope": "loopback",
            "use_existing_postgres": self.use_existing_postgres,
            "database": {
                "host": self.database.host,
                "port": self.database.port,
                "name": self.database.name,
                "user": self.database.user,
                "password_configured": True,
                "driver": self.database.driver,
            },
            "operator": self.operator.safe_summary(),
            "private_env_file": str(self.private_env_file) if self.private_env_file else None,
        }


def load_local_deployment_config(
    env_file: Path | None = Path(".env"),
    *,
    environ: dict[str, str] | None = None,
) -> LocalDeploymentConfig:
    values = dict(os.environ if environ is None else environ)
    if env_file is not None:
        values.update(parse_env_file(env_file))

    bind_host = values.get("AIENT_PRODUCT_BIND_HOST") or values.get("AIENT_HOST") or DEFAULT_BIND_HOST
    _require_loopback(bind_host)
    port = _parse_port(values.get("AIENT_PRODUCT_PORT") or values.get("AIENT_PORT") or str(DEFAULT_PORT))
    use_existing_postgres = _parse_existing_postgres(values.get("AIENT_USE_EXISTING_POSTGRES", "true"))
    if not use_existing_postgres:
        raise LocalDeploymentConfigError(
            "local deployment currently supports only USE_EXISTING_POSTGRES=true"
        )

    try:
        database = DatabaseSettings.from_mapping(values)
    except DatabaseConfigError as exc:
        raise LocalDeploymentConfigError(str(exc)) from exc

    operator = _operator_config(values)
    return LocalDeploymentConfig(
        bind_host=bind_host,
        port=port,
        use_existing_postgres=use_existing_postgres,
        database=database,
        operator=operator,
        private_env_file=env_file,
    )


def apply_private_environment(env_file: Path | None = Path(".env")) -> None:
    if env_file is None:
        return
    os.environ.update(parse_env_file(env_file))


def _require_loopback(bind_host: str) -> None:
    if bind_host in LOOPBACK_HOSTS:
        return
    if bind_host.startswith("127."):
        return
    raise LocalDeploymentConfigError(
        "AIENT_PRODUCT_BIND_HOST must be loopback for local deployment validation"
    )


def _parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise LocalDeploymentConfigError("AIENT_PRODUCT_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise LocalDeploymentConfigError("AIENT_PRODUCT_PORT must be between 1 and 65535")
    return port


def _parse_existing_postgres(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "use_existing", "use_existing_postgres"}:
        return True
    if normalized in {"0", "false", "no", "managed", "compose"}:
        return False
    raise LocalDeploymentConfigError(
        "AIENT_USE_EXISTING_POSTGRES must be true or false"
    )


def _operator_config(values: dict[str, str]) -> LocalOperatorConfig:
    username = values.get("AIENT_OPERATOR_USERNAME", "").strip()
    password_hash = values.get("AIENT_OPERATOR_PASSWORD_HASH", "").strip()
    display_name = values.get("AIENT_OPERATOR_DISPLAY_NAME", username).strip()
    raw_roles = values.get("AIENT_OPERATOR_ROLES", "authenticated_user,project_operator")
    roles = tuple(role.strip() for role in raw_roles.split(",") if role.strip())

    missing = [
        name
        for name, configured in (
            ("AIENT_OPERATOR_USERNAME", bool(username)),
            ("AIENT_OPERATOR_PASSWORD_HASH", bool(password_hash)),
        )
        if not configured
    ]
    if missing:
        raise LocalDeploymentConfigError(
            "missing local operator configuration: " + ", ".join(missing)
        )
    if not display_name:
        raise LocalDeploymentConfigError("AIENT_OPERATOR_DISPLAY_NAME must not be empty")
    if not roles:
        raise LocalDeploymentConfigError("AIENT_OPERATOR_ROLES must include at least one role")
    if "authenticated_user" not in roles:
        raise LocalDeploymentConfigError(
            "AIENT_OPERATOR_ROLES must include authenticated_user"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", password_hash):
        raise LocalDeploymentConfigError(
            "AIENT_OPERATOR_PASSWORD_HASH must be a sha256 hex digest"
        )
    return LocalOperatorConfig(
        username=username,
        display_name=display_name,
        password_hash=password_hash,
        roles=roles,
    )
