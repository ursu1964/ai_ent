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
from ai_ent.project_manifest import GENERATED_MARKER, PROJECT_MANIFEST_ROOT, canonical_bytes
from ai_ent.project_memory import (
    EVIDENCE_RECORDING_INTERFACE_ID,
    PROJECT_MEMORY_CONTRACT_VERSION,
    PROJECT_MEMORY_SCHEMA_VERSION,
    WORKER_PACKAGE_INTERFACE_ID,
    ProjectMemoryEntry,
    ProjectMemoryService,
    ProjectMemorySnapshot,
)

KNOWLEDGE_GRAPH_SCHEMA_VERSION = "knowledge-graph-v0.1"
KNOWLEDGE_GRAPH_CONTRACT_VERSION = "c16.1"
ARTIFACT_EVIDENCE_GRAPH_SCHEMA_VERSION = "artifact-evidence-graph-output-v0.1"
ARTIFACT_EVIDENCE_GRAPH_CONTRACT_VERSION = "c20.1"
ARTIFACT_EVIDENCE_GRAPH_SERVICE_NAME = "artifact-evidence-graph"
ARTIFACT_EVIDENCE_GRAPH_COMPONENT_ID = "CMP-007"
ARTIFACT_EVIDENCE_GRAPH_CAPABILITY_ID = "C20"
ARTIFACT_EVIDENCE_GRAPH_OUTPUT_AUTHORITY_STATE = "NON_AUTHORITATIVE"
ARTIFACT_EVIDENCE_GRAPH_REQUIREMENTS = (
    "ACC-001",
    "ACC-003",
    "DATA-002",
    "DATA-003",
    "FR-003",
    "FR-004",
    "FR-005",
    "FR-006",
    "NFR-002",
    "NFR-003",
    "NFR-004",
    "OPS-001",
    "OPS-002",
    "OPS-003",
    "SEC-001",
    "SEC-002",
    "SEC-003",
)

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
class ArtifactEvidenceBoundary:
    service: str
    interface_id: str
    contract_version: str
    schema_version: str

    def as_dict(self) -> dict[str, str]:
        return {
            "service": self.service,
            "interface_id": self.interface_id,
            "contract_version": self.contract_version,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ArtifactEvidenceNode:
    node_id: str
    node_type: str
    native_id: str
    name: str
    source: str
    payload_hash: str
    payload: dict[str, Any]
    classification: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_kind": "artifact_evidence_graph_node",
            "node_id": self.node_id,
            "node_type": self.node_type,
            "native_id": self.native_id,
            "name": self.name,
            "source": self.source,
            "classification": self.classification,
            "payload_hash": self.payload_hash,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class ArtifactEvidenceEdge:
    edge_id: str
    edge_type: str
    source_node_id: str
    target_node_id: str
    provenance_refs: tuple[str, ...]
    payload_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_kind": "artifact_evidence_graph_edge",
            "edge_id": self.edge_id,
            "edge_type": self.edge_type,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "provenance_refs": list(self.provenance_refs),
            "payload_hash": self.payload_hash,
        }


@dataclass(frozen=True)
class ArtifactEvidenceTrace:
    requirement_id: str
    requirement_node_id: str
    evidence_graph_hash: str
    nodes: tuple[ArtifactEvidenceNode, ...]
    edges: tuple[ArtifactEvidenceEdge, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "requirement_node_id": self.requirement_node_id,
            "evidence_graph_hash": self.evidence_graph_hash,
            "nodes": [node.as_dict() for node in self.nodes],
            "edges": [edge.as_dict() for edge in self.edges],
            "summary": {
                "node_count": len(self.nodes),
                "edge_count": len(self.edges),
                "edge_types": sorted({edge.edge_type for edge in self.edges}),
                "artifact_node_ids": sorted(
                    node.node_id for node in self.nodes if node.node_type == "artifact_evidence_record"
                ),
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


@dataclass(frozen=True)
class ArtifactEvidenceGraph:
    graph_id: str
    contract_version: str
    schema_version: str
    project_id: str
    source_knowledge_graph_hash: str
    source_project_memory_hash: str
    evidence_graph_hash: str
    requirements_covered: tuple[str, ...]
    capabilities_covered: tuple[str, ...]
    service_boundaries: tuple[ArtifactEvidenceBoundary, ...]
    nodes: tuple[ArtifactEvidenceNode, ...]
    edges: tuple[ArtifactEvidenceEdge, ...]

    @property
    def output_hash(self) -> str:
        return self.evidence_graph_hash

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "component_id": ARTIFACT_EVIDENCE_GRAPH_COMPONENT_ID,
            "capability_id": ARTIFACT_EVIDENCE_GRAPH_CAPABILITY_ID,
            "requirements": list(self.requirements_covered),
            "contract_version": self.contract_version,
            "schema_version": self.schema_version,
            "service": ARTIFACT_EVIDENCE_GRAPH_SERVICE_NAME,
            "graph_id": self.graph_id,
            "project_id": self.project_id,
            "source_knowledge_graph_hash": self.source_knowledge_graph_hash,
            "source_project_memory_hash": self.source_project_memory_hash,
            "evidence_graph_hash": self.evidence_graph_hash,
            "output_hash": self.output_hash,
            "input_boundaries": [
                boundary.as_dict()
                for boundary in self.service_boundaries
                if boundary.service != ARTIFACT_EVIDENCE_GRAPH_SERVICE_NAME
            ],
            "output_boundary": {
                "service": ARTIFACT_EVIDENCE_GRAPH_SERVICE_NAME,
                "interface_id": EVIDENCE_RECORDING_INTERFACE_ID,
                "output_authority_state": ARTIFACT_EVIDENCE_GRAPH_OUTPUT_AUTHORITY_STATE,
            },
            "service_boundaries": [boundary.as_dict() for boundary in self.service_boundaries],
            "nodes": [node.as_dict() for node in self.nodes],
            "edges": [edge.as_dict() for edge in self.edges],
            "summary": {
                "node_count": len(self.nodes),
                "edge_count": len(self.edges),
                "requirements_covered": list(self.requirements_covered),
                "capabilities_covered": list(self.capabilities_covered),
                "artifact_record_count": sum(
                    1 for node in self.nodes if node.node_type == "artifact_evidence_record"
                ),
                "runtime_task_count": sum(1 for node in self.nodes if node.node_type == "runtime_task"),
                "human_gate_count": sum(1 for node in self.nodes if node.node_type == "runtime_human_gate"),
                "output_authority_state": ARTIFACT_EVIDENCE_GRAPH_OUTPUT_AUTHORITY_STATE,
            },
        }

    def as_storage_records(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_version": self.contract_version,
            "graph_id": self.graph_id,
            "project_id": self.project_id,
            "evidence_graph_hash": self.evidence_graph_hash,
            "source_knowledge_graph_hash": self.source_knowledge_graph_hash,
            "source_project_memory_hash": self.source_project_memory_hash,
            "artifact_evidence_graph_nodes": [node.as_dict() for node in self.nodes],
            "artifact_evidence_graph_edges": [edge.as_dict() for edge in self.edges],
        }

    def node_by_id(self, node_id: str) -> ArtifactEvidenceNode | None:
        return next((node for node in self.nodes if node.node_id == node_id), None)

    def edges_for(
        self,
        node_id: str,
        *,
        direction: EdgeDirection = "both",
    ) -> tuple[ArtifactEvidenceEdge, ...]:
        if direction == "incoming":
            return tuple(edge for edge in self.edges if edge.target_node_id == node_id)
        if direction == "outgoing":
            return tuple(edge for edge in self.edges if edge.source_node_id == node_id)
        return tuple(
            edge
            for edge in self.edges
            if edge.source_node_id == node_id or edge.target_node_id == node_id
        )

    def trace_for_requirement(self, requirement_id: str) -> ArtifactEvidenceTrace:
        requirement_node_id = (
            requirement_id
            if requirement_id.startswith("requirement:")
            else f"requirement:{requirement_id}"
        )
        edge_ids: set[str] = set()
        node_ids = {requirement_node_id}
        frontier = {requirement_node_id}
        for _ in range(2):
            next_frontier: set[str] = set()
            for node_id in sorted(frontier):
                for edge in self.edges_for(node_id):
                    edge_ids.add(edge.edge_id)
                    for connected_node_id in (edge.source_node_id, edge.target_node_id):
                        if connected_node_id not in node_ids:
                            node_ids.add(connected_node_id)
                            next_frontier.add(connected_node_id)
            frontier = next_frontier
        nodes = tuple(node for node in self.nodes if node.node_id in node_ids)
        edges = tuple(edge for edge in self.edges if edge.edge_id in edge_ids)
        return ArtifactEvidenceTrace(
            requirement_id=requirement_node_id.removeprefix("requirement:"),
            requirement_node_id=requirement_node_id,
            evidence_graph_hash=self.evidence_graph_hash,
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


class ArtifactEvidenceGraphService:
    """Service boundary for the C20 Artifact Evidence Graph contract."""

    def __init__(
        self,
        *,
        knowledge_graphs: KnowledgeGraphService | None = None,
        project_memory: ProjectMemoryService | None = None,
        contract_version: str = ARTIFACT_EVIDENCE_GRAPH_CONTRACT_VERSION,
        schema_version: str = ARTIFACT_EVIDENCE_GRAPH_SCHEMA_VERSION,
    ) -> None:
        self.knowledge_graphs = knowledge_graphs or KnowledgeGraphService()
        self.project_memory = project_memory or ProjectMemoryService()
        self.contract_version = contract_version
        self.schema_version = schema_version

    def build(
        self,
        session: Any,
        *,
        project_id: str,
        root: Path = PROJECT_MANIFEST_ROOT,
        include_execution_evidence: bool = True,
    ) -> ArtifactEvidenceGraph:
        knowledge_graph = self.knowledge_graphs.build(root)
        project_memory = self.project_memory.snapshot(
            session,
            project_id=project_id,
            include_execution_evidence=include_execution_evidence,
        )
        return self.from_snapshots(
            knowledge_graph=knowledge_graph,
            project_memory=project_memory,
        )

    def from_snapshots(
        self,
        *,
        knowledge_graph: KnowledgeGraph,
        project_memory: ProjectMemorySnapshot,
    ) -> ArtifactEvidenceGraph:
        return build_artifact_evidence_graph(
            knowledge_graph=knowledge_graph,
            project_memory=project_memory,
            contract_version=self.contract_version,
            schema_version=self.schema_version,
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


def build_artifact_evidence_graph(
    *,
    knowledge_graph: KnowledgeGraph,
    project_memory: ProjectMemorySnapshot,
    contract_version: str = ARTIFACT_EVIDENCE_GRAPH_CONTRACT_VERSION,
    schema_version: str = ARTIFACT_EVIDENCE_GRAPH_SCHEMA_VERSION,
) -> ArtifactEvidenceGraph:
    nodes: dict[str, ArtifactEvidenceNode] = {}
    edges: dict[str, ArtifactEvidenceEdge] = {}
    project_node_id = _project_node_id(knowledge_graph, project_memory.project_id)
    task_requirements: dict[str, set[str]] = {}

    for node in knowledge_graph.nodes:
        _add_artifact_node(
            nodes,
            node_id=node.node_id,
            node_type=node.node_type,
            native_id=node.native_id,
            name=node.name,
            source="canonical-knowledge-graph",
            payload_hash=node.canonical_payload_hash,
            payload=node.as_dict(),
            classification=node.classification,
        )
    if project_node_id not in nodes:
        _add_artifact_node(
            nodes,
            node_id=project_node_id,
            node_type="runtime_project",
            native_id=project_memory.project_id,
            name=project_memory.project_id,
            source="project-memory",
            payload={
                "project_id": project_memory.project_id,
                "source_project_memory_hash": project_memory.memory_hash,
            },
        )

    for edge in knowledge_graph.edges:
        _add_artifact_edge(
            edges,
            edge_type=edge.edge_type,
            source_node_id=edge.source_node_id,
            target_node_id=edge.target_node_id,
            provenance_refs=edge.provenance_refs,
            payload={"source": "canonical-knowledge-graph", "canonical_edge_id": edge.edge_id},
        )

    for entry in project_memory.entries:
        _add_entry_nodes_and_edges(
            nodes,
            edges,
            project_node_id=project_node_id,
            entry=entry,
            task_requirements=task_requirements,
        )

    for entry in project_memory.entries:
        _add_requirement_edges_from_memory(
            nodes,
            edges,
            entry=entry,
            task_requirements=task_requirements,
        )

    service_boundaries = artifact_evidence_graph_service_boundaries()
    ordered_nodes = tuple(sorted(nodes.values(), key=lambda item: item.node_id))
    ordered_edges = tuple(sorted(edges.values(), key=lambda item: item.edge_id))
    hash_payload = {
        "contract_version": contract_version,
        "schema_version": schema_version,
        "project_id": project_memory.project_id,
        "source_knowledge_graph_hash": knowledge_graph.graph_hash,
        "source_project_memory_hash": project_memory.memory_hash,
        "requirements_covered": list(ARTIFACT_EVIDENCE_GRAPH_REQUIREMENTS),
        "capabilities_covered": [ARTIFACT_EVIDENCE_GRAPH_CAPABILITY_ID],
        "service_boundaries": [boundary.as_dict() for boundary in service_boundaries],
        "nodes": [node.as_dict() for node in ordered_nodes],
        "edges": [edge.as_dict() for edge in ordered_edges],
    }
    graph_hash = hashlib.sha256(canonical_bytes(hash_payload)).hexdigest()
    return ArtifactEvidenceGraph(
        graph_id=f"AEG-{graph_hash[:12]}",
        contract_version=contract_version,
        schema_version=schema_version,
        project_id=project_memory.project_id,
        source_knowledge_graph_hash=knowledge_graph.graph_hash,
        source_project_memory_hash=project_memory.memory_hash,
        evidence_graph_hash=graph_hash,
        requirements_covered=ARTIFACT_EVIDENCE_GRAPH_REQUIREMENTS,
        capabilities_covered=(ARTIFACT_EVIDENCE_GRAPH_CAPABILITY_ID,),
        service_boundaries=service_boundaries,
        nodes=ordered_nodes,
        edges=ordered_edges,
    )


def artifact_evidence_graph_service_boundaries() -> tuple[ArtifactEvidenceBoundary, ...]:
    return (
        ArtifactEvidenceBoundary(
            service="canonical-knowledge-graph",
            interface_id=EVIDENCE_RECORDING_INTERFACE_ID,
            contract_version=KNOWLEDGE_GRAPH_CONTRACT_VERSION,
            schema_version=KNOWLEDGE_GRAPH_SCHEMA_VERSION,
        ),
        ArtifactEvidenceBoundary(
            service="runtime-kernel",
            interface_id=WORKER_PACKAGE_INTERFACE_ID,
            contract_version=ARTIFACT_EVIDENCE_GRAPH_CONTRACT_VERSION,
            schema_version="runtime-kernel-output-v0.1",
        ),
        ArtifactEvidenceBoundary(
            service="project-memory",
            interface_id=EVIDENCE_RECORDING_INTERFACE_ID,
            contract_version=PROJECT_MEMORY_CONTRACT_VERSION,
            schema_version=PROJECT_MEMORY_SCHEMA_VERSION,
        ),
        ArtifactEvidenceBoundary(
            service=ARTIFACT_EVIDENCE_GRAPH_SERVICE_NAME,
            interface_id=EVIDENCE_RECORDING_INTERFACE_ID,
            contract_version=ARTIFACT_EVIDENCE_GRAPH_CONTRACT_VERSION,
            schema_version=ARTIFACT_EVIDENCE_GRAPH_SCHEMA_VERSION,
        ),
        ArtifactEvidenceBoundary(
            service="deterministic-validator",
            interface_id=EVIDENCE_RECORDING_INTERFACE_ID,
            contract_version="c03.1",
            schema_version="deterministic-validation-report-v0.1",
        ),
    )


def compile_knowledge_graph(root: Path = PROJECT_MANIFEST_ROOT) -> KnowledgeGraph:
    return KnowledgeGraphService().build(root)


def compile_artifact_evidence_graph(
    session: Any,
    *,
    project_id: str,
    root: Path = PROJECT_MANIFEST_ROOT,
    include_execution_evidence: bool = True,
) -> ArtifactEvidenceGraph:
    return ArtifactEvidenceGraphService().build(
        session,
        project_id=project_id,
        root=root,
        include_execution_evidence=include_execution_evidence,
    )


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


def _project_node_id(knowledge_graph: KnowledgeGraph, project_id: str) -> str:
    canonical_id = f"project:{project_id}"
    if knowledge_graph.node_by_id(canonical_id) is not None:
        return canonical_id
    return f"runtime_project:{project_id}"


def _add_entry_nodes_and_edges(
    nodes: dict[str, ArtifactEvidenceNode],
    edges: dict[str, ArtifactEvidenceEdge],
    *,
    project_node_id: str,
    entry: ProjectMemoryEntry,
    task_requirements: dict[str, set[str]],
) -> None:
    artifact_node_id = _artifact_node_id(entry)
    _add_artifact_node(
        nodes,
        node_id=artifact_node_id,
        node_type="artifact_evidence_record",
        native_id=entry.memory_id,
        name=entry.summary,
        source=entry.source,
        payload=entry.as_dict(),
    )
    _add_artifact_edge(
        edges,
        edge_type="RECORDS_ARTIFACT",
        source_node_id=project_node_id,
        target_node_id=artifact_node_id,
        provenance_refs=(entry.source,),
        payload={"memory_id": entry.memory_id, "source": entry.source},
    )
    if entry.task_id is not None:
        task_node_id = _task_node_id(entry.task_id)
        _add_artifact_node(
            nodes,
            node_id=task_node_id,
            node_type="runtime_task",
            native_id=entry.task_id,
            name=entry.task_id,
            source="runtime-handoff",
            payload={"project_id": entry.project_id, "task_id": entry.task_id},
        )
        _add_artifact_edge(
            edges,
            edge_type="TASK_HAS_ARTIFACT",
            source_node_id=task_node_id,
            target_node_id=artifact_node_id,
            provenance_refs=(entry.source,),
            payload={"memory_id": entry.memory_id, "task_id": entry.task_id},
        )
    if entry.execution_id is not None:
        execution_node_id = _execution_node_id(entry.execution_id)
        _add_artifact_node(
            nodes,
            node_id=execution_node_id,
            node_type="runtime_execution",
            native_id=entry.execution_id,
            name=entry.execution_id,
            source="runtime-persistence",
            payload={
                "project_id": entry.project_id,
                "task_id": entry.task_id,
                "execution_id": entry.execution_id,
            },
        )
        _add_artifact_edge(
            edges,
            edge_type="EXECUTION_HAS_ARTIFACT",
            source_node_id=execution_node_id,
            target_node_id=artifact_node_id,
            provenance_refs=(entry.source,),
            payload={"memory_id": entry.memory_id, "execution_id": entry.execution_id},
        )
        if entry.task_id is not None:
            _add_artifact_edge(
                edges,
                edge_type="TASK_HAS_EXECUTION",
                source_node_id=_task_node_id(entry.task_id),
                target_node_id=execution_node_id,
                provenance_refs=(entry.source,),
                payload={"task_id": entry.task_id, "execution_id": entry.execution_id},
            )
    if entry.source == "runtime-task-binding" and entry.task_id is not None:
        _add_binding_edges(
            nodes,
            edges,
            entry=entry,
            task_node_id=_task_node_id(entry.task_id),
            artifact_node_id=artifact_node_id,
            task_requirements=task_requirements,
        )
    if entry.source == "runtime-human-gate" and entry.task_id is not None:
        _add_human_gate_edges(
            nodes,
            edges,
            entry=entry,
            task_node_id=_task_node_id(entry.task_id),
            artifact_node_id=artifact_node_id,
        )


def _add_binding_edges(
    nodes: dict[str, ArtifactEvidenceNode],
    edges: dict[str, ArtifactEvidenceEdge],
    *,
    entry: ProjectMemoryEntry,
    task_node_id: str,
    artifact_node_id: str,
    task_requirements: dict[str, set[str]],
) -> None:
    implements = entry.payload.get("implements", {})
    if not isinstance(implements, dict):
        return
    for requirement_id in _string_items(implements.get("requirements")):
        requirement_node_id = f"requirement:{requirement_id}"
        task_requirements.setdefault(entry.task_id or "", set()).add(requirement_id)
        _add_artifact_edge(
            edges,
            edge_type="IMPLEMENTS_REQUIREMENT",
            source_node_id=task_node_id,
            target_node_id=requirement_node_id,
            provenance_refs=(entry.memory_id,),
            payload={"task_id": entry.task_id, "requirement_id": requirement_id},
        )
        _add_artifact_edge(
            edges,
            edge_type="EVIDENCES_REQUIREMENT",
            source_node_id=artifact_node_id,
            target_node_id=requirement_node_id,
            provenance_refs=(entry.memory_id,),
            payload={"memory_id": entry.memory_id, "requirement_id": requirement_id},
        )
    for capability_id in _string_items(implements.get("capabilities")):
        _add_artifact_edge(
            edges,
            edge_type="IMPLEMENTS_CAPABILITY",
            source_node_id=task_node_id,
            target_node_id=f"capability:{capability_id}",
            provenance_refs=(entry.memory_id,),
            payload={"task_id": entry.task_id, "capability_id": capability_id},
        )
    for component_id in _string_items(implements.get("components")):
        _add_artifact_edge(
            edges,
            edge_type="IMPLEMENTS_COMPONENT",
            source_node_id=task_node_id,
            target_node_id=f"component:{component_id}",
            provenance_refs=(entry.memory_id,),
            payload={"task_id": entry.task_id, "component_id": component_id},
        )
    for interface_id in _string_items(implements.get("interfaces")):
        _add_artifact_edge(
            edges,
            edge_type="EXERCISES_INTERFACE",
            source_node_id=task_node_id,
            target_node_id=f"interface:{interface_id}",
            provenance_refs=(entry.memory_id,),
            payload={"task_id": entry.task_id, "interface_id": interface_id},
        )
    _ = nodes


def _add_human_gate_edges(
    nodes: dict[str, ArtifactEvidenceNode],
    edges: dict[str, ArtifactEvidenceEdge],
    *,
    entry: ProjectMemoryEntry,
    task_node_id: str,
    artifact_node_id: str,
) -> None:
    gate_id = entry.payload.get("gate_id")
    if not isinstance(gate_id, str) or not gate_id:
        return
    gate_node_id = _human_gate_node_id(gate_id)
    _add_artifact_node(
        nodes,
        node_id=gate_node_id,
        node_type="runtime_human_gate",
        native_id=gate_id,
        name=gate_id,
        source="runtime-handoff",
        payload=entry.payload,
    )
    _add_artifact_edge(
        edges,
        edge_type="REQUIRES_HUMAN_GATE",
        source_node_id=task_node_id,
        target_node_id=gate_node_id,
        provenance_refs=(entry.memory_id,),
        payload={"task_id": entry.task_id, "gate_id": gate_id},
    )
    _add_artifact_edge(
        edges,
        edge_type="GATE_HAS_ARTIFACT",
        source_node_id=gate_node_id,
        target_node_id=artifact_node_id,
        provenance_refs=(entry.memory_id,),
        payload={"gate_id": gate_id, "memory_id": entry.memory_id},
    )


def _add_requirement_edges_from_memory(
    nodes: dict[str, ArtifactEvidenceNode],
    edges: dict[str, ArtifactEvidenceEdge],
    *,
    entry: ProjectMemoryEntry,
    task_requirements: dict[str, set[str]],
) -> None:
    if entry.task_id is None:
        return
    requirement_ids = task_requirements.get(entry.task_id, set())
    artifact_node_id = _artifact_node_id(entry)
    for requirement_id in sorted(requirement_ids):
        _add_artifact_edge(
            edges,
            edge_type="EVIDENCES_REQUIREMENT",
            source_node_id=artifact_node_id,
            target_node_id=f"requirement:{requirement_id}",
            provenance_refs=(entry.memory_id,),
            payload={"memory_id": entry.memory_id, "requirement_id": requirement_id},
        )
    if entry.source == "runtime-human-gate":
        gate_id = entry.payload.get("gate_id")
        if isinstance(gate_id, str) and gate_id:
            for requirement_id in sorted(requirement_ids):
                _add_artifact_edge(
                    edges,
                    edge_type="GATES_REQUIREMENT",
                    source_node_id=_human_gate_node_id(gate_id),
                    target_node_id=f"requirement:{requirement_id}",
                    provenance_refs=(entry.memory_id,),
                    payload={"gate_id": gate_id, "requirement_id": requirement_id},
                )
    _ = nodes


def _add_artifact_node(
    nodes: dict[str, ArtifactEvidenceNode],
    *,
    node_id: str,
    node_type: str,
    native_id: str,
    name: str,
    source: str,
    payload: dict[str, Any],
    payload_hash: str | None = None,
    classification: str | None = None,
) -> None:
    if node_id in nodes:
        return
    nodes[node_id] = ArtifactEvidenceNode(
        node_id=node_id,
        node_type=node_type,
        native_id=native_id,
        name=name,
        source=source,
        payload_hash=payload_hash or hashlib.sha256(canonical_bytes(payload)).hexdigest(),
        payload=payload,
        classification=classification,
    )


def _add_artifact_edge(
    edges: dict[str, ArtifactEvidenceEdge],
    *,
    edge_type: str,
    source_node_id: str,
    target_node_id: str,
    provenance_refs: tuple[str, ...],
    payload: dict[str, Any],
) -> None:
    normalized_payload = {
        "edge_type": edge_type,
        "source_node_id": source_node_id,
        "target_node_id": target_node_id,
        "provenance_refs": sorted(provenance_refs),
        **payload,
    }
    edge_hash = hashlib.sha256(canonical_bytes(normalized_payload)).hexdigest()
    edge_id = f"artifact-edge:{edge_hash[:32]}"
    if edge_id in edges:
        return
    edges[edge_id] = ArtifactEvidenceEdge(
        edge_id=edge_id,
        edge_type=edge_type,
        source_node_id=source_node_id,
        target_node_id=target_node_id,
        provenance_refs=tuple(sorted(provenance_refs)),
        payload_hash=hashlib.sha256(canonical_bytes(payload)).hexdigest(),
    )


def _artifact_node_id(entry: ProjectMemoryEntry) -> str:
    return f"artifact:{entry.memory_id}"


def _task_node_id(task_id: str) -> str:
    return f"task:{task_id}"


def _execution_node_id(execution_id: str) -> str:
    return f"execution:{execution_id}"


def _human_gate_node_id(gate_id: str) -> str:
    return f"human_gate:{gate_id}"


def _string_items(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(sorted(str(item) for item in value))
    return ()
