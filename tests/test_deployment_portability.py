from __future__ import annotations

from pathlib import Path

import yaml

from ai_ent.project_manifest import write_compiled_project
from scripts.deployment_portability import (
    PACKAGE_ID,
    REQUIRED_VERIFICATION_COMMANDS,
    validate_deployment_portability,
)


def test_local_docker_package_preserves_deployment_boundaries() -> None:
    result = validate_deployment_portability()

    assert result.ok, result.errors


def test_local_docker_package_id_is_unique_across_manifest_documents() -> None:
    manifest_root = Path("manifest/project/ai-ent")
    ids_by_path: dict[str, list[str]] = {}
    for path in sorted(manifest_root.rglob("*.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for item_id in _collect_ids(loaded):
            ids_by_path.setdefault(item_id, []).append(str(path))

    assert ids_by_path.get(PACKAGE_ID) is None


def test_project_manifest_rejects_duplicate_local_package_id_in_source(tmp_path: Path) -> None:
    source = Path("manifest/project/ai-ent")
    target = tmp_path / "manifest"
    _copy_manifest(source, target)
    architecture = target / "architecture" / "deployment.yaml"
    architecture.write_text(
        architecture.read_text(encoding="utf-8").replace(
            "id: DEP-ARCH-001",
            f"id: {PACKAGE_ID}",
        ),
        encoding="utf-8",
    )

    result = validate_deployment_portability(
        package_path=Path("deployment/local-docker/portability.yaml"),
        compose_path=Path("deployment/local-docker/compose.yaml"),
        manifest_root=target,
    )

    assert not result.ok
    assert any("must not duplicate the local package id" in error for error in result.errors)


def test_local_docker_package_deterministic_compile_and_generated_derivative_safety(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AIENT_DB_PASSWORD", "synthetic-marker-secret-value")
    first = write_compiled_project(Path("manifest/project/ai-ent"), tmp_path / "first")
    monkeypatch.setenv("AIENT_DB_PASSWORD", "changed-synthetic-marker-secret-value")
    second = write_compiled_project(Path("manifest/project/ai-ent"), tmp_path / "second")

    assert first.lock.compiled_hash == second.lock.compiled_hash
    assert first.compiled_bytes == second.compiled_bytes
    compiled_text = first.compiled_bytes.decode("utf-8").lower()
    for prohibited in (
        "embedded_secret_values_allowed",
        "synthetic-marker-secret-value",
        "changed-synthetic-marker-secret-value",
        "password",
        "secret",
    ):
        assert prohibited not in compiled_text


def test_portability_contract_keeps_human_gates_and_verification_explicit() -> None:
    portability = yaml.safe_load(Path("deployment/local-docker/portability.yaml").read_text(encoding="utf-8"))

    gates = portability["human_gates"]
    assert {gate["action"] for gate in gates} == {
        "guarded_autonomous_run",
        "migration_or_restore",
        "cloud_target_selection",
    }
    assert all(gate["required"] is True for gate in gates)
    assert portability["verification"]["independent_verification_mandatory"] is True
    assert tuple(portability["verification"]["commands"]) == REQUIRED_VERIFICATION_COMMANDS


def test_compose_uses_operator_supplied_database_secret_only() -> None:
    compose = yaml.safe_load(Path("deployment/local-docker/compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert services["operator"]["command"] == ["python", "scripts/bootstrap.py", "status"]
    operator_password = services["operator"]["environment"]["AIENT_DB_PASSWORD"]
    postgres_password = services["postgres"]["environment"]["POSTGRES_PASSWORD"]
    assert operator_password.startswith("${AIENT_DB_PASSWORD:")
    assert postgres_password.startswith("${AIENT_DB_PASSWORD:")


def test_repeated_portability_validation_is_deterministic() -> None:
    first = validate_deployment_portability().as_dict()
    second = validate_deployment_portability().as_dict()

    assert first == second


def _copy_manifest(source: Path, target: Path) -> None:
    for path in sorted(source.rglob("*.yaml")):
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")


def _collect_ids(value: object) -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, dict):
        raw_id = value.get("id")
        if isinstance(raw_id, str):
            found.append(raw_id)
        for item in value.values():
            found.extend(_collect_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_collect_ids(item))
    return tuple(found)
