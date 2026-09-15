from __future__ import annotations

from typing import Any

from ai_ent.configuration import REDACTED, build_configuration_view
from ai_ent.runtime_operations import mandatory_verification_commands


def test_configuration_view_exposes_structured_non_secret_settings() -> None:
    view = build_configuration_view(
        {
            "AIENT_PROVIDER_ID": "openai",
            "AIENT_PROVIDER_BASE_URL": "https://api.example.test/v1",
            "AIENT_MODEL_PROFILE": "implementation",
            "AIENT_MODEL_ID": "model-for-tests",
            "AIENT_MODEL_VERSION": "2026-09-14",
            "AIENT_ADMIN_EMAIL": "admin@example.test",
            "AIENT_CODEX_TIMEOUT_SECONDS": "120",
            "AIENT_DB_HOST": "localhost",
        }
    )

    payload = view.as_dict()

    assert payload["boundary"]["decision_dependency"] == "DECISION_REQUIRED:PRD-DEC-003"
    assert payload["boundary"]["secrets_backend"] == "private_local_environment"
    assert payload["control_plane_authority"] == "postgresql"
    assert payload["human_gate_policy"] == "explicit_operator_confirmation_required"
    assert payload["independent_verification_required"] is True
    assert payload["verification_commands"] == list(mandatory_verification_commands())
    assert _setting(payload, "provider_settings", "AIENT_PROVIDER_ID") == {
        "section": "provider",
        "name": "AIENT_PROVIDER_ID",
        "configured": True,
        "value": "openai",
        "redacted": False,
        "source": "environment",
    }
    assert _setting(payload, "model_settings", "AIENT_MODEL_ID")["value"] == "model-for-tests"
    assert _setting(payload, "admin_settings", "AIENT_ADMIN_EMAIL")["value"] == "admin@example.test"
    assert _setting(payload, "database_settings", "AIENT_DB_HOST")["value"] == "localhost"


def test_configuration_view_redacts_provider_model_admin_and_database_secrets() -> None:
    secrets = {
        "AIENT_PROVIDER_API_KEY": "provider-secret-value",
        "AIENT_PROVIDER_TOKEN": "provider-token-value",
        "AIENT_MODEL_API_KEY": "model-secret-value",
        "AIENT_ADMIN_PASSWORD": "admin-password-value",
        "AIENT_SESSION_SECRET": "session-secret-value",
        "AIENT_DB_PASSWORD": "database-password-value",
    }

    payload = build_configuration_view(secrets).as_dict()

    for section_name, setting_name in (
        ("provider_settings", "AIENT_PROVIDER_API_KEY"),
        ("provider_settings", "AIENT_PROVIDER_TOKEN"),
        ("model_settings", "AIENT_MODEL_API_KEY"),
        ("admin_settings", "AIENT_ADMIN_PASSWORD"),
        ("admin_settings", "AIENT_SESSION_SECRET"),
        ("database_settings", "AIENT_DB_PASSWORD"),
    ):
        setting = _setting(payload, section_name, setting_name)
        assert setting["configured"] is True
        assert setting["redacted"] is True
        assert setting["value"] == REDACTED

    flattened = tuple(_flatten(payload))
    for secret in secrets.values():
        assert secret not in flattened
    assert REDACTED in flattened


def test_configuration_view_redacts_secret_bearing_values_for_non_secret_keys() -> None:
    payload = build_configuration_view(
        {
            "AIENT_PROVIDER_BASE_URL": (
                "https://user:pass@example.test/v1?api_key=secret-query-value"
            ),
            "AIENT_CODEX_COMMAND": "codex exec --api-key secret-command-value -",
        }
    ).as_dict()

    provider_url = _setting(payload, "provider_settings", "AIENT_PROVIDER_BASE_URL")
    codex_command = _setting(payload, "runtime_settings", "AIENT_CODEX_COMMAND")

    assert provider_url["redacted"] is True
    assert provider_url["value"] == REDACTED
    assert codex_command["redacted"] is True
    assert codex_command["value"] == REDACTED
    flattened = tuple(_flatten(payload))
    assert "secret-query-value" not in flattened
    assert "secret-command-value" not in flattened


def test_configuration_view_marks_missing_settings_without_placeholder_values() -> None:
    payload = build_configuration_view({}).as_dict()

    for section_name in (
        "provider_settings",
        "model_settings",
        "admin_settings",
        "runtime_settings",
        "database_settings",
    ):
        for setting in payload[section_name]:
            assert setting["configured"] is False
            assert setting["value"] is None
            assert setting["redacted"] is False


def _setting(payload: dict[str, Any], section_name: str, setting_name: str) -> dict[str, Any]:
    for setting in payload[section_name]:
        if setting["name"] == setting_name:
            return setting
    raise AssertionError(f"{setting_name} not found in {section_name}")


def _flatten(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _flatten(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in _flatten(nested)]
    if value is None:
        return []
    return [str(value)]
