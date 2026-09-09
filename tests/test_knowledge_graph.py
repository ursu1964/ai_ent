from __future__ import annotations

from pathlib import Path

from ai_ent.canonical_project_model import compile_canonical_project_model
from ai_ent.knowledge_graph import (
    KNOWLEDGE_GRAPH_CONTRACT_VERSION,
    KNOWLEDGE_GRAPH_SCHEMA_VERSION,
    KnowledgeGraph,
    KnowledgeGraphService,
    compile_knowledge_graph,
)

MANIFEST_ROOT = Path("manifest/project/ai-ent")


def test_data_001_knowledge_graph_exposes_first_class_records() -> None:
    graph = compile_knowledge_graph(MANIFEST_ROOT)
    records = graph.as_storage_records()
    node_records = records["knowledge_graph_nodes"]
    edge_records = records["knowledge_graph_edges"]
    node_ids = {record["node_id"] for record in node_records}

    assert records["schema_version"] == KNOWLEDGE_GRAPH_SCHEMA_VERSION
    assert records["contract_version"] == KNOWLEDGE_GRAPH_CONTRACT_VERSION
    assert "data_object:DAT-002" in node_ids
    assert "data_object:DAT-003" in node_ids
    assert "requirement:DATA-001" in node_ids
    assert all(record["record_kind"] == "knowledge_graph_node" for record in node_records)
    assert all(record["record_kind"] == "knowledge_graph_edge" for record in edge_records)
    assert all(len(record["canonical_payload_hash"]) == 64 for record in node_records)
    assert all(len(record["edge_id"]) > len("relationship:") for record in edge_records)

    data_requirement = graph.node_by_id("requirement:DATA-001")

    assert data_requirement is not None
    assert data_requirement.capability_refs == ("C02", "C16")
    assert _has_edge(graph, "REQUIRES_CAPABILITY", "requirement:DATA-001", "capability:C16")
    assert _has_edge(
        graph,
        "IMPLEMENTED_BY_COMPONENT",
        "requirement:DATA-001",
        "component:CMP-003",
    )


def test_fr_002_knowledge_graph_is_repeatable_from_canonical_model() -> None:
    model = compile_canonical_project_model(MANIFEST_ROOT)
    service = KnowledgeGraphService()
    first = service.from_model(model)
    second = service.from_model(model)
    fr_002 = first.node_by_id("requirement:FR-002")

    assert first.graph_hash == second.graph_hash
    assert first.as_dict() == second.as_dict()
    assert first.source_model_hash == model.model_hash
    assert len(first.graph_hash) == 64
    assert len(first.nodes) == len(model.objects)
    assert len(first.edges) == len(model.relationships)
    assert fr_002 is not None
    assert fr_002.name == "Compile manifests into canonical semantic project models"
    assert _has_edge(first, "REQUIRES_CAPABILITY", "requirement:FR-002", "capability:C16")


def test_nfr_002_traceability_projects_source_intent_to_evidence() -> None:
    first = compile_knowledge_graph(MANIFEST_ROOT)
    second = compile_knowledge_graph(MANIFEST_ROOT)
    trace = first.trace_for_requirement("NFR-002")
    relationship_types = {edge.edge_type for edge in trace.edges}
    target_nodes = {edge.target_node_id for edge in trace.edges}

    assert trace.as_dict() == second.trace_for_requirement("NFR-002").as_dict()
    assert {
        "SOURCED_FROM",
        "REQUIRES_CAPABILITY",
        "VERIFIED_BY",
        "EVIDENCED_BY",
    } <= relationship_types
    assert "source:R22" in target_nodes
    assert "capability:C16" in target_nodes
    assert "verification_gate:VG-004" in target_nodes
    assert "evidence:BEAG-001:guarded-autonomous-runner" in target_nodes


def _has_edge(
    graph: KnowledgeGraph,
    edge_type: str,
    source_node_id: str,
    target_node_id: str,
) -> bool:
    return any(
        edge.edge_type == edge_type
        and edge.source_node_id == source_node_id
        and edge.target_node_id == target_node_id
        for edge in graph.edges
    )
