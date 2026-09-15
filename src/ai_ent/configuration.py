from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from ai_ent.runtime_operations import mandatory_verification_commands

REDACTED = "[REDACTED]"
SECRET_FIELD_MARKERS = ("secret", "token", "password", "credential", "private_key", "api_key")

ConfigurationSection = Literal["provider", "model", "admin", "runtime", "database"]


@dataclass(frozen=True)
class ConfigurationSettingView:
    section: ConfigurationSection
    name: str
    configured: bool
    value: str | None
    redacted: bool
    source: str = "environment"

    def as_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "name": self.name,
            "configured": self.configured,
            "value": self.value,
            "redacted": self.redacted,
            "source": self.source,
        }


@dataclass(frozen=True)
class SecretBoundaryView:
    decision_dependency: str = "DECISION_REQUIRED:PRD-DEC-003"
    secrets_backend: str = "private_local_environment"
    production_secrets_backend_configured: bool = False
    internet_exposure_allowed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_dependency": self.decision_dependency,
            "secrets_backend": self.secrets_backend,
            "production_secrets_backend_configured": self.production_secrets_backend_configured,
            "internet_exposure_allowed": self.internet_exposure_allowed,
        }


@dataclass(frozen=True)
class ConfigurationView:
    boundary: SecretBoundaryView
    provider_settings: tuple[ConfigurationSettingView, ...]
    model_settings: tuple[ConfigurationSettingView, ...]
    admin_settings: tuple[ConfigurationSettingView, ...]
    runtime_settings: tuple[ConfigurationSettingView, ...]
    database_settings: tuple[ConfigurationSettingView, ...]
    control_plane_authority: str = "postgresql"
    human_gate_policy: str = "explicit_operator_confirmation_required"
    independent_verification_required: bool = True
    verification_commands: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "boundary": self.boundary.as_dict(),
            "control_plane_authority": self.control_plane_authority,
            "human_gate_policy": self.human_gate_policy,
            "independent_verification_required": self.independent_verification_required,
            "verification_commands": list(self.verification_commands),
            "provider_settings": [setting.as_dict() for setting in self.provider_settings],
            "model_settings": [setting.as_dict() for setting in self.model_settings],
            "admin_settings": [setting.as_dict() for setting in self.admin_settings],
            "runtime_settings": [setting.as_dict() for setting in self.runtime_settings],
            "database_settings": [setting.as_dict() for setting in self.database_settings],
        }


@dataclass(frozen=True)
class _SettingDefinition:
    section: ConfigurationSection
    name: str
    secret: bool = False


PROVIDER_SETTING_DEFINITIONS: tuple[_SettingDefinition, ...] = (
    _SettingDefinition("provider", "AIENT_PROVIDER_ID"),
    _SettingDefinition("provider", "AIENT_PROVIDER_BASE_URL"),
    _SettingDefinition("provider", "AIENT_PROVIDER_API_KEY", secret=True),
    _SettingDefinition("provider", "AIENT_PROVIDER_TOKEN", secret=True),
)
MODEL_SETTING_DEFINITIONS: tuple[_SettingDefinition, ...] = (
    _SettingDefinition("model", "AIENT_MODEL_PROFILE"),
    _SettingDefinition("model", "AIENT_MODEL_ID"),
    _SettingDefinition("model", "AIENT_MODEL_VERSION"),
    _SettingDefinition("model", "AIENT_MODEL_API_KEY", secret=True),
)
ADMIN_SETTING_DEFINITIONS: tuple[_SettingDefinition, ...] = (
    _SettingDefinition("admin", "AIENT_ADMIN_EMAIL"),
    _SettingDefinition("admin", "AIENT_ADMIN_USER"),
    _SettingDefinition("admin", "AIENT_ADMIN_PASSWORD", secret=True),
    _SettingDefinition("admin", "AIENT_SESSION_SECRET", secret=True),
)
RUNTIME_SETTING_DEFINITIONS: tuple[_SettingDefinition, ...] = (
    _SettingDefinition("runtime", "AIENT_CODEX_COMMAND"),
    _SettingDefinition("runtime", "AIENT_CODEX_TIMEOUT_SECONDS"),
)
DATABASE_SETTING_DEFINITIONS: tuple[_SettingDefinition, ...] = (
    _SettingDefinition("database", "AIENT_DB_HOST"),
    _SettingDefinition("database", "AIENT_DB_PORT"),
    _SettingDefinition("database", "AIENT_DB_NAME"),
    _SettingDefinition("database", "AIENT_DB_USER"),
    _SettingDefinition("database", "AIENT_DB_PASSWORD", secret=True),
)


def build_configuration_view(values: Mapping[str, str] | None = None) -> ConfigurationView:
    source = values if values is not None else os.environ
    return ConfigurationView(
        boundary=SecretBoundaryView(),
        provider_settings=_settings(source, PROVIDER_SETTING_DEFINITIONS),
        model_settings=_settings(source, MODEL_SETTING_DEFINITIONS),
        admin_settings=_settings(source, ADMIN_SETTING_DEFINITIONS),
        runtime_settings=_settings(source, RUNTIME_SETTING_DEFINITIONS),
        database_settings=_settings(source, DATABASE_SETTING_DEFINITIONS),
        verification_commands=mandatory_verification_commands(),
    )


def _settings(
    values: Mapping[str, str],
    definitions: tuple[_SettingDefinition, ...],
) -> tuple[ConfigurationSettingView, ...]:
    return tuple(_setting(values, definition) for definition in definitions)


def _setting(values: Mapping[str, str], definition: _SettingDefinition) -> ConfigurationSettingView:
    raw_value = values.get(definition.name)
    configured = raw_value not in {None, ""}
    redacted = configured and _requires_redaction(
        definition.name,
        raw_value or "",
        definition.secret,
    )
    return ConfigurationSettingView(
        section=definition.section,
        name=definition.name,
        configured=configured,
        value=_public_value(raw_value, redacted),
        redacted=redacted,
    )


def _public_value(value: str | None, redacted: bool) -> str | None:
    if value is None or value == "":
        return None
    if redacted:
        return REDACTED
    return _redact_url(value)


def _requires_redaction(name: str, value: str, force: bool) -> bool:
    normalized_name = name.lower().replace("-", "_")
    if force or any(marker in normalized_name for marker in SECRET_FIELD_MARKERS):
        return True
    normalized_value = value.lower().replace("-", "_")
    return any(
        f"{marker}=" in normalized_value or f"{marker} " in normalized_value
        for marker in SECRET_FIELD_MARKERS
    ) or _url_has_secret_material(value)


def _redact_url(value: str) -> str:
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return value
    if "@" not in parts.netloc:
        return value
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit(
        (parts.scheme, f"{REDACTED}@{host}", parts.path, parts.query, parts.fragment)
    )


def _url_has_secret_material(value: str) -> bool:
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return False
    if "@" in parts.netloc:
        return True
    normalized = f"{parts.query} {parts.fragment}".lower().replace("-", "_")
    return any(marker in normalized for marker in SECRET_FIELD_MARKERS)
