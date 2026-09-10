from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.canonical_project_model import compile_canonical_project_model
from ai_ent.execution_planner import ExecutionPlannerService
from ai_ent.knowledge_graph import (
    ARTIFACT_EVIDENCE_GRAPH_CAPABILITY_ID,
    ARTIFACT_EVIDENCE_GRAPH_COMPONENT_ID,
    ARTIFACT_EVIDENCE_GRAPH_OUTPUT_AUTHORITY_STATE,
    ARTIFACT_EVIDENCE_GRAPH_REQUIREMENTS,
    ARTIFACT_EVIDENCE_GRAPH_SCHEMA_VERSION,
    ARTIFACT_EVIDENCE_GRAPH_SERVICE_NAME,
    KNOWLEDGE_GRAPH_CONTRACT_VERSION,
    KNOWLEDGE_GRAPH_SCHEMA_VERSION,
    ArtifactEvidenceGraph,
    ArtifactEvidenceGraphService,
    KnowledgeGraph,
    KnowledgeGraphService,
    compile_knowledge_graph,
)
from ai_ent.persistence.models import Base, Checkpoint, Execution, RuntimeHumanGate, TaskLease
from ai_ent.project_manifest import GENERATED_MARKER, EnvironmentProfile
from ai_ent.project_memory import EVIDENCE_RECORDING_INTERFACE_ID, ProjectMemoryService

MANIFEST_ROOT = Path("manifest/project/ai-ent")
PROJECT_ID = "PRJ-AI-ENT"
CMP_007_TASK_ID = "IMPL-C20-CMP-007"
CMP_007_EXECUTION_ID = "exec-IMPL-C20-CMP-007-001"


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


def test_cmp_007_artifact_evidence_graph_exposes_c20_contract_boundaries(
    tmp_path: Path,
) -> None:
    factory = session_factory()

    with factory() as session:
        _import_c20_plan(session, tmp_path)
        executions_before = session.scalar(select(func.count()).select_from(Execution))
        leases_before = session.scalar(select(func.count()).select_from(TaskLease))

        first = ArtifactEvidenceGraphService().build(
            session,
            project_id=PROJECT_ID,
            root=MANIFEST_ROOT,
        )
        second = ArtifactEvidenceGraphService().build(
            session,
            project_id=PROJECT_ID,
            root=MANIFEST_ROOT,
        )

        assert session.scalar(select(func.count()).select_from(Execution)) == executions_before
        assert session.scalar(select(func.count()).select_from(TaskLease)) == leases_before

    payload = first.as_dict()
    boundaries = {boundary.service: boundary.interface_id for boundary in first.service_boundaries}

    assert first.as_dict() == second.as_dict()
    assert first.evidence_graph_hash == second.evidence_graph_hash
    assert first.contract_version == "c20.1"
    assert first.schema_version == ARTIFACT_EVIDENCE_GRAPH_SCHEMA_VERSION
    assert first.requirements_covered == ARTIFACT_EVIDENCE_GRAPH_REQUIREMENTS
    assert first.capabilities_covered == (ARTIFACT_EVIDENCE_GRAPH_CAPABILITY_ID,)
    assert payload["generated"] == GENERATED_MARKER
    assert payload["component_id"] == ARTIFACT_EVIDENCE_GRAPH_COMPONENT_ID
    assert payload["service"] == ARTIFACT_EVIDENCE_GRAPH_SERVICE_NAME
    assert payload["output_boundary"]["interface_id"] == EVIDENCE_RECORDING_INTERFACE_ID
    assert (
        payload["output_boundary"]["output_authority_state"]
        == ARTIFACT_EVIDENCE_GRAPH_OUTPUT_AUTHORITY_STATE
    )
    assert {
        "canonical-knowledge-graph",
        "runtime-kernel",
        "project-memory",
        "deterministic-validator",
        "artifact-evidence-graph",
    } <= set(boundaries)
    assert payload["summary"]["requirements_covered"] == list(ARTIFACT_EVIDENCE_GRAPH_REQUIREMENTS)


def test_cmp_007_artifact_evidence_graph_links_runtime_artifacts_to_requirements(
    tmp_path: Path,
) -> None:
    factory = session_factory()
    memory = ProjectMemoryService()

    with factory() as session:
        _import_c20_plan(session, tmp_path)
        gate = session.get(RuntimeHumanGate, f"GATE-{CMP_007_TASK_ID}")
        assert gate is not None
        gate.status = "approved"
        session.add(
            Execution(
                id=CMP_007_EXECUTION_ID,
                task_id=CMP_007_TASK_ID,
                executor_type="codex",
                status="succeeded",
                attempt=1,
                terminal_state="success",
                commit_hash="a" * 40,
                candidate_tree_hash="b" * 40,
            )
        )
        session.add(
            Checkpoint(
                id="checkpoint-cmp-007-verification",
                task_id=CMP_007_TASK_ID,
                execution_id=CMP_007_EXECUTION_ID,
                checkpoint_type="execution",
                state='{"commands":["pytest","ruff","pyright"],"status":"passed"}',
                commit_hash="a" * 40,
                tree_hash="b" * 40,
            )
        )
        memory.remember_decision(
            session,
            project_id=PROJECT_ID,
            task_id=CMP_007_TASK_ID,
            decision="Record CMP-007 outputs as non-authoritative evidence graph artifacts",
            rationale="Runtime completion authority remains outside the evidence graph.",
            decided_by="ArtifactEvidenceGraphService",
        )
        session.commit()

        graph = ArtifactEvidenceGraphService(project_memory=memory).build(
            session,
            project_id=PROJECT_ID,
            root=MANIFEST_ROOT,
        )

    assert graph.node_by_id(f"task:{CMP_007_TASK_ID}") is not None
    assert graph.node_by_id(f"execution:{CMP_007_EXECUTION_ID}") is not None
    assert graph.node_by_id(f"human_gate:GATE-{CMP_007_TASK_ID}") is not None
    assert _has_artifact_edge(
        graph,
        "IMPLEMENTS_REQUIREMENT",
        f"task:{CMP_007_TASK_ID}",
        "requirement:FR-006",
    )
    assert _has_artifact_edge(
        graph,
        "EVIDENCES_REQUIREMENT",
        _artifact_for_source(graph, "execution-checkpoint"),
        "requirement:FR-006",
    )
    assert _has_artifact_edge(
        graph,
        "REQUIRES_HUMAN_GATE",
        f"task:{CMP_007_TASK_ID}",
        f"human_gate:GATE-{CMP_007_TASK_ID}",
    )
    assert _has_artifact_edge(
        graph,
        "EXECUTION_HAS_ARTIFACT",
        f"execution:{CMP_007_EXECUTION_ID}",
        _artifact_for_source(graph, "execution-checkpoint"),
    )

    trace = graph.trace_for_requirement("FR-006")
    assert f"task:{CMP_007_TASK_ID}" in {node.node_id for node in trace.nodes}
    assert _artifact_for_source(graph, "runtime-decision") in {node.node_id for node in trace.nodes}
    assert {"EVIDENCES_REQUIREMENT", "IMPLEMENTS_REQUIREMENT"} <= {
        edge.edge_type for edge in trace.edges
    }


def test_cmp_007_artifact_evidence_graph_covers_required_c20_requirements() -> None:
    assert set(ARTIFACT_EVIDENCE_GRAPH_REQUIREMENTS) == {
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
    }


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


def _has_artifact_edge(
    graph: ArtifactEvidenceGraph,
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


def _artifact_for_source(graph: ArtifactEvidenceGraph, source: str) -> str:
    matches = sorted(
        node.node_id
        for node in graph.nodes
        if node.node_type == "artifact_evidence_record" and node.source == source
    )
    assert matches
    return matches[0]


def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def _import_c20_plan(session: Session, tmp_path: Path) -> None:
    service = ExecutionPlannerService()
    plan = service.plan(MANIFEST_ROOT, environment=_environment(codex_configured=False))
    result = service.import_plan(
        session,
        plan,
        require_clean_git=False,
        require_codex_command=False,
        repository_root=tmp_path,
    )
    assert result.status == "IMPORTED"


def _environment(*, codex_configured: bool = True) -> EnvironmentProfile:
    return EnvironmentProfile(
        profile_id="test",
        repository_path="/repo",
        git_available=True,
        postgresql_available=True,
        alembic_available=True,
        docker_available=True,
        docker_compose_available=True,
        codex_command_configured=codex_configured,
        codex_executable_available=True,
        python_path="/repo/aient/bin/python",
        python_available=True,
        ram_mb=8192,
        gpu_available=False,
        external_network="available",
        configured_secret_names=(),
    )
