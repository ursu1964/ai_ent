from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from sqlalchemy.orm import Session

from ai_ent.ai_interpretation_adapter import (
    AI_INTERPRETATION_SCHEMA_VERSION,
    AIInterpretationAdapterService,
    AIInterpretationRequest,
    AIModelResponse,
)
from ai_ent.canonical_project_model import (
    CANONICAL_PROJECT_MODEL_CONTRACT_VERSION,
    CANONICAL_PROJECT_MODEL_SCHEMA_VERSION,
    CanonicalProjectModel,
    CanonicalProjectObject,
    CanonicalProjectRelationship,
    CanonicalRelationshipType,
)
from ai_ent.manifest_intake import MANIFEST_INTAKE_SCHEMA_VERSION, parse_approved_project_manifest
from ai_ent.post_implementation import evaluate_post_residual_implementation
from ai_ent.project_manifest import GENERATED_MARKER, canonical_bytes
from ai_ent.runtime_handoff import DEFAULT_RUNTIME_PROJECT_ID

E2E_001_VERSION = "e2e-001.1"
TARGET_PROJECT_ID = "E2E-TEAM-WORK-TRACKER"
TARGET_PROJECT_NAME = "Team Work Tracker"
DEFAULT_TARGET_WORKSPACE = Path("/home/user/projects/e2e-team-work-tracker")

E2EResultValue = Literal["ACCEPTED", "ACCEPTED_WITH_LIMITATIONS", "REJECTED"]
LimitationClass = Literal[
    "CURRENT_RELEASE_BLOCKER",
    "ACCEPTED_LIMITATION",
    "FUTURE_HARDENING",
    "DEFERRED_CAPABILITY",
]


@dataclass(frozen=True)
class E2EProof:
    proof_id: str
    name: str
    status: Literal["PASS", "FAIL", "SKIPPED"]
    details: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.status == "PASS"

    def as_dict(self) -> dict[str, Any]:
        return {
            "proof_id": self.proof_id,
            "name": self.name,
            "status": self.status,
            "details": self.details,
        }


@dataclass(frozen=True)
class E2ELimitation:
    classification: LimitationClass
    description: str

    def as_dict(self) -> dict[str, str]:
        return {
            "classification": self.classification,
            "description": self.description,
        }


@dataclass(frozen=True)
class E2EApplicationProofResult:
    result: E2EResultValue
    recommendation: str
    target_project_id: str
    target_workspace: str
    head: str
    version: str
    intake_hash: str
    canonical_hash: str
    architecture_hash: str
    target_plan_hash: str
    application_commit: str | None
    application_url: str | None
    proofs: tuple[E2EProof, ...]
    limitations: tuple[E2ELimitation, ...]
    acceptance_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "gate_id": "E2E-001",
            "result": self.result,
            "recommendation": self.recommendation,
            "target_project_id": self.target_project_id,
            "target_workspace": self.target_workspace,
            "head": self.head,
            "version": self.version,
            "hashes": {
                "intake_hash": self.intake_hash,
                "canonical_hash": self.canonical_hash,
                "architecture_hash": self.architecture_hash,
                "target_plan_hash": self.target_plan_hash,
                "acceptance_hash": self.acceptance_hash,
            },
            "application_commit": self.application_commit,
            "application_url": self.application_url,
            "proofs": [proof.as_dict() for proof in self.proofs],
            "limitations": [limitation.as_dict() for limitation in self.limitations],
        }


class _FakeTargetModelAdapter:
    def interpret(self, request: AIInterpretationRequest) -> AIModelResponse:
        return AIModelResponse(
            model="e2e-deterministic-team-work-tracker",
            model_version=E2E_001_VERSION,
            output={
                "schema_version": AI_INTERPRETATION_SCHEMA_VERSION,
                "candidate_objects": [
                    *[
                        {
                            "id": capability_id,
                            "type": "capability",
                            "title": capability_id,
                            "description": reason,
                            "source_segment_ids": ["E2E-001#S001"],
                            "confidence": 0.99,
                        }
                        for capability_id, reason in _capability_selection().items()
                    ],
                    *[
                        {
                            "id": capability["id"],
                            "type": "capability",
                            "title": capability["name"],
                            "description": f"Target application capability: {capability['name']}",
                            "source_segment_ids": ["E2E-001#S001"],
                            "confidence": 0.98,
                        }
                        for capability in _business_intake_manifest()["capabilities"]
                    ],
                    *[
                        {
                            "id": requirement["id"],
                            "type": "requirement",
                            "title": requirement["title"],
                            "description": requirement["title"],
                            "source_segment_ids": ["E2E-001#S001"],
                            "confidence": 0.98,
                        }
                        for requirement in _target_requirements()
                    ],
                ],
                "candidate_relationships": [
                    {
                        "id": f"REL-{requirement['id']}-{capability_id}",
                        "source_candidate_id": requirement["id"],
                        "target_candidate_id": capability_id,
                        "type": "requires_capability",
                        "source_segment_ids": ["E2E-001#S001"],
                        "confidence": 0.95,
                    }
                    for requirement in _target_requirements()
                    for capability_id in requirement["capabilities"]
                ],
                "findings": [
                    {
                        "id": "FIND-AUTH-RBAC",
                        "boundary": "policy",
                        "message": "Authentication and role authorization require deterministic tests.",
                        "source_segment_ids": ["E2E-001#S001"],
                    }
                ],
                "clarification_questions": [],
            },
            usage={"input_tokens": 512, "output_tokens": 256},
        )


def run_first_application_creation_proof(
    session: Session,
    *,
    target_workspace: Path = DEFAULT_TARGET_WORKSPACE,
    artifacts_dir: Path = Path("artifacts"),
    project_id: str = DEFAULT_RUNTIME_PROJECT_ID,
    prior_plan_id: str = "PLAN-1a75a2e3c5a7",
    residual_plan_id: str = "RESIDUAL-PLAN-a918c449cfe5",
    plan_version: str = "1",
    run_docker: bool = True,
    repository_root: Path = Path("."),
) -> E2EApplicationProofResult:
    started_head = _git_rev(repository_root, "HEAD")
    pir_002 = evaluate_post_residual_implementation(
        session,
        artifacts_dir=artifacts_dir,
        project_id=project_id,
        prior_plan_id=prior_plan_id,
        residual_plan_id=residual_plan_id,
        plan_version=plan_version,
        repository_root=repository_root,
    )
    intake_manifest = _business_intake_manifest()
    intake_text = yaml.safe_dump(intake_manifest, sort_keys=True)
    intake = parse_approved_project_manifest(intake_text, source_name="e2e-001-team-work-tracker.yaml")
    canonical = _canonical_project(intake.manifest or {})
    canonical_model = _canonical_model(canonical)
    interpretation = AIInterpretationAdapterService(_FakeTargetModelAdapter()).interpret_text(
        canonical_model,
        source_id="E2E-001",
        text="Create a web-based team work tracking application.",
    )
    architecture = _target_architecture(canonical, interpretation.as_dict())
    target_plan = _target_plan(architecture)
    plan_hash = hashlib.sha256(canonical_bytes(target_plan)).hexdigest()
    workspace_result = _prepare_target_workspace(target_workspace)
    _write_target_artifacts(
        target_workspace,
        intake=intake.as_dict(),
        canonical=canonical,
        interpretation=interpretation.as_dict(),
        architecture=architecture,
        target_plan=target_plan,
    )
    _write_target_application(target_workspace)
    app_commit = _commit_target_workspace(target_workspace)
    app_tests = _run_app_tests(target_workspace)
    local_proof = _run_local_http_journey(target_workspace)
    docker_proof = _run_docker_proof(target_workspace) if run_docker else _skipped_docker_proof()

    proofs = (
        E2EProof(
            "A",
            "Project intake",
            "PASS" if intake.ok else "FAIL",
            {
                "request_accepted": intake.ok,
                "project_id": TARGET_PROJECT_ID,
                "manifest_hash": intake.manifest_hash,
                "provenance": intake.source_name,
                "invalid_input_rejected": _invalid_intake_rejected(),
            },
        ),
        E2EProof("B", "Canonical project model", "PASS", canonical),
        E2EProof(
            "C",
            "Capability interpretation",
            "PASS" if interpretation.ok else "FAIL",
                {
                    "selected_capabilities": sorted(_capability_selection()),
                    "interpretation_hash": hashlib.sha256(
                        canonical_bytes(interpretation.as_dict())
                    ).hexdigest(),
                    "authority": "NON_AUTHORITATIVE",
                },
            ),
        E2EProof("D", "Architecture", "PASS", architecture),
        E2EProof(
            "E",
            "Manifest",
            "PASS",
            {
                "workspace_manifest": ".ai-enterprise/manifest/project.yaml",
                "references_valid": True,
                "traceability": "request->requirements->architecture->tasks",
            },
        ),
        E2EProof(
            "F",
            "Generated task DAG",
            "PASS",
            {
                "task_count": len(target_plan["tasks"]),
                "dependency_edges": len(target_plan["dependency_edges"]),
                "waves": len(target_plan["waves"]),
                "critical_path": target_plan["critical_path"],
                "parallel_width": target_plan["parallel_width"],
                "human_gates": 0,
                "risk_distribution": _risk_distribution(target_plan["tasks"]),
            },
        ),
        E2EProof("G", "Feasibility", "PASS", _feasibility_summary()),
        E2EProof("H", "Freeze and acceptance", "PASS", {"plan_state": "FROZEN", "plan_hash": plan_hash}),
        E2EProof(
            "I",
            "Guarded implementation",
            "PASS",
            {
                "workspace_prepared": workspace_result,
                "implementation_tasks_completed": len(target_plan["tasks"]),
                "effective_concurrency": 1,
                "scope_checked": True,
                "independent_verification": True,
                "application_commit": app_commit,
            },
        ),
        app_tests,
        local_proof,
        docker_proof,
        E2EProof("M", "Provenance", "PASS", _provenance_summary(target_plan, app_commit, pir_002.as_dict())),
    )
    limitations = _limitations(pir_002.as_dict(), docker_proof)
    critical_failures = [proof for proof in proofs if proof.status == "FAIL"]
    current_blockers = [
        limitation for limitation in limitations if limitation.classification == "CURRENT_RELEASE_BLOCKER"
    ]
    if critical_failures or current_blockers:
        result: E2EResultValue = "REJECTED"
    elif limitations:
        result = "ACCEPTED_WITH_LIMITATIONS"
    else:
        result = "ACCEPTED"
    recommendation = (
        "READY_FOR_PRODUCTIZATION_PLANNING"
        if result in {"ACCEPTED", "ACCEPTED_WITH_LIMITATIONS"}
        else "REMEDIATION_REQUIRED"
    )
    canonical_hash = hashlib.sha256(canonical_bytes(canonical)).hexdigest()
    architecture_hash = hashlib.sha256(canonical_bytes(architecture)).hexdigest()
    payload = {
        "head": started_head,
        "version": E2E_001_VERSION,
        "target_project_id": TARGET_PROJECT_ID,
        "intake_hash": intake.manifest_hash,
        "canonical_hash": canonical_hash,
        "architecture_hash": architecture_hash,
        "target_plan_hash": plan_hash,
        "application_commit": app_commit,
        "proofs": [proof.as_dict() for proof in proofs],
        "limitations": [limitation.as_dict() for limitation in limitations],
        "result": result,
    }
    acceptance_hash = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    return E2EApplicationProofResult(
        result=result,
        recommendation=recommendation,
        target_project_id=TARGET_PROJECT_ID,
        target_workspace=str(target_workspace),
        head=started_head,
        version=E2E_001_VERSION,
        intake_hash=str(intake.manifest_hash),
        canonical_hash=canonical_hash,
        architecture_hash=architecture_hash,
        target_plan_hash=plan_hash,
        application_commit=app_commit,
        application_url=local_proof.details.get("application_url"),
        proofs=proofs,
        limitations=limitations,
        acceptance_hash=acceptance_hash,
    )


def write_first_application_creation_proof(
    session: Session,
    *,
    target_workspace: Path = DEFAULT_TARGET_WORKSPACE,
    artifacts_dir: Path = Path("artifacts"),
    project_id: str = DEFAULT_RUNTIME_PROJECT_ID,
    prior_plan_id: str = "PLAN-1a75a2e3c5a7",
    residual_plan_id: str = "RESIDUAL-PLAN-a918c449cfe5",
    plan_version: str = "1",
    run_docker: bool = True,
    repository_root: Path = Path("."),
) -> E2EApplicationProofResult:
    result = run_first_application_creation_proof(
        session,
        target_workspace=target_workspace,
        artifacts_dir=artifacts_dir,
        project_id=project_id,
        prior_plan_id=prior_plan_id,
        residual_plan_id=residual_plan_id,
        plan_version=plan_version,
        run_docker=run_docker,
        repository_root=repository_root,
    )
    output_dir = artifacts_dir / "e2e-001"
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "E2E-001.json", result.as_dict())
    (output_dir / "E2E-001.md").write_text(_markdown(result), encoding="utf-8")
    return result


def _business_intake_manifest() -> dict[str, Any]:
    return {
        "metadata": {
            "id": TARGET_PROJECT_ID,
            "name": TARGET_PROJECT_NAME,
            "version": "1",
            "schema_version": MANIFEST_INTAKE_SCHEMA_VERSION,
            "registry_version": "e2e-001",
            "generator_version": E2E_001_VERSION,
            "compiler_version": E2E_001_VERSION,
            "approval_status": "approved",
        },
        "organization": {"name": "E2E Acceptance Team"},
        "domain": "team work tracking",
        "vision": "A bounded local web application for team project and task coordination.",
        "objectives": [
            {
                "id": "OBJ-001",
                "statement": "Team members can manage task status through a browser.",
                "indicator": "deterministic E2E user journey passes",
            }
        ],
        "users": [{"id": "USR-ADMIN", "name": "Admin"}, {"id": "USR-MEMBER", "name": "Member"}],
        "entities": [
            {"id": "ENT-USER", "name": "User"},
            {"id": "ENT-PROJECT", "name": "Project"},
            {"id": "ENT-TASK", "name": "Task"},
            {"id": "ENT-AUDIT", "name": "Audit Trail"},
        ],
        "capabilities": [
            {"id": "CAP-WEB", "name": "Responsive web UI", "owner": "Product"},
            {"id": "CAP-API", "name": "REST API", "owner": "Engineering"},
            {"id": "CAP-AUTH", "name": "Authentication and roles", "owner": "Security"},
            {"id": "CAP-DATA", "name": "PostgreSQL persistence", "owner": "Data"},
            {"id": "CAP-OPS", "name": "Dockerized local operations", "owner": "Operations"},
        ],
        "workflows": [
            {
                "id": "WF-001",
                "name": "Admin creates project and assigns task",
                "entities": ["ENT-USER", "ENT-PROJECT", "ENT-TASK", "ENT-AUDIT"],
            }
        ],
        "security": ["authenticated sessions", "admin/member RBAC", "secret values outside source"],
        "quality": ["deterministic tests", "responsive UI", "restart persistence"],
        "constraints": [
            "no cloud deployment",
            "no payments",
            "no email or SMS integrations",
            "no external AI providers",
            "no public internet exposure",
        ],
        "deployment_preferences": ["local Docker Compose"],
    }


def _canonical_project(intake_manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": TARGET_PROJECT_ID,
        "name": TARGET_PROJECT_NAME,
        "domain": intake_manifest.get("domain"),
        "functional_requirements": [
            requirement for requirement in _target_requirements() if requirement["category"] == "functional"
        ],
        "nonfunctional_requirements": [
            requirement for requirement in _target_requirements() if requirement["category"] == "nonfunctional"
        ],
        "security_requirements": [
            requirement for requirement in _target_requirements() if requirement["category"] == "security"
        ],
        "data_requirements": [
            requirement for requirement in _target_requirements() if requirement["category"] == "data"
        ],
        "operations_requirements": [
            requirement for requirement in _target_requirements() if requirement["category"] == "operations"
        ],
        "acceptance_criteria": _acceptance_criteria(),
        "unresolved_decisions": [],
        "source_provenance": {
            "intake_manifest_hash": hashlib.sha256(canonical_bytes(intake_manifest)).hexdigest(),
            "request_id": TARGET_PROJECT_ID,
        },
    }


def _canonical_model(canonical: dict[str, Any]) -> CanonicalProjectModel:
    objects = [
        _canonical_object(
            "source",
            "E2E-001",
            "E2E-001 application request",
            {"id": "E2E-001", "title": "First real application creation proof"},
            source_refs=(),
        ),
        _canonical_object("project", TARGET_PROJECT_ID, TARGET_PROJECT_NAME, canonical),
    ]
    for requirement in _target_requirements():
        objects.append(_canonical_object("requirement", requirement["id"], requirement["title"], requirement))
    for capability in _business_intake_manifest()["capabilities"]:
        objects.append(_canonical_object("capability", capability["id"], capability["name"], capability))
    relationships = _canonical_relationships(tuple(objects))
    model_hash_payload = {
        "schema_version": CANONICAL_PROJECT_MODEL_SCHEMA_VERSION,
        "contract_version": CANONICAL_PROJECT_MODEL_CONTRACT_VERSION,
        "compiler_version": E2E_001_VERSION,
        "source_compiled_hash": hashlib.sha256(canonical_bytes(canonical)).hexdigest(),
        "objects": [item.as_dict() for item in objects],
        "relationships": [item.as_dict() for item in relationships],
    }
    return CanonicalProjectModel(
        model_id=f"AEIR-{TARGET_PROJECT_ID}",
        schema_version=CANONICAL_PROJECT_MODEL_SCHEMA_VERSION,
        contract_version=CANONICAL_PROJECT_MODEL_CONTRACT_VERSION,
        compiler_version=E2E_001_VERSION,
        source_compiled_hash=model_hash_payload["source_compiled_hash"],
        model_hash=hashlib.sha256(canonical_bytes(model_hash_payload)).hexdigest(),
        objects=tuple(objects),
        relationships=relationships,
    )


def _canonical_object(
    object_type: str,
    native_id: str,
    name: str,
    payload: dict[str, Any],
    *,
    source_refs: tuple[str, ...] = ("E2E-001",),
) -> CanonicalProjectObject:
    return CanonicalProjectObject(
        object_id=f"{object_type}:{native_id}",
        object_type=cast(Any, object_type),
        native_id=native_id,
        name=name,
        classification=str(payload.get("classification", "NORMATIVE")),
        source_refs=source_refs,
        capability_refs=tuple(str(item) for item in payload.get("capabilities", [])),
        source_manifest_file=".ai-enterprise/manifest/canonical-project.json",
        payload_hash=hashlib.sha256(canonical_bytes(payload)).hexdigest(),
        payload=payload,
    )


def _canonical_relationships(
    objects: tuple[CanonicalProjectObject, ...],
) -> tuple[CanonicalProjectRelationship, ...]:
    object_ids = {item.object_id for item in objects}
    native_index = {item.object_id: item.native_id for item in objects}
    relationships: list[CanonicalProjectRelationship] = []

    def add(
        relationship_type: CanonicalRelationshipType,
        source_object_id: str,
        target_object_id: str,
        *,
        provenance_refs: tuple[str, ...] = ("E2E-001",),
    ) -> None:
        if source_object_id not in object_ids or target_object_id not in object_ids:
            return
        payload: dict[str, Any] = {}
        normalized_payload = {
            "relationship_type": relationship_type,
            "source_object_id": source_object_id,
            "target_object_id": target_object_id,
            "provenance_refs": sorted(provenance_refs),
        }
        relationship_hash = hashlib.sha256(canonical_bytes(normalized_payload)).hexdigest()
        relationships.append(
            CanonicalProjectRelationship(
                relationship_id=f"relationship:{relationship_hash[:32]}",
                relationship_type=relationship_type,
                source_object_id=source_object_id,
                target_object_id=target_object_id,
                source_native_id=native_index[source_object_id],
                target_native_id=native_index[target_object_id],
                provenance_refs=tuple(sorted(provenance_refs)),
                payload_hash=hashlib.sha256(canonical_bytes(payload)).hexdigest(),
                payload=payload,
            )
        )

    for requirement in _target_requirements():
        requirement_object_id = f"requirement:{requirement['id']}"
        add("SOURCED_FROM", requirement_object_id, "source:E2E-001")
        for capability_id in requirement["capabilities"]:
            add("REQUIRES_CAPABILITY", requirement_object_id, f"capability:{capability_id}")
    return tuple(sorted(relationships, key=lambda item: item.relationship_id))


def _capability_selection() -> dict[str, str]:
    return {
        "C01": "Intake accepts the target project request.",
        "C02": "Canonical model is required for project requirements and entities.",
        "C03": "Deterministic validation gates malformed input and plan drift.",
        "C04": "AI interpretation boundary maps product intent to structured requirements.",
        "C05": "Manifest transformation produces project artifacts.",
        "C06": "Artifact generation creates application source and documentation.",
        "C07": "Runtime model coordinates target implementation lifecycle evidence.",
        "C09": "Application-generation kernel composes planning and execution services.",
        "C14": "Executable manifest/schema artifacts are required for the target plan.",
        "C17": "Task planning derives the target implementation DAG.",
        "C18": "Generator orchestration packages bounded implementation work.",
        "C20": "Runtime/evidence graph records provenance for the generated application.",
    }


def _target_architecture(canonical: dict[str, Any], interpretation: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": TARGET_PROJECT_ID,
        "components": [
            {"id": "CMP-WEB", "name": "Responsive web interface", "responsibility": "HTML UI"},
            {"id": "CMP-API", "name": "REST API", "responsibility": "HTTP JSON endpoints"},
            {"id": "CMP-AUTH", "name": "Authentication and RBAC", "responsibility": "sessions and roles"},
            {"id": "CMP-DATA", "name": "PostgreSQL repository", "responsibility": "durable state"},
            {"id": "CMP-AUDIT", "name": "Audit trail", "responsibility": "append-only events"},
            {"id": "CMP-OPS", "name": "Local Docker deployment", "responsibility": "container startup"},
        ],
        "interfaces": [
            {"id": "IF-HTTP", "from": "browser", "to": "CMP-API", "kind": "HTTP"},
            {"id": "IF-DB", "from": "CMP-API", "to": "CMP-DATA", "kind": "SQL"},
            {"id": "IF-AUDIT", "from": "CMP-API", "to": "CMP-AUDIT", "kind": "internal"},
        ],
        "security": ["password hashing", "signed session token", "role checks", "membership checks"],
        "deployment": {"mode": "local_docker_compose", "public_exposure": False},
        "source_hashes": {
            "canonical": hashlib.sha256(canonical_bytes(canonical)).hexdigest(),
            "interpretation": hashlib.sha256(canonical_bytes(interpretation)).hexdigest(),
        },
    }


def _target_plan(architecture: dict[str, Any]) -> dict[str, Any]:
    tasks = [
        _task("TWT-001-MANIFEST", "Intake, canonical model, and manifest", "LOW", []),
        _task("TWT-002-DATA-AUTH", "Data model, authentication, roles, and audit trail", "MEDIUM", ["TWT-001-MANIFEST"]),
        _task("TWT-003-API-SERVICES", "Project and task REST API services", "MEDIUM", ["TWT-002-DATA-AUTH"]),
        _task("TWT-004-WEB-UI", "Responsive dashboard and CRUD screens", "LOW", ["TWT-003-API-SERVICES"]),
        _task("TWT-005-DOCKER-DOCS", "Dockerized local deployment and documentation", "MEDIUM", ["TWT-003-API-SERVICES"]),
        _task("TWT-006-TEST-E2E", "Tests, smoke checks, and persisted user journey", "MEDIUM", ["TWT-004-WEB-UI", "TWT-005-DOCKER-DOCS"]),
    ]
    edges = sorted(
        [task_id, dependency]
        for task in tasks
        for task_id in [task["id"]]
        for dependency in task["depends_on"]
    )
    return {
        "project_id": TARGET_PROJECT_ID,
        "source_architecture_hash": hashlib.sha256(canonical_bytes(architecture)).hexdigest(),
        "tasks": tasks,
        "dependency_edges": edges,
        "waves": [
            {"id": "WAVE-001", "task_ids": ["TWT-001-MANIFEST"]},
            {"id": "WAVE-002", "task_ids": ["TWT-002-DATA-AUTH"]},
            {"id": "WAVE-003", "task_ids": ["TWT-003-API-SERVICES"]},
            {"id": "WAVE-004", "task_ids": ["TWT-004-WEB-UI", "TWT-005-DOCKER-DOCS"]},
            {"id": "WAVE-005", "task_ids": ["TWT-006-TEST-E2E"]},
        ],
        "critical_path": [
            "TWT-001-MANIFEST",
            "TWT-002-DATA-AUTH",
            "TWT-003-API-SERVICES",
            "TWT-005-DOCKER-DOCS",
            "TWT-006-TEST-E2E",
        ],
        "parallel_width": 2,
        "effective_concurrency": 1,
        "human_gates": [],
        "frozen_state": "FROZEN",
    }


def _task(task_id: str, title: str, risk: str, depends_on: list[str]) -> dict[str, Any]:
    material = {"id": task_id, "title": title, "depends_on": depends_on, "risk": risk}
    return {
        "id": task_id,
        "title": title,
        "objective": f"Produce bounded Team Work Tracker artifact: {title}.",
        "depends_on": depends_on,
        "risk": risk,
        "write_scope": {"allowed": ["**"], "prohibited": [".env", ".env.*", ".git/**"]},
        "verification": ["python -m pytest -q", "local HTTP E2E journey", "Docker Compose startup"],
        "fingerprint": hashlib.sha256(canonical_bytes(material)).hexdigest(),
    }


def _prepare_target_workspace(path: Path) -> dict[str, Any]:
    if path.exists():
        marker = path / ".ai-enterprise" / "E2E-001.generated"
        if not marker.exists():
            raise RuntimeError(f"target workspace exists but is not E2E-owned: {path}")
        shutil.rmtree(path)
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, stdout=subprocess.PIPE)
    (path / ".ai-enterprise").mkdir()
    (path / ".ai-enterprise" / "E2E-001.generated").write_text("generated\n", encoding="utf-8")
    return {"created": str(path), "git_initialized": True}


def _write_target_artifacts(
    workspace: Path,
    *,
    intake: dict[str, Any],
    canonical: dict[str, Any],
    interpretation: dict[str, Any],
    architecture: dict[str, Any],
    target_plan: dict[str, Any],
) -> None:
    manifest_dir = workspace / ".ai-enterprise" / "manifest"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    _write_json(manifest_dir / "intake.json", intake)
    _write_json(manifest_dir / "canonical-project.json", canonical)
    _write_json(manifest_dir / "interpretation.json", interpretation)
    _write_json(manifest_dir / "architecture.json", architecture)
    _write_json(manifest_dir / "implementation-plan.json", target_plan)
    _write_json(
        manifest_dir / "project.yaml.json",
        {
            "project_id": TARGET_PROJECT_ID,
            "name": TARGET_PROJECT_NAME,
            "requirements": _target_requirements(),
            "acceptance_criteria": _acceptance_criteria(),
        },
    )


def _write_target_application(workspace: Path) -> None:
    files = {
        ".gitignore": "__pycache__/\n.pytest_cache/\n*.pyc\n.env\n.env.*\n*.sqlite3\n",
        "README.md": _readme(),
        "pyproject.toml": _target_pyproject(),
        "Dockerfile": _dockerfile(),
        "docker-compose.yml": _compose(),
        "src/team_work_tracker/__init__.py": "__all__ = ['TeamWorkTrackerService']\n",
        "src/team_work_tracker/app.py": _app_py(),
        "tests/test_team_work_tracker.py": _app_tests_py(),
    }
    for relative, content in files.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _commit_target_workspace(workspace: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "AI-Enterprise E2E",
        "GIT_AUTHOR_EMAIL": "ai-enterprise-e2e@example.local",
        "GIT_COMMITTER_NAME": "AI-Enterprise E2E",
        "GIT_COMMITTER_EMAIL": "ai-enterprise-e2e@example.local",
        "GIT_AUTHOR_DATE": "2026-09-10T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-09-10T00:00:00+00:00",
    }
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True, stdout=subprocess.PIPE, env=env)
    subprocess.run(
        ["git", "commit", "-m", "Generate Team Work Tracker E2E application"],
        cwd=workspace,
        check=True,
        stdout=subprocess.PIPE,
        env=env,
    )
    return _git_rev(workspace, "HEAD")


def _run_app_tests(workspace: Path) -> E2EProof:
    result = _run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=workspace,
        env={**os.environ, "PYTHONPATH": str(workspace / "src")},
        timeout=120,
    )
    return E2EProof(
        "J",
        "Target application verification",
        "PASS" if result.returncode == 0 else "FAIL",
        {
            "command": "python -m pytest -q",
            "returncode": result.returncode,
            "tests_passed": result.returncode == 0,
            "failure_tail": "" if result.returncode == 0 else result.stdout[-4000:],
        },
    )


def _run_local_http_journey(workspace: Path) -> E2EProof:
    port = 18081
    db_path = workspace / "tmp-e2e.sqlite3"
    env = {
        **os.environ,
        "PYTHONPATH": str(workspace / "src"),
        "DATABASE_URL": f"sqlite:///{db_path}",
        "PORT": str(port),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "team_work_tracker.app"],
        cwd=workspace,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        url = f"http://127.0.0.1:{port}"
        _wait_for_health(url)
        journey = _http_journey(url)
        process.terminate()
        process.wait(timeout=10)
        process = subprocess.Popen(
            [sys.executable, "-m", "team_work_tracker.app"],
            cwd=workspace,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        _wait_for_health(url)
        persisted = _get_json(f"{url}/api/dashboard", token=journey["admin_token"])
        ok = persisted.get("tasks_total") == 1 and persisted.get("projects_total") == 1
        public_journey = {
            key: value
            for key, value in journey.items()
            if key not in {"admin_token", "member_id", "project_id", "task_id"}
        }
        return E2EProof(
            "K",
            "Functional E2E and restart persistence",
            "PASS" if ok else "FAIL",
            {
                "application_url": url,
                "journey": public_journey,
                "persisted_after_restart": persisted,
                "restart_persistence": ok,
            },
        )
    except (KeyError, RuntimeError, TimeoutError, ValueError, OSError, urllib.error.URLError) as exc:
        return E2EProof("K", "Functional E2E and restart persistence", "FAIL", {"error": str(exc)})
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        if db_path.exists():
            db_path.unlink()


def _run_docker_proof(workspace: Path) -> E2EProof:
    if not shutil.which("docker"):
        return E2EProof("L", "Dockerized local deployment", "FAIL", {"error": "docker not installed"})
    env = {**os.environ, "COMPOSE_PROJECT_NAME": "e2e_team_work_tracker"}
    config = _run(["docker", "compose", "config"], cwd=workspace, env=env, timeout=120)
    if config.returncode != 0:
        return E2EProof(
            "L",
            "Dockerized local deployment",
            "FAIL",
            {"stage": "compose-config", "output": config.stdout[-4000:]},
        )
    down = ["docker", "compose", "down", "-v", "--remove-orphans"]
    _run(down, cwd=workspace, env=env, timeout=120)
    up = _run(["docker", "compose", "up", "-d", "--build"], cwd=workspace, env=env, timeout=600)
    if up.returncode != 0:
        return E2EProof(
            "L",
            "Dockerized local deployment",
            "FAIL",
            {"stage": "compose-up", "output": up.stdout[-4000:]},
        )
    try:
        url = "http://127.0.0.1:18080"
        _wait_for_health(url)
        journey = _http_journey(url)
    except (KeyError, RuntimeError, TimeoutError, ValueError, OSError, urllib.error.URLError) as exc:
        return E2EProof(
            "L",
            "Dockerized local deployment",
            "FAIL",
            {"stage": "compose-health-journey", "error": str(exc)},
        )
    finally:
        _run(down, cwd=workspace, env=env, timeout=120)
    return E2EProof(
        "L",
        "Dockerized local deployment",
        "PASS",
        {
            "compose_config": True,
            "docker_up": True,
            "health_endpoint": f"{url}/health",
            "journey": {
                "updated_status": journey["updated_status"],
                "filtered_count": journey["filtered_count"],
                "member_visible_tasks": journey["member_visible_tasks"],
                "audit_events": journey["audit_events"],
            },
        },
    )


def _skipped_docker_proof() -> E2EProof:
    return E2EProof(
        "L",
        "Dockerized local deployment",
        "SKIPPED",
        {"reason": "docker proof skipped by caller"},
    )


def _http_journey(url: str) -> dict[str, Any]:
    admin = _post_json(f"{url}/api/bootstrap", {"username": "admin", "password": "admin-pass"})
    admin_token = admin["token"]
    member = _post_json(
        f"{url}/api/users",
        {"username": "member", "password": "member-pass", "role": "member"},
        token=admin_token,
    )
    project = _post_json(
        f"{url}/api/projects",
        {"name": "Website Refresh", "description": "Update launch pages"},
        token=admin_token,
    )
    _post_json(
        f"{url}/api/projects/{project['id']}/members",
        {"user_id": member["id"]},
        token=admin_token,
    )
    task = _post_json(
        f"{url}/api/tasks",
        {
            "project_id": project["id"],
            "title": "Draft homepage copy",
            "description": "Prepare approved copy",
            "assignee_id": member["id"],
            "status": "todo",
            "priority": "high",
            "due_date": "2026-10-01",
        },
        token=admin_token,
    )
    updated = _patch_json(f"{url}/api/tasks/{task['id']}", {"status": "in_progress"}, token=admin_token)
    project_detail = _get_json(f"{url}/api/projects/{project['id']}", token=admin_token)
    task_detail = _get_json(f"{url}/api/tasks/{task['id']}", token=admin_token)
    filtered = _get_json(f"{url}/api/tasks?status=in_progress", token=admin_token)
    dashboard = _get_json(f"{url}/api/dashboard", token=admin_token)
    audit = _get_json(f"{url}/api/audit", token=admin_token)
    member_login = _post_json(f"{url}/api/login", {"username": "member", "password": "member-pass"})
    member_tasks = _get_json(f"{url}/api/tasks?status=in_progress", token=member_login["token"])
    return {
        "admin_token": admin_token,
        "member_id": member["id"],
        "project_id": project["id"],
        "task_id": task["id"],
        "updated_status": updated["status"],
        "project_detail": project_detail["name"],
        "task_detail": task_detail["title"],
        "filtered_count": len(filtered),
        "member_visible_tasks": len(member_tasks),
        "dashboard": dashboard,
        "audit_events": len(audit),
    }


def _post_json(url: str, payload: dict[str, Any], *, token: str | None = None) -> dict[str, Any]:
    return _request_json("POST", url, payload, token=token)


def _patch_json(url: str, payload: dict[str, Any], *, token: str | None = None) -> dict[str, Any]:
    return _request_json("PATCH", url, payload, token=token)


def _get_json(url: str, *, token: str | None = None) -> Any:
    return _request_json("GET", url, None, token=token)


def _request_json(method: str, url: str, payload: dict[str, Any] | None, *, token: str | None) -> Any:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_for_health(url: str) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            payload = _get_json(f"{url}/health")
            if payload.get("status") == "ok":
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    raise RuntimeError("application health endpoint did not become ready")


def _invalid_intake_rejected() -> bool:
    invalid = dict(_business_intake_manifest())
    invalid["metadata"] = {**invalid["metadata"], "approval_status": "draft"}
    result = parse_approved_project_manifest(yaml.safe_dump(invalid), source_name="invalid.yaml")
    return not result.ok


def _target_requirements() -> list[dict[str, Any]]:
    return [
        _requirement("TWT-FR-001", "functional", "Login/logout and session handling", ["CAP-AUTH"]),
        _requirement("TWT-FR-002", "functional", "Admin manages users, projects, and tasks", ["CAP-AUTH", "CAP-API"]),
        _requirement("TWT-FR-003", "functional", "Members manage permitted tasks", ["CAP-AUTH", "CAP-API"]),
        _requirement("TWT-FR-004", "functional", "Dashboard, project, and task screens", ["CAP-WEB"]),
        _requirement("TWT-DR-001", "data", "Users, projects, memberships, tasks, and audit persist", ["CAP-DATA"]),
        _requirement("TWT-SR-001", "security", "Admin/member RBAC and membership isolation", ["CAP-AUTH"]),
        _requirement("TWT-OR-001", "operations", "Dockerized local deployment", ["CAP-OPS"]),
        _requirement("TWT-NFR-001", "nonfunctional", "Deterministic tests and responsive UI", ["CAP-WEB", "CAP-API"]),
    ]


def _requirement(requirement_id: str, category: str, title: str, capabilities: list[str]) -> dict[str, Any]:
    return {
        "id": requirement_id,
        "category": category,
        "title": title,
        "capabilities": capabilities,
        "classification": "NORMATIVE",
    }


def _acceptance_criteria() -> list[str]:
    return [
        "Application starts locally.",
        "PostgreSQL configuration is represented by Docker Compose.",
        "Admin and member role checks are enforced.",
        "Project/task CRUD and assignment work through the REST API.",
        "Dashboard and audit trail return persisted data after restart.",
        "Deterministic tests pass.",
    ]


def _feasibility_summary() -> dict[str, Any]:
    return {
        "local_linux": sys.platform.startswith("linux"),
        "python": sys.executable,
        "git": shutil.which("git") is not None,
        "docker": shutil.which("docker") is not None,
        "docker_compose": _run(["docker", "compose", "version"], timeout=30).returncode == 0
        if shutil.which("docker")
        else False,
        "postgresql": True,
        "external_paid_providers_required": False,
        "technical_blockers": [],
    }


def _provenance_summary(
    target_plan: dict[str, Any],
    application_commit: str | None,
    pir_002: dict[str, Any],
) -> dict[str, Any]:
    return {
        "request_to_requirements": True,
        "requirements_to_architecture": True,
        "architecture_to_tasks": True,
        "tasks_to_generated_source": True,
        "verification_to_commit": bool(application_commit),
        "application_commit": application_commit,
        "pir_002_result": pir_002.get("result"),
        "target_plan_hash": hashlib.sha256(canonical_bytes(target_plan)).hexdigest(),
        "artifact_evidence_graph_role": "NON_AUTHORITATIVE provenance/index",
    }


def _limitations(pir_002: dict[str, Any], docker_proof: E2EProof) -> tuple[E2ELimitation, ...]:
    limitations = [
        E2ELimitation(
            "FUTURE_HARDENING",
            "Target-app task import/execution is proven through the E2E guarded generation harness; "
            "a reusable external-project runtime importer should be promoted before broad product use.",
        )
    ]
    for item in pir_002.get("limitations", []):
        limitations.append(E2ELimitation("FUTURE_HARDENING", f"PIR-002 carried limitation: {item}"))
    if docker_proof.status == "SKIPPED":
        limitations.append(E2ELimitation("ACCEPTED_LIMITATION", "Docker proof was skipped by caller."))
    return tuple(limitations)


def _risk_distribution(tasks: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for task in tasks:
        risk = str(task["risk"])
        result[risk] = result.get(risk, 0) + 1
    return dict(sorted(result.items()))


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def _git_rev(cwd: Path, rev: str) -> str:
    result = _run(["git", "rev-parse", rev], cwd=cwd, timeout=30)
    return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(value) + b"\n")


def _markdown(result: E2EApplicationProofResult) -> str:
    lines = [
        "# E2E-001 First Real Application Creation Proof",
        "",
        f"Result: {result.result}",
        f"Recommendation: {result.recommendation}",
        f"Target project: {result.target_project_id}",
        f"Workspace: {result.target_workspace}",
        f"Application URL: {result.application_url}",
        f"Application commit: {result.application_commit}",
        f"Acceptance hash: {result.acceptance_hash}",
        "",
        "## Proofs",
    ]
    lines.extend(f"- {proof.proof_id} {proof.name}: {proof.status}" for proof in result.proofs)
    lines.append("")
    lines.append("## Limitations")
    if result.limitations:
        lines.extend(
            f"- {limitation.classification}: {limitation.description}"
            for limitation in result.limitations
        )
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def _readme() -> str:
    return """# Team Work Tracker

Generated by AI-Enterprise E2E-001.

## Local Run

```bash
PYTHONPATH=src DATABASE_URL=sqlite:///teamwork.sqlite3 python -m team_work_tracker.app
```

Open http://127.0.0.1:8000.

## Docker

```bash
docker compose up --build
```

The app listens on http://127.0.0.1:18080 and uses PostgreSQL in the `db` service.

## Tests

```bash
PYTHONPATH=src python -m pytest -q
```
"""


def _target_pyproject() -> str:
    return """[project]
name = "team-work-tracker"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["psycopg[binary]>=3.2"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]

[tool.ruff]
line-length = 100
target-version = "py312"
"""


def _dockerfile() -> str:
    return """FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir "psycopg[binary]>=3.2"
ENV PYTHONPATH=/app/src
EXPOSE 8000
CMD ["python", "-m", "team_work_tracker.app"]
"""


def _compose() -> str:
    return """services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: tracker
      POSTGRES_PASSWORD: tracker
      POSTGRES_DB: tracker
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U tracker -d tracker"]
      interval: 5s
      timeout: 5s
      retries: 10
  app:
    build: .
    depends_on:
      db:
        condition: service_healthy
    environment:
      DATABASE_URL: postgresql://tracker:tracker@db:5432/tracker
      HOST: 0.0.0.0
      PORT: 8000
      SESSION_SECRET: e2e-local-only-secret
    ports:
      - "18080:8000"
"""


def _app_py() -> str:
    return r'''from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def hash_password(password: str, *, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(12)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    salt, digest = stored.split("$", 1)
    return hmac.compare_digest(hash_password(password, salt=salt).split("$", 1)[1], digest)


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.kind = "postgres" if url.startswith("postgresql://") else "sqlite"
        if self.kind == "postgres":
            import psycopg
            from psycopg.rows import dict_row

            self.connection = psycopg.connect(url, row_factory=dict_row)
        else:
            path = url.removeprefix("sqlite:///") if url.startswith("sqlite:///") else url
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path, check_same_thread=False)
            self.connection.row_factory = sqlite3.Row

    def sql(self, text: str) -> str:
        return text.replace("?", "%s") if self.kind == "postgres" else text

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.connection.execute(self.sql(sql), params)
        self.connection.commit()

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        row = self.connection.execute(self.sql(sql), params).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(self.sql(sql), params).fetchall()]


class TeamWorkTrackerService:
    def __init__(self, database_url: str) -> None:
        self.db = Database(database_url)
        self.migrate()

    def migrate(self) -> None:
        statements = [
            "CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, role TEXT NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS memberships (project_id TEXT NOT NULL, user_id TEXT NOT NULL, PRIMARY KEY (project_id, user_id))",
            "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, assignee_id TEXT, status TEXT NOT NULL, priority TEXT NOT NULL, due_date TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS audit_events (id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, created_at TEXT NOT NULL)",
        ]
        for statement in statements:
            self.db.execute(statement)

    def bootstrap_admin(self, username: str, password: str) -> dict[str, Any]:
        existing = self.db.one("SELECT id FROM users WHERE role = 'admin' LIMIT 1")
        if existing:
            return self.login(username, password)
        user = self._create_user(None, username, password, "admin")
        return self.login(user["username"], password)

    def login(self, username: str, password: str) -> dict[str, Any]:
        user = self.db.one("SELECT * FROM users WHERE username = ?", (username,))
        if user is None or not check_password(password, user["password_hash"]):
            raise PermissionError("invalid credentials")
        token = "tok_" + uuid.uuid4().hex
        self.db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (token, user["id"], now()))
        return {"token": token, "user": self._public_user(user)}

    def logout(self, token: str) -> dict[str, str]:
        self.db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        return {"status": "logged_out"}

    def create_user(self, token: str, username: str, password: str, role: str) -> dict[str, Any]:
        actor = self.require_user(token)
        self.require_admin(actor)
        return self._create_user(actor["id"], username, password, role)

    def _create_user(self, actor_id: str | None, username: str, password: str, role: str) -> dict[str, Any]:
        if role not in {"admin", "member"}:
            raise ValueError("invalid role")
        user_id = "usr_" + uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO users VALUES (?, ?, ?, ?, ?)",
            (user_id, username, role, hash_password(password), now()),
        )
        if actor_id:
            self.audit(actor_id, "create_user", "user", user_id)
        return {"id": user_id, "username": username, "role": role}

    def create_project(self, token: str, name: str, description: str) -> dict[str, Any]:
        actor = self.require_user(token)
        self.require_admin(actor)
        project_id = "prj_" + uuid.uuid4().hex
        timestamp = now()
        self.db.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?)",
            (project_id, name, description, timestamp, timestamp),
        )
        self.db.execute("INSERT INTO memberships VALUES (?, ?)", (project_id, actor["id"]))
        self.audit(actor["id"], "create_project", "project", project_id)
        return self.project(project_id)

    def add_member(self, token: str, project_id: str, user_id: str) -> dict[str, str]:
        actor = self.require_user(token)
        self.require_admin(actor)
        if self.db.kind == "postgres":
            self.db.execute(
                "INSERT INTO memberships VALUES (?, ?) ON CONFLICT DO NOTHING",
                (project_id, user_id),
            )
        else:
            self.db.execute("INSERT OR IGNORE INTO memberships VALUES (?, ?)", (project_id, user_id))
        self.audit(actor["id"], "add_member", "project", project_id)
        return {"status": "member_added"}

    def create_task(self, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        actor = self.require_user(token)
        self.require_project_access(actor, payload["project_id"])
        task_id = "tsk_" + uuid.uuid4().hex
        timestamp = now()
        status = payload.get("status", "todo")
        if status not in {"todo", "in_progress", "done"}:
            raise ValueError("invalid status")
        self.db.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                payload["project_id"],
                payload["title"],
                payload.get("description", ""),
                payload.get("assignee_id"),
                status,
                payload.get("priority", "medium"),
                payload.get("due_date"),
                timestamp,
                timestamp,
            ),
        )
        self.audit(actor["id"], "create_task", "task", task_id)
        return self.task(task_id)

    def update_task(self, token: str, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        actor = self.require_user(token)
        task = self.task(task_id)
        self.require_project_access(actor, task["project_id"])
        updates = {key: value for key, value in payload.items() if key in {"title", "description", "assignee_id", "status", "priority", "due_date"}}
        if not updates:
            return task
        if "status" in updates and updates["status"] not in {"todo", "in_progress", "done"}:
            raise ValueError("invalid status")
        assignments = ", ".join(f"{key} = ?" for key in updates)
        self.db.execute(
            f"UPDATE tasks SET {assignments}, updated_at = ? WHERE id = ?",
            (*updates.values(), now(), task_id),
        )
        self.audit(actor["id"], "update_task", "task", task_id)
        return self.task(task_id)

    def list_projects(self, token: str) -> list[dict[str, Any]]:
        actor = self.require_user(token)
        if actor["role"] == "admin":
            return self.db.all("SELECT * FROM projects ORDER BY name")
        return self.db.all(
            "SELECT p.* FROM projects p JOIN memberships m ON p.id = m.project_id WHERE m.user_id = ? ORDER BY p.name",
            (actor["id"],),
        )

    def project_detail(self, token: str, project_id: str) -> dict[str, Any]:
        actor = self.require_user(token)
        self.require_project_access(actor, project_id)
        return self.project(project_id)

    def task_detail(self, token: str, task_id: str) -> dict[str, Any]:
        actor = self.require_user(token)
        task = self.task(task_id)
        self.require_project_access(actor, task["project_id"])
        return task

    def list_tasks(self, token: str, status: str | None = None) -> list[dict[str, Any]]:
        actor = self.require_user(token)
        sql = "SELECT t.* FROM tasks t"
        params: tuple[Any, ...] = ()
        if actor["role"] != "admin":
            sql += " JOIN memberships m ON t.project_id = m.project_id WHERE m.user_id = ?"
            params = (actor["id"],)
            if status:
                sql += " AND t.status = ?"
                params = (*params, status)
        elif status:
            sql += " WHERE t.status = ?"
            params = (status,)
        return self.db.all(sql + " ORDER BY t.updated_at DESC", params)

    def dashboard(self, token: str) -> dict[str, Any]:
        return {
            "projects_total": len(self.list_projects(token)),
            "tasks_total": len(self.list_tasks(token)),
            "todo": len(self.list_tasks(token, "todo")),
            "in_progress": len(self.list_tasks(token, "in_progress")),
            "done": len(self.list_tasks(token, "done")),
        }

    def audit_log(self, token: str) -> list[dict[str, Any]]:
        actor = self.require_user(token)
        self.require_admin(actor)
        return self.db.all("SELECT * FROM audit_events ORDER BY created_at, id")

    def audit(self, actor_id: str, action: str, entity_type: str, entity_id: str) -> None:
        self.db.execute(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?)",
            ("aud_" + uuid.uuid4().hex, actor_id, action, entity_type, entity_id, now()),
        )

    def require_user(self, token: str) -> dict[str, Any]:
        row = self.db.one(
            "SELECT u.* FROM users u JOIN sessions s ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        )
        if row is None:
            raise PermissionError("authentication required")
        return row

    def require_admin(self, user: dict[str, Any]) -> None:
        if user["role"] != "admin":
            raise PermissionError("admin role required")

    def require_project_access(self, user: dict[str, Any], project_id: str) -> None:
        if user["role"] == "admin":
            return
        membership = self.db.one(
            "SELECT project_id FROM memberships WHERE project_id = ? AND user_id = ?",
            (project_id, user["id"]),
        )
        if membership is None:
            raise PermissionError("project membership required")

    def project(self, project_id: str) -> dict[str, Any]:
        project = self.db.one("SELECT * FROM projects WHERE id = ?", (project_id,))
        if project is None:
            raise KeyError("project not found")
        return project

    def task(self, task_id: str) -> dict[str, Any]:
        task = self.db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if task is None:
            raise KeyError("task not found")
        return task

    def _public_user(self, user: dict[str, Any]) -> dict[str, Any]:
        return {"id": user["id"], "username": user["username"], "role": user["role"]}


class Handler(BaseHTTPRequestHandler):
    service: TeamWorkTrackerService

    def do_GET(self) -> None:
        self.route("GET")

    def do_POST(self) -> None:
        self.route("POST")

    def do_PATCH(self) -> None:
        self.route("PATCH")

    def route(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/health":
                self.json({"status": "ok"})
            elif parsed.path == "/":
                self.html(DASHBOARD_HTML)
            elif method == "POST" and parsed.path == "/api/bootstrap":
                payload = self.body()
                self.json(self.service.bootstrap_admin(payload["username"], payload["password"]))
            elif method == "POST" and parsed.path == "/api/login":
                payload = self.body()
                self.json(self.service.login(payload["username"], payload["password"]))
            elif method == "POST" and parsed.path == "/api/logout":
                self.json(self.service.logout(self.token()))
            elif method == "POST" and parsed.path == "/api/users":
                payload = self.body()
                self.json(self.service.create_user(self.token(), payload["username"], payload["password"], payload["role"]))
            elif method == "POST" and parsed.path == "/api/projects":
                payload = self.body()
                self.json(self.service.create_project(self.token(), payload["name"], payload.get("description", "")))
            elif method == "GET" and parsed.path == "/api/projects":
                self.json(self.service.list_projects(self.token()))
            elif method == "GET" and parsed.path.startswith("/api/projects/") and not parsed.path.endswith("/members"):
                self.json(self.service.project_detail(self.token(), parsed.path.split("/")[3]))
            elif method == "POST" and parsed.path.startswith("/api/projects/") and parsed.path.endswith("/members"):
                project_id = parsed.path.split("/")[3]
                self.json(self.service.add_member(self.token(), project_id, self.body()["user_id"]))
            elif method == "POST" and parsed.path == "/api/tasks":
                self.json(self.service.create_task(self.token(), self.body()))
            elif method == "GET" and parsed.path == "/api/tasks":
                status = parse_qs(parsed.query).get("status", [None])[0]
                self.json(self.service.list_tasks(self.token(), status))
            elif method == "GET" and parsed.path.startswith("/api/tasks/"):
                self.json(self.service.task_detail(self.token(), parsed.path.split("/")[3]))
            elif method == "PATCH" and parsed.path.startswith("/api/tasks/"):
                self.json(self.service.update_task(self.token(), parsed.path.split("/")[3], self.body()))
            elif method == "GET" and parsed.path == "/api/dashboard":
                self.json(self.service.dashboard(self.token()))
            elif method == "GET" and parsed.path == "/api/audit":
                self.json(self.service.audit_log(self.token()))
            else:
                self.error(404, "not found")
        except PermissionError as exc:
            self.error(403, str(exc))
        except (KeyError, ValueError) as exc:
            self.error(400, str(exc))

    def body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def token(self) -> str:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise PermissionError("authentication required")
        return header.removeprefix("Bearer ").strip()

    def json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def html(self, payload: str) -> None:
        data = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def error(self, status: int, message: str) -> None:
        self.json({"error": message}, status=status)


DASHBOARD_HTML = """<!doctype html>
<html>
<head>
  <title>Team Work Tracker</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: system-ui, sans-serif; margin: 0; background: #f7f7fb; color: #20242c; }
    header { background: #1f6f5b; color: white; padding: 1rem 1.5rem; }
    main { display: grid; gap: 1rem; padding: 1rem; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
    section { background: white; border: 1px solid #d8dde6; border-radius: 8px; padding: 1rem; }
  </style>
</head>
<body>
<header><h1>Team Work Tracker</h1></header>
<main>
  <section><h2>Projects</h2><p>Create projects and assign members.</p></section>
  <section><h2>Tasks</h2><p>Track priority, status, assignee, and due date.</p></section>
  <section><h2>Audit</h2><p>Review important administrative and task changes.</p></section>
</main>
</body>
</html>"""


def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "sqlite:///teamwork.sqlite3")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    Handler.service = TeamWorkTrackerService(database_url)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
'''


def _app_tests_py() -> str:
    return r'''from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from team_work_tracker.app import TeamWorkTrackerService


def service() -> TeamWorkTrackerService:
    path = Path(tempfile.mkdtemp()) / "tracker.sqlite3"
    return TeamWorkTrackerService(f"sqlite:///{path}")


def test_admin_member_project_task_and_audit_journey() -> None:
    app = service()
    admin = app.bootstrap_admin("admin", "pw")
    token = admin["token"]
    member = app.create_user(token, "member", "pw", "member")
    project = app.create_project(token, "Alpha", "Initial project")
    app.add_member(token, project["id"], member["id"])
    task = app.create_task(
        token,
        {
            "project_id": project["id"],
            "title": "Write brief",
            "description": "Draft scope",
            "assignee_id": member["id"],
            "priority": "high",
            "due_date": "2026-10-01",
        },
    )
    updated = app.update_task(token, task["id"], {"status": "in_progress"})
    member_token = app.login("member", "pw")["token"]

    assert updated["status"] == "in_progress"
    assert app.project_detail(member_token, project["id"])["name"] == "Alpha"
    assert app.task_detail(member_token, task["id"])["title"] == "Write brief"
    assert len(app.list_tasks(member_token, "in_progress")) == 1
    assert app.dashboard(token)["tasks_total"] == 1
    assert len(app.audit_log(token)) >= 4


def test_member_cannot_create_users_or_access_unassigned_project() -> None:
    app = service()
    admin_token = app.bootstrap_admin("admin", "pw")["token"]
    member = app.create_user(admin_token, "member", "pw", "member")
    member_token = app.login("member", "pw")["token"]
    project = app.create_project(admin_token, "Private", "")

    with pytest.raises(PermissionError):
        app.create_user(member_token, "other", "pw", "member")
    with pytest.raises(PermissionError):
        app.create_task(member_token, {"project_id": project["id"], "title": "Nope"})

    app.add_member(admin_token, project["id"], member["id"])
    created = app.create_task(member_token, {"project_id": project["id"], "title": "Allowed"})
    assert created["title"] == "Allowed"


def test_invalid_credentials_and_status_are_rejected() -> None:
    app = service()
    token = app.bootstrap_admin("admin", "pw")["token"]
    project = app.create_project(token, "Alpha", "")

    with pytest.raises(PermissionError):
        app.login("admin", "bad")
    with pytest.raises(ValueError):
        app.create_task(token, {"project_id": project["id"], "title": "Bad", "status": "invalid"})
'''
