from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ai_ent.canonical_project_model import (
    CanonicalObjectType,
    CanonicalProjectModel,
    CanonicalProjectModelService,
    CanonicalRelationshipType,
)
from ai_ent.project_manifest import PROJECT_MANIFEST_ROOT, canonical_bytes

KNOWLEDGE_GRAPH_SCHEMA_VERSION = "knowledge-graph-v0.1"
KNOWLEDGE_GRAPH_CONTRACT_VERSION = "c16.1"

EdgeDirection = Literal["incoming", "outgoing", "both"]


@dataclass(frozen=True)
class KnowledgeGraphNode:
    node_id: str
    node_type: CanonicalObjectType
    native_id: str
    name: str
    classification: str | None
    source_refs: tuple[str, ...]
    capability_refs: tuple[str, ...]
    source_manifest_file: str | None
    canonical_payload_hash: str
    in_degree: int
    out_degree: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_kind": "knowledge_graph_node",
            "node_id": self.node_id,
            "node_type": self.node_type,
            "native_id": self.native_id,
            "name": self.name,
            "classification": self.classification,
            "source_refs": list(self.source_refs),
            "capability_refs": list(self.capability_refs),
            "source_manifest_file": self.source_manifest_file,
            "canonical_payload_hash": self.canonical_payload_hash,
            "in_degree": self.in_degree,
            "out_degree": self.out_degree,
        }


@dataclass(frozen=True)
class KnowledgeGraphEdge:
    edge_id: str
    edge_type: CanonicalRelationshipType
    source_node_id: str
    target_node_id: str
    source_native_id: str
    target_native_id: str
    provenance_refs: tuple[str, ...]
    canonical_payload_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_kind": "knowledge_graph_edge",
            "edge_id": self.edge_id,
            "edge_type": self.edge_type,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "source_native_id": self.source_native_id,
            "target_native_id": self.target_native_id,
            "provenance_refs": list(self.provenance_refs),
            "canonical_payload_hash": self.canonical_payload_hash,
        }


@dataclass(frozen=True)
class KnowledgeGraphTrace:
    requirement_id: str
    requirement_node_id: str
    graph_hash: str
    nodes: tuple[KnowledgeGraphNode, ...]
    edges: tuple[KnowledgeGraphEdge, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "requirement_node_id": self.requirement_node_id,
            "graph_hash": self.graph_hash,
            "nodes": [node.as_dict() for node in self.nodes],
            "edges": [edge.as_dict() for edge in self.edges],
            "summary": {
                "node_count": len(self.nodes),
                "edge_count": len(self.edges),
                "edge_types": sorted({edge.edge_type for edge in self.edges}),
                "target_node_ids": sorted({edge.target_node_id for edge in self.edges}),
            },
        }


@dataclass(frozen=True)
class KnowledgeGraph:
    graph_id: str
    schema_version: str
    contract_version: str
    source_compiled_hash: str
    source_model_hash: str
    graph_hash: str
    nodes: tuple[KnowledgeGraphNode, ...]
    edges: tuple[KnowledgeGraphEdge, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "schema_version": self.schema_version,
            "contract_version": self.contract_version,
            "source_compiled_hash": self.source_compiled_hash,
            "source_model_hash": self.source_model_hash,
            "graph_hash": self.graph_hash,
            "nodes": [node.as_dict() for node in self.nodes],
            "edges": [edge.as_dict() for edge in self.edges],
            "summary": {
                "node_count": len(self.nodes),
                "edge_count": len(self.edges),
                "node_types": sorted({node.node_type for node in self.nodes}),
                "edge_types": sorted({edge.edge_type for edge in self.edges}),
            },
        }

    def as_storage_records(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_version": self.contract_version,
            "graph_id": self.graph_id,
            "graph_hash": self.graph_hash,
            "source_compiled_hash": self.source_compiled_hash,
            "source_model_hash": self.source_model_hash,
            "knowledge_graph_nodes": [node.as_dict() for node in self.nodes],
            "knowledge_graph_edges": [edge.as_dict() for edge in self.edges],
        }

    def node_by_id(self, node_id: str) -> KnowledgeGraphNode | None:
        graph_node_id = _node_id_or_native(node_id, self.nodes)
        return next((node for node in self.nodes if node.node_id == graph_node_id), None)

    def edges_for(
        self,
        node_id: str,
        *,
        direction: EdgeDirection = "both",
    ) -> tuple[KnowledgeGraphEdge, ...]:
        graph_node_id = _node_id_or_native(node_id, self.nodes)
        if direction == "incoming":
            return tuple(edge for edge in self.edges if edge.target_node_id == graph_node_id)
        if direction == "outgoing":
            return tuple(edge for edge in self.edges if edge.source_node_id == graph_node_id)
        return tuple(
            edge
            for edge in self.edges
            if edge.source_node_id == graph_node_id or edge.target_node_id == graph_node_id
        )

    def trace_for_requirement(self, requirement_id: str) -> KnowledgeGraphTrace:
        requirement_node_id = (
            requirement_id
            if requirement_id.startswith("requirement:")
            else f"requirement:{requirement_id}"
        )
        edges = self.edges_for(requirement_node_id, direction="outgoing")
        node_ids = {requirement_node_id, *(edge.target_node_id for edge in edges)}
        nodes = tuple(node for node in self.nodes if node.node_id in node_ids)
        return KnowledgeGraphTrace(
            requirement_id=requirement_node_id.removeprefix("requirement:"),
            requirement_node_id=requirement_node_id,
            graph_hash=self.graph_hash,
            nodes=nodes,
            edges=edges,
        )


class KnowledgeGraphService:
    """Service boundary for the C16 Knowledge Graph contract."""

    def __init__(
        self,
        *,
        schema_version: str = KNOWLEDGE_GRAPH_SCHEMA_VERSION,
        contract_version: str = KNOWLEDGE_GRAPH_CONTRACT_VERSION,
    ) -> None:
        self.schema_version = schema_version
        self.contract_version = contract_version

    def build(self, root: Path = PROJECT_MANIFEST_ROOT) -> KnowledgeGraph:
        model = CanonicalProjectModelService().compile(root)
        return self.from_model(model)

    def from_model(self, model: CanonicalProjectModel) -> KnowledgeGraph:
        return build_knowledge_graph(
            model,
            schema_version=self.schema_version,
            contract_version=self.contract_version,
        )


def build_knowledge_graph(
    model: CanonicalProjectModel,
    *,
    schema_version: str = KNOWLEDGE_GRAPH_SCHEMA_VERSION,
    contract_version: str = KNOWLEDGE_GRAPH_CONTRACT_VERSION,
) -> KnowledgeGraph:
    in_degrees: dict[str, int] = {item.object_id: 0 for item in model.objects}
    out_degrees: dict[str, int] = {item.object_id: 0 for item in model.objects}
    for relationship in model.relationships:
        out_degrees[relationship.source_object_id] += 1
        in_degrees[relationship.target_object_id] += 1

    nodes = tuple(
        KnowledgeGraphNode(
            node_id=item.object_id,
            node_type=item.object_type,
            native_id=item.native_id,
            name=item.name,
            classification=item.classification,
            source_refs=item.source_refs,
            capability_refs=item.capability_refs,
            source_manifest_file=item.source_manifest_file,
            canonical_payload_hash=item.payload_hash,
            in_degree=in_degrees[item.object_id],
            out_degree=out_degrees[item.object_id],
        )
        for item in sorted(model.objects, key=lambda value: value.object_id)
    )
    edges = tuple(
        KnowledgeGraphEdge(
            edge_id=relationship.relationship_id,
            edge_type=relationship.relationship_type,
            source_node_id=relationship.source_object_id,
            target_node_id=relationship.target_object_id,
            source_native_id=relationship.source_native_id,
            target_native_id=relationship.target_native_id,
            provenance_refs=relationship.provenance_refs,
            canonical_payload_hash=relationship.payload_hash,
        )
        for relationship in sorted(model.relationships, key=lambda value: value.relationship_id)
    )
    payload = {
        "schema_version": schema_version,
        "contract_version": contract_version,
        "source_compiled_hash": model.source_compiled_hash,
        "source_model_hash": model.model_hash,
        "nodes": [node.as_dict() for node in nodes],
        "edges": [edge.as_dict() for edge in edges],
    }
    graph_hash = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    return KnowledgeGraph(
        graph_id=f"KG-{graph_hash[:12]}",
        schema_version=schema_version,
        contract_version=contract_version,
        source_compiled_hash=model.source_compiled_hash,
        source_model_hash=model.model_hash,
        graph_hash=graph_hash,
        nodes=nodes,
        edges=edges,
    )


def compile_knowledge_graph(root: Path = PROJECT_MANIFEST_ROOT) -> KnowledgeGraph:
    return KnowledgeGraphService().build(root)


def _node_id_or_native(
    node_id: str,
    nodes: tuple[KnowledgeGraphNode, ...],
) -> str:
    known = {node.node_id for node in nodes}
    if node_id in known:
        return node_id
    matches = [node.node_id for node in nodes if node.native_id == node_id]
    if len(matches) == 1:
        return matches[0]
    return node_id
