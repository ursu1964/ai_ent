from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PACKAGE = Path("deployment/local-docker/portability.yaml")
DEFAULT_COMPOSE = Path("deployment/local-docker/compose.yaml")
DEFAULT_MANIFEST_ROOT = Path("manifest/project/ai-ent")
PACKAGE_ID = "DEP-PKG-LOCAL-DOCKER-001"
REQUIRED_DECISIONS = frozenset({"PRD-DEC-002", "PRD-DEC-003"})
REQUIRED_VERIFICATION_COMMANDS = (
    "/home/user/projects/ai_ent/aient/bin/python -m pytest -q",
    "/home/user/projects/ai_ent/aient/bin/python -m ruff check .",
    "/home/user/projects/ai_ent/aient/bin/python -m pyright",
)
REQUIRED_PACKAGE_FILE_PATHS = (
    "deployment/local-docker/README.md",
    "deployment/local-docker/Dockerfile",
    "deployment/local-docker/compose.yaml",
    "deployment/local-docker/portability.yaml",
)


@dataclass(frozen=True)
class PortabilityValidationResult:
    errors: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def validate_deployment_portability(
    package_path: Path = DEFAULT_PACKAGE,
    compose_path: Path = DEFAULT_COMPOSE,
    manifest_root: Path = DEFAULT_MANIFEST_ROOT,
) -> PortabilityValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    package = _load_yaml_mapping(package_path, errors)
    compose = _load_yaml_mapping(compose_path, errors)
    project = _load_yaml_mapping(manifest_root / "project.yaml", errors)
    deployment = _load_yaml_mapping(manifest_root / "deployment.yaml", errors)
    architecture = _load_yaml_mapping(manifest_root / "architecture" / "deployment.yaml", errors)
    operations = _load_yaml_mapping(manifest_root / "operations.yaml", errors)

    if errors:
        return PortabilityValidationResult(tuple(errors), tuple(warnings))

    _validate_package(package, errors)
    _validate_package_inventory(package, errors)
    _validate_compose(compose, errors)
    _validate_manifest_boundaries(project, deployment, architecture, operations, errors)
    _validate_no_embedded_secret_values(
        {
            "package": package,
            "compose": compose,
            "deployment": deployment,
            "architecture": architecture,
            "operations": operations,
        },
        errors,
    )
    return PortabilityValidationResult(tuple(errors), tuple(warnings))


def _load_yaml_mapping(path: Path, errors: list[str]) -> dict[str, Any]:
    if not path.exists():
        errors.append(f"missing required file: {path}")
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        errors.append(f"{path} must contain a YAML mapping")
        return {}
    return loaded


def _validate_package(package_document: Mapping[str, Any], errors: list[str]) -> None:
    package = _mapping(package_document.get("package"))
    if package.get("id") != PACKAGE_ID:
        errors.append(f"package.id must be {PACKAGE_ID}")
    if package.get("target") != "local_docker_host":
        errors.append("package.target must be local_docker_host")
    if package.get("classification") != "NORMATIVE":
        errors.append("package.classification must be NORMATIVE")

    decisions = {str(item) for item in _sequence(package_document.get("decision_dependencies"))}
    missing_decisions = sorted(REQUIRED_DECISIONS - decisions)
    if missing_decisions:
        errors.append("missing decision dependencies: " + ", ".join(missing_decisions))

    authority = _mapping(package_document.get("authority"))
    if authority.get("control_plane_backend") != "postgresql":
        errors.append("authority.control_plane_backend must remain postgresql")
    if authority.get("operator_invocation_required") is not True:
        errors.append("authority.operator_invocation_required must be true")

    human_gates = _sequence(package_document.get("human_gates"))
    if len(human_gates) < 3:
        errors.append("human_gates must include guarded run, migration/restore, and cloud target gates")
    for gate in human_gates:
        gate_mapping = _mapping(gate)
        if gate_mapping.get("required") is not True:
            errors.append(f"human gate {gate_mapping.get('id', '<unknown>')} must be required")

    boundaries = [_mapping(item) for item in _sequence(package_document.get("migration_boundaries"))]
    if not any(boundary.get("future_cloud_status") == "deferred" for boundary in boundaries):
        errors.append("migration_boundaries must defer future cloud targets")
    if not any(boundary.get("future_cloud_status") == "decision_required" for boundary in boundaries):
        errors.append("migration_boundaries must keep future cloud targets decision-gated")

    verification = _mapping(package_document.get("verification"))
    if verification.get("independent_verification_mandatory") is not True:
        errors.append("verification.independent_verification_mandatory must be true")
    commands = tuple(str(command) for command in _sequence(verification.get("commands")))
    for command in REQUIRED_VERIFICATION_COMMANDS:
        if command not in commands:
            errors.append(f"missing required verification command: {command}")

    secret_policy = _mapping(package_document.get("secret_policy"))
    if secret_policy.get("embedded_secret_values_allowed") is not False:
        errors.append("secret_policy.embedded_secret_values_allowed must be false")


def _validate_package_inventory(package_document: Mapping[str, Any], errors: list[str]) -> None:
    entries = [_mapping(item) for item in _sequence(package_document.get("package_files"))]
    paths = tuple(str(entry.get("path", "")) for entry in entries)
    path_set = set(paths)

    if len(paths) != len(path_set):
        errors.append("package_files must not contain duplicate paths")

    missing_paths = sorted(set(REQUIRED_PACKAGE_FILE_PATHS) - path_set)
    if missing_paths:
        errors.append("package_files missing required paths: " + ", ".join(missing_paths))

    extra_paths = sorted(path for path in path_set if path not in REQUIRED_PACKAGE_FILE_PATHS)
    if extra_paths:
        errors.append(
            "package_files contains paths outside the local Docker package: "
            + ", ".join(extra_paths)
        )

    for entry in entries:
        raw_path = str(entry.get("path", ""))
        path = Path(raw_path)
        if path.is_absolute() or ".." in path.parts:
            errors.append(f"package file path must be repository-relative: {raw_path}")
            continue
        if path.name == ".env" or path.name.startswith(".env."):
            errors.append(f"package file path must not include environment files: {raw_path}")
        if entry.get("classification") != "NORMATIVE":
            errors.append(f"package file {raw_path or '<unknown>'} must be classified NORMATIVE")
        if not entry.get("role"):
            errors.append(f"package file {raw_path or '<unknown>'} must declare a role")
        if not raw_path or not path.exists():
            errors.append(f"package file does not exist: {raw_path or '<unknown>'}")


def _validate_compose(compose: Mapping[str, Any], errors: list[str]) -> None:
    services = _mapping(compose.get("services"))
    postgres = _mapping(services.get("postgres"))
    operator = _mapping(services.get("operator"))
    if not postgres:
        errors.append("compose must define a postgres service")
    if not operator:
        errors.append("compose must define an operator service")

    image = str(postgres.get("image", ""))
    if image == "postgres:latest" or not image.startswith("postgres:"):
        errors.append("postgres service must use an explicit postgres image tag")

    volumes = _sequence(postgres.get("volumes"))
    if "postgres-data:/var/lib/postgresql/data" not in {str(volume) for volume in volumes}:
        errors.append("postgres service must persist control-plane state on postgres-data")

    environment = _mapping(operator.get("environment"))
    if environment.get("AIENT_DB_HOST") != "postgres":
        errors.append("operator service must target the local postgres service")
    if "AIENT_DB_PASSWORD" not in environment:
        errors.append("operator service must require AIENT_DB_PASSWORD from the operator environment")

    command = tuple(str(part) for part in _sequence(operator.get("command")))
    if "guarded-run" in command:
        errors.append("operator service default command must not auto-start guarded-run")


def _validate_manifest_boundaries(
    project: Mapping[str, Any],
    deployment: Mapping[str, Any],
    architecture: Mapping[str, Any],
    operations: Mapping[str, Any],
    errors: list[str],
) -> None:
    project_authority = _mapping(project.get("authority"))
    if project_authority.get("state_backend") != "postgresql":
        errors.append("project authority state_backend must remain postgresql")

    deployment_body = _mapping(deployment.get("deployment"))
    package_refs = {str(item) for item in _sequence(deployment_body.get("package_refs"))}
    if PACKAGE_ID not in package_refs:
        errors.append(f"deployment.package_refs must include {PACKAGE_ID}")
    if deployment_body.get("current_mode") != "local_development":
        errors.append("deployment.current_mode must remain local_development")

    local_package = _mapping(deployment_body.get("local_docker_package"))
    if local_package.get("path") != "deployment/local-docker":
        errors.append("deployment.local_docker_package.path must be deployment/local-docker")
    if local_package.get("private_environment_boundary") != "operator_supplied_runtime_values":
        errors.append("deployment local package must preserve the private environment boundary")

    architecture_body = _mapping(architecture.get("deployment"))
    if architecture_body.get("id") == PACKAGE_ID:
        errors.append("architecture deployment id must not duplicate the local package id")
    if architecture_body.get("local_package_ref") != PACKAGE_ID:
        errors.append(f"architecture.deployment.local_package_ref must reference {PACKAGE_ID}")
    if architecture_body.get("database") != "postgresql":
        errors.append("architecture deployment database must remain postgresql")

    operation_ids = {str(_mapping(item).get("id")) for item in _sequence(operations.get("operations"))}
    if "OP-006" not in operation_ids:
        errors.append("operations must include OP-006 deployment portability validation")


def _validate_no_embedded_secret_values(documents: Mapping[str, Any], errors: list[str]) -> None:
    for document_name, document in documents.items():
        for path, value in _walk_sensitive_values(document):
            if not _is_allowed_secret_reference(value):
                errors.append(f"{document_name}.{path} contains an embedded secret-like value")


def _walk_sensitive_values(value: Any, path: str = "") -> tuple[tuple[str, str], ...]:
    matches: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}" if path else key
            if _is_sensitive_key(key) and isinstance(item, str):
                matches.append((child_path, item))
            matches.extend(_walk_sensitive_values(item, child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            child_path = f"{path}[{index}]"
            matches.extend(_walk_sensitive_values(item, child_path))
    return tuple(matches)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(
        marker in normalized
        for marker in (
            "password",
            "token",
            "credential",
            "secret",
            "api_key",
            "access_key",
            "private_key",
        )
    )


def _is_allowed_secret_reference(value: str) -> bool:
    stripped = value.strip()
    return stripped.startswith(("${", "/run/secrets/")) or stripped in {"", "CHANGE_ME"}


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, list | tuple):
        return tuple(value)
    return ()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate local Docker deployment portability.")
    subparsers = parser.add_subparsers(dest="command")
    validate = subparsers.add_parser("validate")
    validate.add_argument("--package", type=Path, default=DEFAULT_PACKAGE)
    validate.add_argument("--compose", type=Path, default=DEFAULT_COMPOSE)
    validate.add_argument("--manifest-root", type=Path, default=DEFAULT_MANIFEST_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command != "validate":
        parser.print_help()
        return 2
    result = validate_deployment_portability(args.package, args.compose, args.manifest_root)
    print(json.dumps(result.as_dict(), sort_keys=True, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
