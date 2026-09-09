from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

Classification = Literal["NORMATIVE", "INFORMATIVE", "DECISION", "EXAMPLE", "FUTURE/DEFERRED"]
CapabilityStatus = Literal["REQUIRED", "CONDITIONAL", "DEFERRED", "NOT_REQUIRED"]
CapabilityMaturity = Literal["NOT_IMPLEMENTED", "DEVELOPMENT", "VALIDATED", "PRODUCTION_READY"]

PROJECT_MANIFEST_ROOT = Path("manifest/project/ai-ent")


class SourceRef(BaseModel):
    id: str
    path: str
    role: str


class ProjectDocument(BaseModel):
    id: str
    name: str
    version: str
    classification: Classification
    status: str
    source_corpus: list[SourceRef] = Field(default_factory=list)
    objectives: list[str] = Field(default_factory=list)
    already_implemented_platform_capabilities: list[str] = Field(default_factory=list)
    authority: dict[str, Any] = Field(default_factory=dict)


class Capability(BaseModel):
    id: str
    name: str
    status: CapabilityStatus
    maturity: CapabilityMaturity
    classification: Classification
    source_refs: list[str]
    depends_on: list[str] = Field(default_factory=list)


class TraceableItem(BaseModel):
    id: str
    title: str | None = None
    name: str | None = None
    classification: Classification | None = None
    source_refs: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    capability: str | None = None
    requirements: list[str] = Field(default_factory=list)
    epic: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    from_component: str | None = None
    to_component: str | None = None


@dataclass(frozen=True)
class ManifestValidationResult:
    manifest_files: tuple[str, ...]
    requirements_count: int
    components_count: int
    interfaces_count: int
    capabilities_selected: tuple[str, ...]
    unresolved_decisions: tuple[str, ...]
    validation_errors: tuple[str, ...]
    validation_warnings: tuple[str, ...]
    already_satisfied: tuple[str, ...]
    still_missing: tuple[str, ...]
    compilation_hash: str

    @property
    def ok(self) -> bool:
        return not self.validation_errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest_files_loaded": list(self.manifest_files),
            "requirements_count": self.requirements_count,
            "components_count": self.components_count,
            "interfaces_count": self.interfaces_count,
            "capabilities_selected": list(self.capabilities_selected),
            "unresolved_decisions": list(self.unresolved_decisions),
            "validation_errors": list(self.validation_errors),
            "validation_warnings": list(self.validation_warnings),
            "implementation_areas_already_satisfied": list(self.already_satisfied),
            "implementation_areas_still_missing": list(self.still_missing),
            "compilation_hash": self.compilation_hash,
        }


def validate_project_manifest(root: Path = PROJECT_MANIFEST_ROOT) -> ManifestValidationResult:
    documents = load_manifest_documents(root)
    errors: list[str] = []
    warnings: list[str] = []

    project = _parse_project(documents, errors)
    source_ids = {source.id for source in project.source_corpus} if project else set()
    source_ids.add("BEAG-001")

    capabilities = _parse_capabilities(documents, errors)
    capability_ids = {capability.id for capability in capabilities}
    _validate_unique_ids(documents, errors)
    _validate_source_refs(documents, source_ids, errors)
    _validate_capability_dependencies(capabilities, capability_ids, errors)

    requirements = _items(documents, "requirements")
    components = _items(documents, "components")
    interfaces = _items(documents, "interfaces")
    agents = _items(documents, "agents")
    epics = _items(documents, "epics")
    features = _items(documents, "features")

    for item in [*requirements, *components, *agents]:
        _validate_capability_refs(item, capability_ids, errors)
    component_ids = {_item_id(component) for component in components}
    requirement_ids = {_item_id(requirement) for requirement in requirements}
    epic_ids = {_item_id(epic) for epic in epics}
    _validate_interfaces(interfaces, component_ids, errors)
    _validate_feature_refs(features, epic_ids, requirement_ids, errors)

    selected = tuple(sorted(capability.id for capability in capabilities if capability.status in {"REQUIRED", "CONDITIONAL"}))
    already = tuple(sorted(capability.id for capability in capabilities if capability.maturity in {"VALIDATED", "PRODUCTION_READY"}))
    missing = tuple(
        sorted(
            capability.id
            for capability in capabilities
            if capability.status == "REQUIRED" and capability.maturity not in {"VALIDATED", "PRODUCTION_READY"}
        )
    )
    unresolved = _unresolved_decisions(documents)
    if not unresolved:
        warnings.append("no unresolved decisions recorded")

    return ManifestValidationResult(
        manifest_files=tuple(sorted(str(path.relative_to(root)) for path in documents)),
        requirements_count=len(requirements),
        components_count=len(components),
        interfaces_count=len(interfaces),
        capabilities_selected=selected,
        unresolved_decisions=tuple(unresolved),
        validation_errors=tuple(errors),
        validation_warnings=tuple(warnings),
        already_satisfied=already,
        still_missing=missing,
        compilation_hash=compilation_hash(documents),
    )


def load_manifest_documents(root: Path) -> dict[Path, dict[str, Any]]:
    if not root.exists():
        raise FileNotFoundError(f"project manifest root not found: {root}")
    documents: dict[Path, dict[str, Any]] = {}
    for path in sorted(root.rglob("*.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise TypeError(f"{path} must contain a YAML mapping")
        documents[path] = loaded
    return documents


def compilation_hash(documents: dict[Path, dict[str, Any]]) -> str:
    normalized = {
        str(path): documents[path]
        for path in sorted(documents, key=lambda item: str(item))
    }
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_project(documents: dict[Path, dict[str, Any]], errors: list[str]) -> ProjectDocument | None:
    project_path = next((path for path in documents if path.name == "project.yaml"), None)
    if project_path is None:
        errors.append("missing project.yaml")
        return None
    try:
        return ProjectDocument.model_validate(documents[project_path])
    except ValidationError as exc:
        errors.append(f"project.yaml schema error: {_compact_validation(exc)}")
        return None


def _parse_capabilities(documents: dict[Path, dict[str, Any]], errors: list[str]) -> list[Capability]:
    path = next((candidate for candidate in documents if candidate.name == "capabilities.yaml"), None)
    if path is None:
        errors.append("missing capabilities.yaml")
        return []
    raw = documents[path].get("capabilities", [])
    if not isinstance(raw, list):
        errors.append("capabilities.yaml capabilities must be a list")
        return []
    capabilities: list[Capability] = []
    for item in raw:
        try:
            capabilities.append(Capability.model_validate(item))
        except ValidationError as exc:
            errors.append(f"capability schema error: {_compact_validation(exc)}")
    if len(capabilities) != 20:
        errors.append(f"expected 20 capabilities, found {len(capabilities)}")
    return capabilities


def _items(documents: dict[Path, dict[str, Any]], key: str) -> list[TraceableItem]:
    items: list[TraceableItem] = []
    for document in documents.values():
        raw = document.get(key)
        if isinstance(raw, list):
            for value in raw:
                if isinstance(value, dict) and "id" in value:
                    items.append(TraceableItem.model_validate(value))
    return items


def _validate_unique_ids(documents: dict[Path, dict[str, Any]], errors: list[str]) -> None:
    seen: dict[str, Path] = {}
    for path, document in documents.items():
        for item_id in _walk_ids(document):
            previous = seen.get(item_id)
            if previous is not None:
                errors.append(f"duplicate id {item_id}: {previous} and {path}")
            else:
                seen[item_id] = path


def _walk_ids(value: object) -> list[str]:
    ids: list[str] = []
    if isinstance(value, dict):
        item_id = value.get("id")
        if isinstance(item_id, str):
            ids.append(item_id)
        for child in value.values():
            ids.extend(_walk_ids(child))
    elif isinstance(value, list):
        for child in value:
            ids.extend(_walk_ids(child))
    return ids


def _validate_source_refs(documents: dict[Path, dict[str, Any]], source_ids: set[str], errors: list[str]) -> None:
    for path, document in documents.items():
        for source_ref in _walk_source_refs(document):
            if source_ref not in source_ids:
                errors.append(f"unresolved source_ref {source_ref}: {path}")


def _walk_source_refs(value: object) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        raw = value.get("source_refs")
        if isinstance(raw, list):
            refs.extend(str(item) for item in raw)
        for child in value.values():
            refs.extend(_walk_source_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.extend(_walk_source_refs(child))
    return refs


def _validate_capability_dependencies(capabilities: list[Capability], capability_ids: set[str], errors: list[str]) -> None:
    for capability in capabilities:
        for dependency in capability.depends_on:
            if dependency not in capability_ids:
                errors.append(f"{capability.id} depends on unknown capability {dependency}")


def _validate_capability_refs(item: TraceableItem, capability_ids: set[str], errors: list[str]) -> None:
    refs = [*item.capabilities]
    if item.capability:
        refs.append(item.capability)
    for capability_id in refs:
        if capability_id not in capability_ids:
            errors.append(f"{item.id} references unknown capability {capability_id}")


def _validate_interfaces(items: list[TraceableItem], component_ids: set[str], errors: list[str]) -> None:
    for item in items:
        if item.from_component not in component_ids:
            errors.append(f"{item.id} references unknown from_component {item.from_component}")
        if item.to_component not in component_ids:
            errors.append(f"{item.id} references unknown to_component {item.to_component}")


def _validate_feature_refs(
    features: list[TraceableItem],
    epic_ids: set[str],
    requirement_ids: set[str],
    errors: list[str],
) -> None:
    for feature in features:
        epic = getattr(feature, "epic", None)
        if isinstance(epic, str) and epic not in epic_ids:
            errors.append(f"{feature.id} references unknown epic {epic}")
        for requirement_id in feature.requirements:
            if requirement_id not in requirement_ids:
                errors.append(f"{feature.id} references unknown requirement {requirement_id}")


def _item_id(item: TraceableItem) -> str:
    return item.id


def _unresolved_decisions(documents: dict[Path, dict[str, Any]]) -> list[str]:
    unresolved: list[str] = []
    for document in documents.values():
        decisions = document.get("decisions")
        if isinstance(decisions, list):
            for decision in decisions:
                if isinstance(decision, dict) and decision.get("status") in {"open", "unresolved"}:
                    unresolved.append(str(decision.get("id", "unknown")))
    return sorted(unresolved)


def _compact_validation(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", "validation failed"))
    return f"{loc}: {message}" if loc else message
