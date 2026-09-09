from __future__ import annotations

from pathlib import Path

from ai_ent.canonical_project_model import (
    CANONICAL_PROJECT_MODEL_CONTRACT_VERSION,
    CANONICAL_PROJECT_MODEL_SCHEMA_VERSION,
    CanonicalProjectModel,
    CanonicalProjectModelService,
    compile_canonical_project_model,
)

MANIFEST_ROOT = Path("manifest/project/ai-ent")


def test_data_001_exposes_canonical_objects_and_relationships_as_records() -> None:
    model = compile_canonical_project_model(MANIFEST_ROOT)
    records = model.as_storage_records()
    object_records = records["canonical_objects"]
    relationship_records = records["canonical_relationships"]
    object_ids = {record["object_id"] for record in object_records}

    assert records["schema_version"] == CANONICAL_PROJECT_MODEL_SCHEMA_VERSION
    assert records["contract_version"] == CANONICAL_PROJECT_MODEL_CONTRACT_VERSION
    assert "data_object:DAT-002" in object_ids
    assert "data_object:DAT-003" in object_ids
    assert "requirement:DATA-001" in object_ids
    assert all(record["record_kind"] == "canonical_object" for record in object_records)
    assert all(len(record["payload_hash"]) == 64 for record in object_records)
    assert all(record["record_kind"] == "canonical_relationship" for record in relationship_records)
    assert all(len(record["relationship_id"]) > len("relationship:") for record in relationship_records)

    data_requirement = model.object_by_id("requirement:DATA-001")

    assert data_requirement is not None
    assert data_requirement.capability_refs == ("C02", "C16")
    assert _has_relationship(model, "REQUIRES_CAPABILITY", "requirement:DATA-001", "capability:C02")
    assert _has_relationship(
        model,
        "IMPLEMENTED_BY_COMPONENT",
        "requirement:DATA-001",
        "component:CMP-003",
    )


def test_fr_002_compiles_manifest_into_repeatable_canonical_project_model() -> None:
    first = CanonicalProjectModelService().compile(MANIFEST_ROOT)
    second = CanonicalProjectModelService().compile(MANIFEST_ROOT)
    fr_002 = first.object_by_id("requirement:FR-002")
    c02 = first.object_by_id("capability:C02")

    assert first.model_hash == second.model_hash
    assert first.as_dict() == second.as_dict()
    assert first.schema_version == "aeir-canonical-project-model-v0.1"
    assert first.compiler_version == second.compiler_version
    assert len(first.source_compiled_hash) == 64
    assert fr_002 is not None
    assert fr_002.name == "Compile manifests into canonical semantic project models"
    assert c02 is not None
    assert c02.name == "Canonical AEIR project model"
    assert _has_relationship(first, "REQUIRES_CAPABILITY", "requirement:FR-002", "capability:C02")
    assert _has_relationship(first, "SOURCED_FROM", "requirement:FR-002", "source:R3")


def test_nfr_002_traceability_links_source_intent_to_verification_and_evidence() -> None:
    model = compile_canonical_project_model(MANIFEST_ROOT)
    trace = model.trace_for_requirement("NFR-002")
    relationship_types = {relationship.relationship_type for relationship in trace}
    targets = {relationship.target_object_id for relationship in trace}

    assert {
        "SOURCED_FROM",
        "REQUIRES_CAPABILITY",
        "VERIFIED_BY",
        "EVIDENCED_BY",
    } <= relationship_types
    assert "source:R22" in targets
    assert "capability:C02" in targets
    assert "capability:C16" in targets
    assert "capability:C20" in targets
    assert "verification_gate:VG-004" in targets
    assert "evidence:BEAG-001:guarded-autonomous-runner" in targets


def _has_relationship(
    model: CanonicalProjectModel,
    relationship_type: str,
    source_object_id: str,
    target_object_id: str,
) -> bool:
    return any(
        relationship.relationship_type == relationship_type
        and relationship.source_object_id == source_object_id
        and relationship.target_object_id == target_object_id
        for relationship in model.relationships
    )
