from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

MANIFEST_INTAKE_SCHEMA_VERSION = "aepm-0.1"

_APPROVED_STATUS = "approved"
_METADATA_VERSION_FIELDS = (
    "version",
    "schema_version",
    "registry_version",
    "generator_version",
    "compiler_version",
)
_REQUIRED_SECTIONS = (
    "metadata",
    "organization",
    "domain",
    "entities",
    "capabilities",
)
_IMPLEMENTATION_SECTIONS = {
    "api",
    "apis",
    "api_endpoints",
    "components",
    "database",
    "database_schema",
    "databases",
    "frameworks",
    "infrastructure",
    "microservices",
    "orm_models",
    "react_components",
    "source_code",
    "tables",
}
_BUSINESS_LIST_SECTIONS = (
    "objectives",
    "outcomes",
    "users",
    "stakeholders",
    "entities",
    "capabilities",
    "workflows",
    "business_rules",
    "policies",
    "integrations",
    "reporting",
    "security",
    "quality",
    "constraints",
    "deployment_preferences",
)


@dataclass(frozen=True)
class ManifestIntakeResult:
    source_name: str
    source_format: str
    manifest_hash: str | None
    manifest: dict[str, Any] | None
    validation_errors: tuple[str, ...]
    validation_warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.validation_errors

    def as_dict(self) -> dict[str, Any]:
        metadata = self.manifest.get("metadata", {}) if self.manifest else {}
        return {
            "source_name": self.source_name,
            "source_format": self.source_format,
            "manifest_id": metadata.get("id"),
            "manifest_version": metadata.get("version"),
            "schema_version": metadata.get("schema_version"),
            "approval_status": _approval_status(metadata),
            "manifest_hash": self.manifest_hash,
            "ok": self.ok,
            "validation_errors": list(self.validation_errors),
            "validation_warnings": list(self.validation_warnings),
        }


def load_approved_project_manifest(path: Path) -> ManifestIntakeResult:
    return parse_approved_project_manifest(path.read_text(encoding="utf-8"), source_name=str(path))


def parse_approved_project_manifest(
    payload: str | bytes,
    *,
    source_name: str = "<memory>",
) -> ManifestIntakeResult:
    source_format, loaded, errors = _load_document(payload, source_name)
    if errors:
        return ManifestIntakeResult(
            source_name=source_name,
            source_format=source_format,
            manifest_hash=None,
            manifest=None,
            validation_errors=tuple(errors),
            validation_warnings=(),
        )
    if not isinstance(loaded, dict):
        return ManifestIntakeResult(
            source_name=source_name,
            source_format=source_format,
            manifest_hash=None,
            manifest=None,
            validation_errors=(f"{source_name} must contain a manifest mapping",),
            validation_warnings=(),
        )

    manifest = _normalize(loaded)
    validation_errors: list[str] = []
    validation_warnings: list[str] = []
    _validate_manifest(manifest, validation_errors, validation_warnings)
    manifest_hash = hashlib.sha256(_canonical_bytes(manifest)).hexdigest()
    return ManifestIntakeResult(
        source_name=source_name,
        source_format=source_format,
        manifest_hash=manifest_hash,
        manifest=manifest,
        validation_errors=tuple(validation_errors),
        validation_warnings=tuple(validation_warnings),
    )


def require_approved_project_manifest(
    payload: str | bytes,
    *,
    source_name: str = "<memory>",
) -> dict[str, Any]:
    result = parse_approved_project_manifest(payload, source_name=source_name)
    if not result.ok:
        raise ValueError("manifest intake failed: " + "; ".join(result.validation_errors))
    if result.manifest is None:
        raise ValueError("manifest intake failed: parsed manifest is missing")
    return result.manifest


def _load_document(payload: str | bytes, source_name: str) -> tuple[str, Any, list[str]]:
    text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    suffix = Path(source_name).suffix.lower()
    if suffix == ".json":
        try:
            return "json", json.loads(text), []
        except json.JSONDecodeError as exc:
            return "json", None, [f"{source_name} JSON parse error: {exc.msg}"]

    source_format = "json" if text.lstrip().startswith(("{", "[")) else "yaml"
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return source_format, None, [f"{source_name} YAML parse error: {exc}"]
    return source_format, loaded, []


def _validate_manifest(
    manifest: dict[str, Any],
    errors: list[str],
    warnings: list[str],
) -> None:
    for section in _REQUIRED_SECTIONS:
        if section not in manifest:
            errors.append(f"missing required manifest section {section}")
    _validate_metadata(manifest.get("metadata"), errors)
    _validate_business_intent(manifest, errors)
    _validate_section_shapes(manifest, errors)
    _validate_implementation_independence(manifest, errors)
    _validate_duplicate_ids(manifest, errors)
    _validate_capability_dependencies(manifest, errors)
    _validate_workflow_references(manifest, errors)

    if not manifest.get("workflows"):
        warnings.append("no workflows supplied; planner will need clarification before execution graph generation")


def _validate_metadata(metadata: object, errors: list[str]) -> None:
    if not isinstance(metadata, dict):
        errors.append("metadata must be a mapping")
        return
    for field in ("id", "name", *_METADATA_VERSION_FIELDS):
        if not _present(metadata.get(field)):
            errors.append(f"metadata.{field} is required")
    schema_version = metadata.get("schema_version")
    if _present(schema_version) and schema_version != MANIFEST_INTAKE_SCHEMA_VERSION:
        errors.append(
            f"metadata.schema_version must be {MANIFEST_INTAKE_SCHEMA_VERSION}, found {schema_version}"
        )
    if _approval_status(metadata) != _APPROVED_STATUS:
        errors.append("metadata.approval_status must be approved before intake")


def _validate_business_intent(manifest: dict[str, Any], errors: list[str]) -> None:
    if not _present(manifest.get("vision")) and not _present(manifest.get("intent")):
        errors.append("manifest must define business intent using vision or intent")
    if not _nonempty_list(manifest.get("objectives")) and not _nonempty_list(manifest.get("outcomes")):
        errors.append("manifest must define measurable objectives or outcomes")

    objectives = _list(manifest.get("objectives"))
    outcomes = _list(manifest.get("outcomes"))
    for index, item in enumerate([*objectives, *outcomes], start=1):
        if isinstance(item, dict) and not any(
            _present(item.get(field))
            for field in ("indicator", "measure", "metric", "success_measure", "target")
        ):
            errors.append(f"objective/outcome at index {index} must include a measurable indicator")


def _validate_section_shapes(manifest: dict[str, Any], errors: list[str]) -> None:
    if not isinstance(manifest.get("organization"), dict):
        errors.append("organization must be a mapping")
    if not _present(manifest.get("domain")):
        errors.append("domain must be non-empty")
    for section in _BUSINESS_LIST_SECTIONS:
        if section in manifest and not isinstance(manifest[section], list):
            errors.append(f"{section} must be a list")
    for section in ("entities", "capabilities"):
        if section in manifest and not _nonempty_list(manifest.get(section)):
            errors.append(f"{section} must contain at least one item")
    for index, capability in enumerate(_list(manifest.get("capabilities")), start=1):
        if isinstance(capability, dict) and not _present(capability.get("owner")):
            errors.append(f"capability at index {index} must define a business owner")


def _validate_implementation_independence(manifest: dict[str, Any], errors: list[str]) -> None:
    for path, key in _walk_keys(manifest):
        if key in _IMPLEMENTATION_SECTIONS:
            errors.append(f"{path} is implementation-specific and is not allowed in Manifest Intake")


def _validate_duplicate_ids(manifest: dict[str, Any], errors: list[str]) -> None:
    seen: dict[str, str] = {}
    for section in _BUSINESS_LIST_SECTIONS:
        for index, item in enumerate(_list(manifest.get(section)), start=1):
            item_id = _explicit_id(item)
            if item_id is None:
                continue
            location = f"{section}[{index}]"
            previous = seen.get(item_id)
            if previous is not None:
                errors.append(f"duplicate manifest object id {item_id}: {previous} and {location}")
            else:
                seen[item_id] = location


def _validate_capability_dependencies(manifest: dict[str, Any], errors: list[str]) -> None:
    capabilities = _list(manifest.get("capabilities"))
    aliases, primary_ids = _aliases(capabilities)
    graph: dict[str, set[str]] = {primary_id: set() for primary_id in primary_ids}
    for index, capability in enumerate(capabilities, start=1):
        if not isinstance(capability, dict):
            continue
        capability_id = primary_ids[index - 1]
        depends_on = capability.get("depends_on", [])
        if not isinstance(depends_on, list):
            errors.append(f"capability {capability_id} depends_on must be a list")
            continue
        for raw_dependency in depends_on:
            dependency = aliases.get(_alias(raw_dependency))
            if dependency is None:
                errors.append(f"capability {capability_id} depends on unknown capability {raw_dependency}")
                continue
            if dependency == capability_id:
                errors.append(f"capability {capability_id} cannot depend on itself")
            graph[capability_id].add(dependency)
    cycle = _cycle(graph)
    if cycle:
        errors.append(f"capability dependency cycle: {' -> '.join(cycle)}")


def _validate_workflow_references(manifest: dict[str, Any], errors: list[str]) -> None:
    capability_aliases, _ = _aliases(_list(manifest.get("capabilities")))
    entity_aliases, _ = _aliases(_list(manifest.get("entities")))
    for workflow_index, workflow in enumerate(_list(manifest.get("workflows")), start=1):
        if not isinstance(workflow, dict):
            continue
        steps = workflow.get("steps", [])
        if not isinstance(steps, list):
            errors.append(f"workflow at index {workflow_index} steps must be a list")
            continue
        for step_index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            for ref in _reference_values(step, "capability", "capabilities"):
                if _alias(ref) not in capability_aliases:
                    errors.append(
                        f"workflow {workflow_index} step {step_index} references unknown capability {ref}"
                    )
            for ref in _reference_values(step, "entity", "entities"):
                if _alias(ref) not in entity_aliases:
                    errors.append(f"workflow {workflow_index} step {step_index} references unknown entity {ref}")


def _aliases(items: list[Any]) -> tuple[dict[str, str], list[str]]:
    aliases: dict[str, str] = {}
    primary_ids: list[str] = []
    for index, item in enumerate(items, start=1):
        primary = _primary_id(item, index)
        primary_ids.append(primary)
        for alias in _item_aliases(item, primary):
            aliases[alias] = primary
    return aliases, primary_ids


def _item_aliases(item: object, primary: str) -> set[str]:
    aliases = {_alias(primary)}
    if isinstance(item, str):
        aliases.add(_alias(item))
    elif isinstance(item, dict):
        for key in ("id", "name", "title"):
            if _present(item.get(key)):
                aliases.add(_alias(item[key]))
    return aliases


def _primary_id(item: object, index: int) -> str:
    explicit = _explicit_id(item)
    if explicit is not None:
        return explicit
    if isinstance(item, str):
        return _alias(item)
    if isinstance(item, dict):
        for key in ("name", "title"):
            if _present(item.get(key)):
                return _alias(item[key])
    return f"item-{index:03d}"


def _explicit_id(item: object) -> str | None:
    if isinstance(item, dict) and _present(item.get("id")):
        return str(item["id"])
    return None


def _reference_values(item: dict[str, Any], singular: str, plural: str) -> list[Any]:
    refs: list[Any] = []
    if _present(item.get(singular)):
        refs.append(item[singular])
    if isinstance(item.get(plural), list):
        refs.extend(item[plural])
    return refs


def _cycle(graph: dict[str, set[str]]) -> list[str]:
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(node: str) -> list[str] | None:
        if node in visited:
            return None
        if node in visiting:
            return [*visiting[visiting.index(node) :], node]
        visiting.append(node)
        for dependency in sorted(graph.get(node, set())):
            found = visit(dependency)
            if found:
                return found
        visiting.pop()
        visited.add(node)
        return None

    for node in sorted(graph):
        found = visit(node)
        if found:
            return found
    return []


def _walk_keys(value: object, prefix: str = "manifest") -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}"
            keys.append((path, key))
            keys.extend(_walk_keys(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value, start=1):
            keys.extend(_walk_keys(child, f"{prefix}[{index}]"))
    return keys


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            _normalize_key(key): _normalize(value[key])
            for key in sorted(value, key=lambda item: _normalize_key(item))
            if value[key] is not None
        }
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, str):
        return value.strip()
    return value


def _normalize_key(key: object) -> str:
    value = str(key).strip().replace("-", "_").replace(" ", "_")
    value = re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()
    return re.sub(r"_+", "_", value)


def _approval_status(metadata: dict[str, Any]) -> str | None:
    raw = metadata.get("approval_status", metadata.get("status"))
    return str(raw).strip().lower() if _present(raw) else None


def _alias(value: object) -> str:
    normalized = str(value).strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
    return normalized.strip("-")


def _present(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _nonempty_list(value: object) -> bool:
    return isinstance(value, list) and bool(value)


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(_normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
