from __future__ import annotations

from pathlib import Path

from ai_ent.persistence.config import DatabaseConfigError, DatabaseSettings, load_database_settings


def valid_values() -> dict[str, str]:
    return {
        "AIENT_DB_HOST": "localhost",
        "AIENT_DB_PORT": "5432",
        "AIENT_DB_NAME": "ai_ent",
        "AIENT_DB_USER": "ai_ent",
        "AIENT_DB_PASSWORD": "secret-password",
    }


def test_valid_settings_parsing() -> None:
    settings = DatabaseSettings.from_mapping(valid_values())

    assert settings.host == "localhost"
    assert settings.port == 5432
    assert settings.name == "ai_ent"
    assert settings.driver == "postgresql+psycopg"
    assert settings.safe_url == "postgresql+psycopg://ai_ent:***@localhost:5432/ai_ent"


def test_missing_db_configuration_is_rejected() -> None:
    values = valid_values()
    values["AIENT_DB_PASSWORD"] = "CHANGE_ME"

    try:
        DatabaseSettings.from_mapping(values)
    except DatabaseConfigError as exc:
        assert "AIENT_DB_PASSWORD" in str(exc)
    else:
        raise AssertionError("DatabaseConfigError was not raised")


def test_invalid_port_is_rejected() -> None:
    values = valid_values()
    values["AIENT_DB_PORT"] = "not-a-port"

    try:
        DatabaseSettings.from_mapping(values)
    except DatabaseConfigError as exc:
        assert "AIENT_DB_PORT" in str(exc)
    else:
        raise AssertionError("DatabaseConfigError was not raised")


def test_load_database_settings_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(f"{key}={value}" for key, value in valid_values().items()),
        encoding="utf-8",
    )

    settings = load_database_settings(env_file)

    assert settings.user == "ai_ent"
    assert settings.password == "secret-password"


def test_settings_repr_does_not_leak_password() -> None:
    settings = DatabaseSettings.from_mapping(valid_values())

    rendered = repr(settings)

    assert "secret-password" not in rendered
    assert "[REDACTED]" in rendered
