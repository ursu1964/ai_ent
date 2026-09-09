from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ai_ent.project_manifest import (
    PROJECT_MANIFEST_ROOT,
    CompilationResult,
    canonical_bytes,
    compile_project_manifest,
)

CANONICAL_PROJECT_MODEL_SCHEMA_VERSION = "aeir-canonical-project-model-v0.1"
CANONICAL_PROJECT_MODEL_CONTRACT_VERSION = "c02.1"

CanonicalObjectType = Literal[
    "agent",
    "business_objective",
    "business_stakeholder",
    "capability",
    "component",
    "data_object",
    "deployment",
    "epic",
    "evidence",
    "feature",
    "intake",
    "interface",
    "model",
    "operation",
    "project",
    "repository",
    "requirement",
    "roadmap_item",
    "source",
    "success_measure",
    "task_graph",
    "tool",
    "verification_gate",
]

CanonicalRelationshipType = Literal[
    "BELONGS_TO_EPIC",
    "CONNECTS_FROM",
    "CONNECTS_TO",
    "DEPENDS_ON",
    "EVIDENCED_BY",
    "EXPOSED_BY_INTERFACE",
    "IMPLEMENTS_CAPABILITY",
    "IMPLEMENTED_BY_COMPONENT",
    "PLANS_REQUIREMENT",
    "REQUIRES_CAPABILITY",
    "SOURCED_FROM",
    "VERIFIED_BY",
]


@dataclass(frozen=True)
class CanonicalProjectObject:
    object_id: str
    object_type: CanonicalObjectType
    native_id: str
    name: str
    classification: str | None
    source_refs: tuple[str, ...]
    capability_refs: tuple[str, ...]
    source_manifest_file: str | None
    payload_hash: str
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_kind": "canonical_object",
            "object_id": self.object_id,
            "object_type": self.object_type,
            "native_id": self.native_id,
            "name": self.name,
            "classification": self.classification,
            "source_refs": list(self.source_refs),
            "capability_refs": list(self.capability_refs),
            "source_manifest_file": self.source_manifest_file,
            "payload_hash": self.payload_hash,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class CanonicalProjectRelationship:
    relationship_id: str
    relationship_type: CanonicalRelationshipType
    source_object_id: str
    target_object_id: str
    source_native_id: str
    target_native_id: str
    provenance_refs: tuple[str, ...]
    payload_hash: str
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_kind": "canonical_relationship",
            "relationship_id": self.relationship_id,
            "relationship_type": self.relationship_type,
            "source_object_id": self.source_object_id,
            "target_object_id": self.target_object_id,
            "source_native_id": self.source_native_id,
            "target_native_id": self.target_native_id,
            "provenance_refs": list(self.provenance_refs),
            "payload_hash": self.payload_hash,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class CanonicalProjectModel:
    model_id: str
    schema_version: str
    contract_version: str
    compiler_version: str
    source_compiled_hash: str
    model_hash: str
    objects: tuple[CanonicalProjectObject, ...]
    relationships: tuple[CanonicalProjectRelationship, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "schema_version": self.schema_version,
            "contract_version": self.contract_version,
            "compiler_version": self.compiler_version,
            "source_compiled_hash": self.source_compiled_hash,
            "model_hash": self.model_hash,
            "objects": [item.as_dict() for item in self.objects],
            "relationships": [item.as_dict() for item in self.relationships],
            "summary": {
                "object_count": len(self.objects),
                "relationship_count": len(self.relationships),
                "object_types": sorted({item.object_type for item in self.objects}),
                "relationship_types": sorted(
                    {item.relationship_type for item in self.relationships}
                ),
            },
        }

    def as_storage_records(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_version": self.contract_version,
            "model_id": self.model_id,
            "model_hash": self.model_hash,
            "compiler_version": self.compiler_version,
            "source_compiled_hash": self.source_compiled_hash,
            "canonical_objects": [item.as_dict() for item in self.objects],
            "canonical_relationships": [item.as_dict() for item in self.relationships],
        }

    def object_by_id(self, object_id: str) -> CanonicalProjectObject | None:
        return next((item for item in self.objects if item.object_id == object_id), None)

    def objects_by_type(
        self,
        object_type: CanonicalObjectType,
    ) -> tuple[CanonicalProjectObject, ...]:
        return tuple(item for item in self.objects if item.object_type == object_type)

    def relationships_for(self, object_id: str) -> tuple[CanonicalProjectRelationship, ...]:
        canonical_id = _canonical_id_or_native(object_id, self.objects)
        return tuple(
            item
            for item in self.relationships
            if item.source_object_id == canonical_id or item.target_object_id == canonical_id
        )

    def trace_for_requirement(
        self,
        requirement_id: str,
    ) -> tuple[CanonicalProjectRelationship, ...]:
        canonical_id = (
            requirement_id
            if requirement_id.startswith("requirement:")
            else f"requirement:{requirement_id}"
        )
        return tuple(item for item in self.relationships if item.source_object_id == canonical_id)


class CanonicalProjectModelService:
    """Service boundary for the C02 Canonical AEIR project model contract."""

    def compile(self, root: Path = PROJECT_MANIFEST_ROOT) -> CanonicalProjectModel:
        return self.from_compilation(compile_project_manifest(root))

    def from_compilation(self, compilation: CompilationResult) -> CanonicalProjectModel:
        return build_canonical_project_model(compilation)


def compile_canonical_project_model(root: Path = PROJECT_MANIFEST_ROOT) -> CanonicalProjectModel:
    return CanonicalProjectModelService().compile(root)


def build_canonical_project_model(compilation: CompilationResult) -> CanonicalProjectModel:
    objects = _canonical_objects(compilation)
    relationships = _canonical_relationships(compilation, objects)
    payload = {
        "schema_version": CANONICAL_PROJECT_MODEL_SCHEMA_VERSION,
        "contract_version": CANONICAL_PROJECT_MODEL_CONTRACT_VERSION,
        "compiler_version": compilation.lock.compiler_version,
        "source_compiled_hash": compilation.lock.compiled_hash,
        "objects": [item.as_dict() for item in objects],
        "relationships": [item.as_dict() for item in relationships],
    }
    model_hash = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    return CanonicalProjectModel(
        model_id=f"AEIR-{model_hash[:12]}",
        schema_version=CANONICAL_PROJECT_MODEL_SCHEMA_VERSION,
        contract_version=CANONICAL_PROJECT_MODEL_CONTRACT_VERSION,
        compiler_version=compilation.lock.compiler_version,
        source_compiled_hash=compilation.lock.compiled_hash,
        model_hash=model_hash,
        objects=objects,
        relationships=relationships,
    )


def _canonical_objects(compilation: CompilationResult) -> tuple[CanonicalProjectObject, ...]:
    compiled = compilation.compiled
    objects: list[CanonicalProjectObject] = []
    referenced_sources: set[str] = set()
    evidence_refs: set[str] = set()

    def add(object_type: CanonicalObjectType, native_id: str, payload: dict[str, Any]) -> None:
        objects.append(_object_record(object_type, native_id, payload))
        referenced_sources.update(str(item) for item in payload.get("source_refs", []))
        evidence_refs.update(str(item) for item in payload.get("evidence_refs", []))

    add("project", str(compiled.project["id"]), dict(compiled.project))
    add("intake", str(compiled.project["id"]), dict(compiled.intake))
    add("repository", str(compiled.project["id"]), dict(compiled.repository))
    add("deployment", str(compiled.project["id"]), dict(compiled.deployment))

    for source in compiled.project.get("source_corpus", []):
        if isinstance(source, dict) and source.get("id") is not None:
            add("source", str(source["id"]), dict(source))

    for item in compiled.intake.get("objectives", []):
        add("business_objective", str(item["id"]), dict(item))
    for item in compiled.intake.get("stakeholders", []):
        add("business_stakeholder", str(item["id"]), dict(item))
    for item in compiled.intake.get("success_measures", []):
        add("success_measure", str(item["id"]), dict(item))
    for item in compiled.requirements:
        add("requirement", str(item["id"]), dict(item))
    for item in compiled.capabilities:
        add("capability", str(item["id"]), dict(item))
    for item in compiled.architecture.get("components", []):
        add("component", str(item["id"]), dict(item))
    for item in compiled.architecture.get("interfaces", []):
        add("interface", str(item["id"]), dict(item))
    for item in compiled.architecture.get("data_objects", []):
        add("data_object", str(item["id"]), dict(item))
    for item in compiled.verification.get("verification", {}).get("gates", []):
        add("verification_gate", str(item["id"]), dict(item))
    for item in compiled.agents:
        add("agent", str(item["id"]), dict(item))
    for item in compiled.models:
        add("model", str(item["id"]), dict(item))
    for item in compiled.tools:
        add("tool", str(item["id"]), dict(item))
    for item in compiled.operations:
        add("operation", str(item["id"]), dict(item))
    for item in compiled.planning.get("roadmap", []):
        add("roadmap_item", str(item["id"]), dict(item))
    for item in compiled.planning.get("epics", []):
        add("epic", str(item["id"]), dict(item))
    for item in compiled.planning.get("features", []):
        add("feature", str(item["id"]), dict(item))
    add("task_graph", str(compiled.project["id"]), dict(compiled.planning.get("task_graph", {})))

    known_source_ids = {item.native_id for item in objects if item.object_type == "source"}
    for source_id in sorted(referenced_sources - known_source_ids):
        add("source", source_id, {"id": source_id, "role": "external_trace_source"})
    for evidence_ref in sorted(evidence_refs):
        add(
            "evidence",
            evidence_ref,
            {"id": evidence_ref, "source_refs": [_evidence_source_id(evidence_ref)]},
        )

    return tuple(sorted(objects, key=lambda item: (item.object_type, item.object_id)))


def _object_record(
    object_type: CanonicalObjectType,
    native_id: str,
    payload: dict[str, Any],
) -> CanonicalProjectObject:
    payload_hash = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    capability_refs = tuple(
        sorted(
            {
                str(capability)
                for capability in [
                    *payload.get("capabilities", []),
                    *([payload["capability"]] if payload.get("capability") is not None else []),
                ]
            }
        )
    )
    source_manifest_file = payload.get("source_manifest_file")
    return CanonicalProjectObject(
        object_id=_object_id(object_type, native_id),
        object_type=object_type,
        native_id=native_id,
        name=_object_name(native_id, payload),
        classification=payload.get("classification"),
        source_refs=tuple(sorted(str(item) for item in payload.get("source_refs", []))),
        capability_refs=capability_refs,
        source_manifest_file=(
            str(source_manifest_file) if source_manifest_file is not None else None
        ),
        payload_hash=payload_hash,
        payload=payload,
    )


def _canonical_relationships(
    compilation: CompilationResult,
    objects: tuple[CanonicalProjectObject, ...],
) -> tuple[CanonicalProjectRelationship, ...]:
    compiled = compilation.compiled
    object_ids = {item.object_id for item in objects}
    native_index = _native_index(objects)
    relationships: dict[str, CanonicalProjectRelationship] = {}

    def add(
        relationship_type: CanonicalRelationshipType,
        source_object_id: str,
        target_object_id: str,
        *,
        provenance_refs: tuple[str, ...] = (),
        payload: dict[str, Any] | None = None,
    ) -> None:
        if source_object_id not in object_ids or target_object_id not in object_ids:
            return
        relationship = _relationship_record(
            relationship_type,
            source_object_id,
            target_object_id,
            native_index,
            provenance_refs=provenance_refs,
            payload=payload or {},
        )
        relationships[relationship.relationship_id] = relationship

    component_by_capability = _ids_by_capability(compiled.architecture.get("components", []))
    interface_by_component = _interfaces_by_component(compiled.architecture.get("interfaces", []))
    gates_by_source = _ids_by_source(compiled.verification.get("verification", {}).get("gates", []))
    capability_by_id = {str(item["id"]): dict(item) for item in compiled.capabilities}

    for item in objects:
        for source_ref in item.source_refs:
            add(
                "SOURCED_FROM",
                item.object_id,
                f"source:{source_ref}",
                provenance_refs=(source_ref,),
            )

    for capability in compiled.capabilities:
        capability_id = str(capability["id"])
        capability_object_id = f"capability:{capability_id}"
        for dependency in capability.get("depends_on", []):
            add(
                "DEPENDS_ON",
                capability_object_id,
                f"capability:{dependency}",
                provenance_refs=tuple(capability.get("source_refs", [])),
            )
        for evidence_ref in capability.get("evidence_refs", []):
            add(
                "EVIDENCED_BY",
                capability_object_id,
                f"evidence:{evidence_ref}",
                provenance_refs=tuple(capability.get("source_refs", [])),
            )

    for component in compiled.architecture.get("components", []):
        component_id = str(component["id"])
        for capability_id in component.get("capabilities", []):
            add(
                "IMPLEMENTS_CAPABILITY",
                f"component:{component_id}",
                f"capability:{capability_id}",
                provenance_refs=tuple(component.get("source_refs", [])),
            )

    for interface in compiled.architecture.get("interfaces", []):
        interface_id = str(interface["id"])
        from_component = interface.get("from_component")
        to_component = interface.get("to_component")
        if from_component is not None:
            add(
                "CONNECTS_FROM",
                f"interface:{interface_id}",
                f"component:{from_component}",
                provenance_refs=tuple(interface.get("source_refs", [])),
            )
        if to_component is not None:
            add(
                "CONNECTS_TO",
                f"interface:{interface_id}",
                f"component:{to_component}",
                provenance_refs=tuple(interface.get("source_refs", [])),
            )

    for requirement in compiled.requirements:
        requirement_id = str(requirement["id"])
        requirement_object_id = f"requirement:{requirement_id}"
        requirement_sources = tuple(str(item) for item in requirement.get("source_refs", []))
        capability_ids = tuple(str(item) for item in requirement.get("capabilities", []))
        component_ids = tuple(
            sorted(
                {
                    component_id
                    for capability_id in capability_ids
                    for component_id in component_by_capability.get(capability_id, ())
                }
            )
        )
        interface_ids = tuple(
            sorted(
                {
                    interface_id
                    for component_id in component_ids
                    for interface_id in interface_by_component.get(component_id, ())
                }
            )
        )
        verification_ids = tuple(
            sorted(
                {
                    gate_id
                    for source_ref in requirement_sources
                    for gate_id in gates_by_source.get(source_ref, ())
                }
            )
        )
        evidence_refs = tuple(
            sorted(
                {
                    str(evidence_ref)
                    for capability_id in capability_ids
                    for evidence_ref in capability_by_id.get(capability_id, {}).get(
                        "evidence_refs",
                        [],
                    )
                }
            )
        )
        for capability_id in capability_ids:
            add(
                "REQUIRES_CAPABILITY",
                requirement_object_id,
                f"capability:{capability_id}",
                provenance_refs=requirement_sources,
            )
        for component_id in component_ids:
            add(
                "IMPLEMENTED_BY_COMPONENT",
                requirement_object_id,
                f"component:{component_id}",
                provenance_refs=requirement_sources,
            )
        for interface_id in interface_ids:
            add(
                "EXPOSED_BY_INTERFACE",
                requirement_object_id,
                f"interface:{interface_id}",
                provenance_refs=requirement_sources,
            )
        for gate_id in verification_ids:
            add(
                "VERIFIED_BY",
                requirement_object_id,
                f"verification_gate:{gate_id}",
                provenance_refs=requirement_sources,
            )
        for evidence_ref in evidence_refs:
            add(
                "EVIDENCED_BY",
                requirement_object_id,
                f"evidence:{evidence_ref}",
                provenance_refs=requirement_sources,
            )

    for feature in compiled.planning.get("features", []):
        feature_id = str(feature["id"])
        for requirement_id in feature.get("requirements", []):
            add(
                "PLANS_REQUIREMENT",
                f"feature:{feature_id}",
                f"requirement:{requirement_id}",
                provenance_refs=tuple(feature.get("source_refs", [])),
            )
        epic_id = feature.get("epic")
        if epic_id is not None:
            add("BELONGS_TO_EPIC", f"feature:{feature_id}", f"epic:{epic_id}")

    return tuple(sorted(relationships.values(), key=lambda item: item.relationship_id))


def _relationship_record(
    relationship_type: CanonicalRelationshipType,
    source_object_id: str,
    target_object_id: str,
    native_index: dict[str, str],
    *,
    provenance_refs: tuple[str, ...],
    payload: dict[str, Any],
) -> CanonicalProjectRelationship:
    normalized_payload = {
        "relationship_type": relationship_type,
        "source_object_id": source_object_id,
        "target_object_id": target_object_id,
        "provenance_refs": sorted(provenance_refs),
        **payload,
    }
    payload_hash = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    relationship_hash = hashlib.sha256(canonical_bytes(normalized_payload)).hexdigest()
    return CanonicalProjectRelationship(
        relationship_id=f"relationship:{relationship_hash[:32]}",
        relationship_type=relationship_type,
        source_object_id=source_object_id,
        target_object_id=target_object_id,
        source_native_id=native_index[source_object_id],
        target_native_id=native_index[target_object_id],
        provenance_refs=tuple(sorted(provenance_refs)),
        payload_hash=payload_hash,
        payload=payload,
    )


def _object_id(object_type: CanonicalObjectType, native_id: str) -> str:
    return f"{object_type}:{native_id}"


def _object_name(native_id: str, payload: dict[str, Any]) -> str:
    for key in ("name", "title", "statement", "role", "path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return native_id


def _native_index(objects: tuple[CanonicalProjectObject, ...]) -> dict[str, str]:
    return {item.object_id: item.native_id for item in objects}


def _canonical_id_or_native(object_id: str, objects: tuple[CanonicalProjectObject, ...]) -> str:
    known = {item.object_id for item in objects}
    if object_id in known:
        return object_id
    matches = [item.object_id for item in objects if item.native_id == object_id]
    if len(matches) == 1:
        return matches[0]
    return object_id


def _ids_by_capability(items: object) -> dict[str, tuple[str, ...]]:
    result: dict[str, set[str]] = {}
    if not isinstance(items, list):
        return {}
    for item in items:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        for capability_id in item.get("capabilities", []):
            result.setdefault(str(capability_id), set()).add(str(item["id"]))
        capability_id = item.get("capability")
        if capability_id is not None:
            result.setdefault(str(capability_id), set()).add(str(item["id"]))
    return {capability_id: tuple(sorted(values)) for capability_id, values in result.items()}


def _interfaces_by_component(items: object) -> dict[str, tuple[str, ...]]:
    result: dict[str, set[str]] = {}
    if not isinstance(items, list):
        return {}
    for item in items:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        interface_id = str(item["id"])
        for component_id in (item.get("from_component"), item.get("to_component")):
            if component_id is not None:
                result.setdefault(str(component_id), set()).add(interface_id)
    return {component_id: tuple(sorted(values)) for component_id, values in result.items()}


def _ids_by_source(items: object) -> dict[str, tuple[str, ...]]:
    result: dict[str, set[str]] = {}
    if not isinstance(items, list):
        return {}
    for item in items:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        for source_ref in item.get("source_refs", []):
            result.setdefault(str(source_ref), set()).add(str(item["id"]))
    return {source_ref: tuple(sorted(values)) for source_ref, values in result.items()}


def _evidence_source_id(evidence_ref: str) -> str:
    return evidence_ref.split(":", 1)[0]
