from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ai_ent.external_project import (
    CONTROL_PLANE_AUTHORITY,
    DEFAULT_PROHIBITED_PATHS,
    MANDATORY_VERIFICATION_COMMANDS,
)
from ai_ent.governance_contract import validate_governance_evolution_request
from ai_ent.knowledge_graph import (
    ARTIFACT_EVIDENCE_GRAPH_OUTPUT_AUTHORITY_STATE,
    ArtifactEvidenceGraph,
    ArtifactEvidenceGraphService,
)
from ai_ent.persistence.config import DatabaseConfigError, load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.persistence.models import (
    Checkpoint,
    Execution,
    RuntimeHumanGate,
    RuntimePlanImport,
    Task,
    TaskDependency,
    TaskLease,
)
from ai_ent.product_plan_final_acceptance import PRODUCT_PLAN_ID
from ai_ent.product_runtime_handoff import (
    PRODUCT_PLAN_VERSION,
    ProductRuntimeHandoffArtifacts,
    load_product_runtime_handoff_artifacts,
)
from ai_ent.project_manifest import (
    PROJECT_MANIFEST_ROOT,
    canonical_bytes,
    compile_project_manifest,
    resolve_compiled_capabilities,
)
from ai_ent.runtime_handoff import DEFAULT_RUNTIME_PROJECT_ID
from ai_ent.scheduler.readiness import TaskReadinessService
from ai_ent.scheduler.repair import FailureClassifier, RepairPolicy
from ai_ent_product_api.errors import (
    REDACTED,
    ProductApiError,
    is_secret_field_name,
    redact_secret_details,
)
from ai_ent_product_api.schemas import (
    PRODUCT_API_ACCESS_DECISION_DEPENDENCIES,
    AccessBindingProfile,
    ArtifactEvidenceServiceBoundarySnapshot,
    ArtifactMetadataSnapshot,
    ArtifactRecordMetadataSnapshot,
    BoundarySnapshot,
    CapabilityReview,
    ExecutionCollectionSnapshot,
    ExecutionSnapshot,
    FirewallAssumptionConfig,
    GeneratedDagEdge,
    GeneratedDagSnapshot,
    GeneratedHumanGateSnapshot,
    GeneratedPlanSnapshot,
    GovernanceValidationRequest,
    GovernanceValidationResponse,
    GovernanceValidationSummary,
    HealthStatus,
    HumanGateApprovalRequest,
    HumanGateDecisionResponse,
    HumanGateEvidenceValidationRequest,
    HumanGateEvidenceValidationResponse,
    HumanGateRejectionRequest,
    HumanGateReviewCollectionSnapshot,
    HumanGateReviewSnapshot,
    HumanGateScopedDecisionRequest,
    ProductAccessProfileSnapshot,
    ProjectArchitectureReview,
    ProjectIntakeReview,
    ProjectRequirementsReview,
    ProjectReview,
    ProvenanceEdgeSnapshot,
    ProvenanceNodeSnapshot,
    ProvenanceTraceSnapshot,
    RepairCollectionSnapshot,
    RepairSnapshot,
    ReverseProxyAccessConfig,
    ReviewBoundarySnapshot,
    RuntimeAuthoritySnapshot,
    RuntimeComponentStatus,
    RuntimeHumanGateStateSnapshot,
    RuntimeStateSnapshot,
    RuntimeStatus,
    ServiceDependencyStatus,
    TaskCollectionSnapshot,
    TaskSnapshot,
    TlsAccessConfig,
)

ArtifactsLoader = Callable[[], ProductRuntimeHandoffArtifacts]
SessionFactory = Callable[[], Session]
type RuntimeComponentName = Literal["readiness", "recovery", "docker", "codex", "postgresql"]
type RuntimeProbe = Callable[[], RuntimeComponentStatus]


@dataclass(frozen=True)
class ProductBoundaryService:
    def boundary(self) -> BoundarySnapshot:
        return BoundarySnapshot(
            control_plane_authority=CONTROL_PLANE_AUTHORITY,
            mandatory_verification_commands=MANDATORY_VERIFICATION_COMMANDS,
            allowed_operations=(
                "observe_boundary",
                "query_provenance",
                "read_artifact_metadata",
                "read_artifact_provenance",
                "validate_governance_request",
                "read_human_gate_review",
                "validate_human_gate_evidence",
                "submit_human_gate_decision",
            ),
            denied_operations=(
                "approve_gate",
                "bypass_commit_boundary",
                "bypass_verifier",
                "claim_task",
                "commit",
                "execute_repair",
                "execute_runtime",
                "push",
                "record_evidence",
                "schedule_execution",
                "weaken_policy",
            ),
            prohibited_paths=DEFAULT_PROHIBITED_PATHS,
        )

    def dependency_status(self) -> ServiceDependencyStatus:
        return ServiceDependencyStatus(
            name="product_boundary",
            boundary="existing control-plane authority projection",
            status="wired",
        )


@dataclass(frozen=True)
class ProductAccessProfileService:
    bind_address: str = "127.0.0.1"
    port: int = 8000
    lan_bind_address: str = "explicit_private_interface_required"

    def profile(self) -> ProductAccessProfileSnapshot:
        return ProductAccessProfileSnapshot(
            profile_id="ACCESS-LOCAL-LAN-001",
            active_profile="local_loopback",
            configured_profiles=("local_loopback", "lan_gated"),
            binding=AccessBindingProfile(
                bind_address=self.bind_address,
                port=self.port,
                lan_bind_address=self.lan_bind_address,
                effective_bind_address=self.bind_address,
                network_scope="local_loopback",
                blocked_by_decisions=PRODUCT_API_ACCESS_DECISION_DEPENDENCIES,
            ),
            reverse_proxy=ReverseProxyAccessConfig(
                forwards_to_bind_address=self.bind_address,
                forwards_to_port=self.port,
            ),
            tls=TlsAccessConfig(),
            firewall_assumptions=FirewallAssumptionConfig(),
            mandatory_verification_commands=MANDATORY_VERIFICATION_COMMANDS,
            authority=_runtime_authority(),
        )

    def dependency_status(self) -> ServiceDependencyStatus:
        return ServiceDependencyStatus(
            name="product_access_profile",
            boundary="read-only local and LAN access profile projection",
            status="wired",
        )


@dataclass(frozen=True)
class GovernanceValidationService:
    def validate(self, request: GovernanceValidationRequest) -> GovernanceValidationResponse:
        decision = validate_governance_evolution_request(request.model_dump(mode="python"))
        payload = decision.as_dict()
        summary = payload["summary"]
        return GovernanceValidationResponse(
            contract_version=str(payload["contract_version"]),
            schema_version=str(payload["schema_version"]),
            status=decision.status,
            request_id=decision.request_id,
            request_hash=decision.request_hash,
            contract_hash=decision.contract_hash,
            accepted_authorities=decision.accepted_authorities,
            blockers=decision.blockers,
            required_evolution_sequence=decision.required_evolution_sequence,
            summary=GovernanceValidationSummary(
                ok=bool(summary["ok"]),
                gate_approval_granted=bool(summary["gate_approval_granted"]),
                verifier_bypass_granted=bool(summary["verifier_bypass_granted"]),
                commit_boundary_bypass_granted=bool(summary["commit_boundary_bypass_granted"]),
                self_scheduling_granted=bool(summary["self_scheduling_granted"]),
                runtime_execution_granted=bool(summary["runtime_execution_granted"]),
                policy_weakening_granted=bool(summary["policy_weakening_granted"]),
            ),
        )

    def dependency_status(self) -> ServiceDependencyStatus:
        return ServiceDependencyStatus(
            name="governance_validation",
            boundary="ai_ent.governance_contract.validate_governance_evolution_request",
            status="wired",
        )


@dataclass(frozen=True)
class RuntimeProbeProvider:
    readiness: RuntimeProbe
    recovery: RuntimeProbe
    docker: RuntimeProbe
    codex: RuntimeProbe
    postgresql: RuntimeProbe

    @classmethod
    def passive_defaults(cls) -> RuntimeProbeProvider:
        return cls(
            readiness=_readiness_status,
            recovery=_recovery_status,
            docker=_docker_status,
            codex=_codex_status,
            postgresql=_postgresql_status,
        )


@dataclass(frozen=True)
class RuntimeStatusService:
    probes: RuntimeProbeProvider

    @classmethod
    def defaults(cls) -> RuntimeStatusService:
        return cls(probes=RuntimeProbeProvider.passive_defaults())

    def status(self) -> RuntimeStatus:
        readiness = self.component_status("readiness")
        recovery = self.component_status("recovery")
        docker = self.component_status("docker")
        codex = self.component_status("codex")
        postgresql = self.component_status("postgresql")
        components = (readiness, recovery, docker, codex, postgresql)
        return RuntimeStatus(
            status=(
                "degraded"
                if any(component.status == "unavailable" for component in components)
                else "ok"
            ),
            control_plane_authority=CONTROL_PLANE_AUTHORITY,
            mandatory_verification_commands=MANDATORY_VERIFICATION_COMMANDS,
            readiness=readiness,
            recovery=recovery,
            docker=docker,
            codex=codex,
            postgresql=postgresql,
        )

    def component_status(self, name: str) -> RuntimeComponentStatus:
        try:
            probe = _component_probe(self.probes, name)
            return _safe_component_status(probe())
        except (RuntimeError, OSError, ValueError, SQLAlchemyError):
            return RuntimeComponentStatus(
                name=_component_name(name),
                boundary=f"{name} passive status probe",
                status="unknown",
                summary="status probe failed without exposing diagnostic details",
                details={"error": "status probe failed"},
            )

    def dependency_status(self) -> ServiceDependencyStatus:
        return ServiceDependencyStatus(
            name="runtime_status",
            boundary="read-only runtime component status facade",
            status="wired",
        )


@dataclass(frozen=True)
class ProjectReviewService:
    manifest_root: Path = PROJECT_MANIFEST_ROOT

    def project(self) -> ProjectReview:
        compilation = compile_project_manifest(self.manifest_root)
        project = compilation.compiled.project
        return ProjectReview(
            project_id=str(project["id"]),
            source_compiled_hash=compilation.lock.compiled_hash,
            project_hash=_hash_payload(project),
            project=project,
            review_boundary=self.review_boundary(),
        )

    def intake(self) -> ProjectIntakeReview:
        compilation = compile_project_manifest(self.manifest_root)
        intake = compilation.compiled.intake
        return ProjectIntakeReview(
            project_id=compilation.lock.project_id,
            source_compiled_hash=compilation.lock.compiled_hash,
            intake_hash=_hash_payload(intake),
            intake=intake,
            review_boundary=self.review_boundary(),
        )

    def requirements(self) -> ProjectRequirementsReview:
        compilation = compile_project_manifest(self.manifest_root)
        requirements = compilation.compiled.requirements
        return ProjectRequirementsReview(
            project_id=compilation.lock.project_id,
            source_compiled_hash=compilation.lock.compiled_hash,
            requirements_hash=_hash_payload(list(requirements)),
            requirements_count=len(requirements),
            requirements=requirements,
            review_boundary=self.review_boundary(),
        )

    def architecture(self) -> ProjectArchitectureReview:
        compilation = compile_project_manifest(self.manifest_root)
        architecture = compilation.compiled.architecture
        components = architecture.get("components", [])
        interfaces = architecture.get("interfaces", [])
        data_objects = architecture.get("data_objects", [])
        return ProjectArchitectureReview(
            project_id=compilation.lock.project_id,
            source_compiled_hash=compilation.lock.compiled_hash,
            architecture_hash=_hash_payload(architecture),
            component_count=len(components) if isinstance(components, list) else 0,
            interface_count=len(interfaces) if isinstance(interfaces, list) else 0,
            data_object_count=len(data_objects) if isinstance(data_objects, list) else 0,
            architecture=architecture,
            review_boundary=self.review_boundary(),
        )

    def capabilities_review(self) -> CapabilityReview:
        compilation = compile_project_manifest(self.manifest_root)
        resolution = resolve_compiled_capabilities(compilation)
        return CapabilityReview(
            project_id=compilation.lock.project_id,
            source_compiled_hash=resolution.source_compiled_hash,
            capability_resolution_hash=resolution.capability_resolution_hash,
            capability_count=len(resolution.capabilities),
            capabilities=tuple(capability.as_dict() for capability in resolution.capabilities),
            required_capabilities=resolution.required_capabilities,
            satisfied_capabilities=resolution.satisfied_capabilities,
            partially_satisfied_capabilities=resolution.partially_satisfied_capabilities,
            unsatisfied_capabilities=resolution.unsatisfied_capabilities,
            deferred_capabilities=resolution.deferred_capabilities,
            blocked_capabilities=resolution.blocked_capabilities,
            mandatory_verification_commands=MANDATORY_VERIFICATION_COMMANDS,
            review_boundary=self.review_boundary(),
        )

    def review_boundary(self) -> ReviewBoundarySnapshot:
        return ReviewBoundarySnapshot(
            source_authority=str(self.manifest_root),
            control_plane_authority=CONTROL_PLANE_AUTHORITY,
            mandatory_verification_commands=MANDATORY_VERIFICATION_COMMANDS,
            allowed_operations=(
                "observe_project",
                "observe_intake",
                "observe_requirements",
                "observe_architecture",
                "review_capabilities",
            ),
            denied_operations=(
                "approve_gate",
                "bypass_commit_boundary",
                "bypass_verifier",
                "claim_task",
                "commit",
                "execute_repair",
                "execute_runtime",
                "push",
                "schedule_execution",
                "weaken_policy",
            ),
            prohibited_paths=DEFAULT_PROHIBITED_PATHS,
        )

    def dependency_status(self) -> ServiceDependencyStatus:
        return ServiceDependencyStatus(
            name="project_review",
            boundary="ai_ent.project_manifest bounded read-only facade",
            status="wired",
        )


@dataclass(frozen=True)
class ProductRuntimeSnapshotService:
    artifacts_loader: ArtifactsLoader = load_product_runtime_handoff_artifacts
    session_factory: SessionFactory | None = None
    readiness: TaskReadinessService = field(default_factory=TaskReadinessService)
    classifier: FailureClassifier = field(default_factory=FailureClassifier)
    repair_policy: RepairPolicy = field(
        default_factory=lambda: RepairPolicy(max_autonomous_repair_attempts=1)
    )

    def generated_plan(self) -> GeneratedPlanSnapshot:
        artifacts = self.artifacts_loader()
        lock = artifacts.lock
        dag = _productization_dag(artifacts)
        summary = _mapping(dag.get("summary"))
        return GeneratedPlanSnapshot(
            product_plan_id=str(lock.get("product_plan_id", PRODUCT_PLAN_ID)),
            plan_version=str(lock.get("plan_version", PRODUCT_PLAN_VERSION)),
            plan_state=str(lock.get("state", "UNKNOWN")),
            plan_hash=str(lock.get("accepted_prd_plan_hash", "")),
            task_count=_int(summary.get("task_count"), len(artifacts.import_preview)),
            dependency_edge_count=_int(
                summary.get("dependency_edges"),
                len(_runtime_dependency_edges(artifacts)),
            ),
            human_gate_count=_int(
                summary.get("human_gates"),
                len(_human_gate_definitions(artifacts)),
            ),
            effective_concurrency=_int(lock.get("effective_concurrency"), 1),
            critical_path=_string_tuple(dag.get("critical_path")),
            waves=_dict_tuple(dag.get("waves")),
            authority=_runtime_authority(),
        )

    def generated_dag(self) -> GeneratedDagSnapshot:
        artifacts = self.artifacts_loader()
        lock = artifacts.lock
        dag = _productization_dag(artifacts)
        return GeneratedDagSnapshot(
            product_plan_id=str(lock.get("product_plan_id", PRODUCT_PLAN_ID)),
            plan_version=str(lock.get("plan_version", PRODUCT_PLAN_VERSION)),
            dependency_edges=tuple(
                GeneratedDagEdge(dependency_task_id=dependency, task_id=task_id)
                for dependency, task_id in _runtime_dependency_edges(artifacts)
            ),
            waves=_dict_tuple(dag.get("waves")),
            critical_path=_string_tuple(dag.get("critical_path")),
            human_gates=tuple(
                GeneratedHumanGateSnapshot(
                    gate_id=str(gate.get("gate_id", "")),
                    task_id=str(gate.get("task_id", "")),
                    status=str(gate.get("status", "PENDING_NOT_APPROVED")),
                    reason=str(gate.get("reason", "")),
                    risk_level=str(gate.get("risk", gate.get("risk_level", ""))),
                    approval_boundary=str(gate.get("approval_boundary", "")),
                    resume_semantics=str(gate.get("resume_semantics", "")),
                )
                for gate in _human_gate_definitions(artifacts)
            ),
            authority=_runtime_authority(),
        )

    def tasks(self) -> TaskCollectionSnapshot:
        artifacts = self.artifacts_loader()
        lock = artifacts.lock
        runtime_tasks = self._runtime_tasks()
        return TaskCollectionSnapshot(
            product_plan_id=str(lock.get("product_plan_id", PRODUCT_PLAN_ID)),
            plan_version=str(lock.get("plan_version", PRODUCT_PLAN_VERSION)),
            tasks=tuple(
                self._task_snapshot(contract, runtime_tasks.get(str(contract["task_id"])))
                for contract in sorted(
                    artifacts.import_preview,
                    key=lambda item: str(item["task_id"]),
                )
            ),
            authority=_runtime_authority(),
        )

    def executions(self) -> ExecutionCollectionSnapshot:
        artifacts = self.artifacts_loader()
        lock = artifacts.lock
        return ExecutionCollectionSnapshot(
            product_plan_id=str(lock.get("product_plan_id", PRODUCT_PLAN_ID)),
            plan_version=str(lock.get("plan_version", PRODUCT_PLAN_VERSION)),
            executions=self._execution_snapshots(),
            authority=_runtime_authority(),
        )

    def repairs(self) -> RepairCollectionSnapshot:
        artifacts = self.artifacts_loader()
        lock = artifacts.lock
        return RepairCollectionSnapshot(
            product_plan_id=str(lock.get("product_plan_id", PRODUCT_PLAN_ID)),
            plan_version=str(lock.get("plan_version", PRODUCT_PLAN_VERSION)),
            repairs=self._repair_snapshots(),
            authority=_runtime_authority(),
        )

    def runtime_state(self) -> RuntimeStateSnapshot:
        artifacts = self.artifacts_loader()
        lock = artifacts.lock
        if self.session_factory is None:
            return RuntimeStateSnapshot(
                project_id=DEFAULT_RUNTIME_PROJECT_ID,
                product_plan_id=str(lock.get("product_plan_id", PRODUCT_PLAN_ID)),
                plan_version=str(lock.get("plan_version", PRODUCT_PLAN_VERSION)),
                import_status=None,
                imported_tasks=0,
                dependency_edges=0,
                human_gates=0,
                pending_human_gates=0,
                approved_human_gates=0,
                rejected_human_gates=0,
                ready_tasks=(),
                waiting_tasks=(),
                gated_tasks=(),
                decision_blocked_tasks=(),
                executions=0,
                failed_executions=0,
                active_leases=0,
                runtime_human_gates=(),
                authority=_runtime_authority(),
            )

        with self.session_factory() as session:
            runtime_import = _latest_product_import(session)
            project_id = (
                runtime_import.project_id
                if runtime_import is not None
                else DEFAULT_RUNTIME_PROJECT_ID
            )
            task_ids = _product_task_ids(session)
            readiness = {
                task_id: self.readiness.evaluate_task(session, task_id)
                for task_id in task_ids
            }
            gates = tuple(
                RuntimeHumanGateStateSnapshot(
                    gate_id=gate.id,
                    task_id=gate.task_id,
                    status=gate.status,
                    reason=gate.reason,
                    approval_boundary=gate.approval_boundary,
                )
                for gate in session.scalars(
                    select(RuntimeHumanGate)
                    .where(RuntimeHumanGate.plan_id == PRODUCT_PLAN_ID)
                    .order_by(RuntimeHumanGate.id)
                )
            )
            return RuntimeStateSnapshot(
                project_id=project_id,
                product_plan_id=str(lock.get("product_plan_id", PRODUCT_PLAN_ID)),
                plan_version=str(lock.get("plan_version", PRODUCT_PLAN_VERSION)),
                import_status=runtime_import.status if runtime_import is not None else None,
                imported_tasks=len(task_ids),
                dependency_edges=_count_product_dependencies(session),
                human_gates=len(gates),
                pending_human_gates=sum(1 for gate in gates if gate.status == "pending"),
                approved_human_gates=sum(1 for gate in gates if gate.status == "approved"),
                rejected_human_gates=sum(1 for gate in gates if gate.status == "rejected"),
                ready_tasks=tuple(
                    task_id for task_id, decision in sorted(readiness.items()) if decision.ready
                ),
                waiting_tasks=tuple(
                    task_id
                    for task_id, decision in sorted(readiness.items())
                    if decision.status == "WAITING_DEPENDENCIES"
                ),
                gated_tasks=tuple(
                    task_id
                    for task_id, decision in sorted(readiness.items())
                    if decision.reasons == ("pending_human_gate",)
                ),
                decision_blocked_tasks=tuple(
                    task_id
                    for task_id, decision in sorted(readiness.items())
                    if decision.status == "DECISION_BLOCKED"
                ),
                executions=_count_product_executions(session),
                failed_executions=_count_failed_product_executions(session),
                active_leases=_count_active_product_leases(session),
                runtime_human_gates=gates,
                authority=_runtime_authority(),
            )

    def dependency_status(self) -> ServiceDependencyStatus:
        return ServiceDependencyStatus(
            name="product_runtime_snapshots",
            boundary="read-only projection of product runtime handoff and persistence state",
            status="wired",
        )

    def _task_snapshot(
        self,
        contract: dict[str, Any],
        runtime: tuple[Task, tuple[str, ...], str, tuple[str, ...]] | None,
    ) -> TaskSnapshot:
        task_id = str(contract["task_id"])
        title = task_id
        objective = task_id
        status = "generated"
        readiness_status = "RUNTIME_STATE_UNAVAILABLE"
        readiness_reasons: tuple[str, ...] = ()
        dependencies = tuple(str(item) for item in contract.get("depends_on", ()))
        if runtime is not None:
            task, runtime_dependencies, readiness_status, readiness_reasons = runtime
            title = task.title
            objective = task.objective or task.title
            status = task.status
            dependencies = runtime_dependencies
        return TaskSnapshot(
            task_id=task_id,
            title=title,
            objective=objective,
            status=status,
            execution_class=str(contract["execution_class"]),
            schedulable=bool(contract["schedulable"]),
            fingerprint=str(contract["fingerprint"]),
            dependencies=dependencies,
            readiness_status=readiness_status,
            readiness_reasons=readiness_reasons,
            risk_level=str(contract["risk_level"]),
            agent_role=str(contract["agent_role"]),
            model_profile=str(contract["model_profile"]),
            executor=str(contract["executor"]),
            verification_profile=str(contract["verification_profile"]),
            feasibility_status=str(contract.get("feasibility_status", "")),
            policy_decision=str(contract.get("policy_decision", "")),
            acceptance_criteria=_string_tuple(contract.get("acceptance_criteria")),
            required_decisions=_string_tuple(contract.get("required_decisions")),
            allowed_write_scope=_string_tuple(contract.get("allowed_write_scope")),
            prohibited_paths=_string_tuple(contract.get("prohibited_paths")),
            human_gate_ids=_string_tuple(contract.get("human_gate_ids")),
        )

    def _runtime_tasks(self) -> dict[str, tuple[Task, tuple[str, ...], str, tuple[str, ...]]]:
        if self.session_factory is None:
            return {}
        with self.session_factory() as session:
            tasks = {
                task.id: task
                for task in session.scalars(
                    select(Task).where(Task.id.like("PRD-TASK-%")).order_by(Task.id)
                ).all()
            }
            return {
                task_id: (
                    task,
                    tuple(
                        session.scalars(
                            select(TaskDependency.depends_on_task_id)
                            .where(TaskDependency.task_id == task_id)
                            .order_by(TaskDependency.depends_on_task_id)
                        ).all()
                    ),
                    (decision := self.readiness.evaluate_task(session, task_id)).status,
                    decision.reasons,
                )
                for task_id, task in tasks.items()
            }

    def _execution_snapshots(self) -> tuple[ExecutionSnapshot, ...]:
        if self.session_factory is None:
            return ()
        with self.session_factory() as session:
            active_leases = {
                execution_id
                for execution_id in session.scalars(
                    select(TaskLease.execution_id)
                    .join(Task, TaskLease.task_id == Task.id)
                    .where(Task.id.like("PRD-TASK-%"), TaskLease.status == "active")
                ).all()
                if execution_id is not None
            }
            checkpoint_counts: dict[str, int] = {}
            checkpoint_rows = session.execute(
                select(Checkpoint.execution_id, func.count())
                .join(Task, Checkpoint.task_id == Task.id)
                .where(Task.id.like("PRD-TASK-%"), Checkpoint.execution_id.is_not(None))
                .group_by(Checkpoint.execution_id)
            )
            for execution_id, count in checkpoint_rows:
                if execution_id is not None:
                    checkpoint_counts[execution_id] = _int(count, 0)
            return tuple(
                ExecutionSnapshot(
                    execution_id=execution.id,
                    task_id=execution.task_id,
                    executor_type=execution.executor_type,
                    status=execution.status,
                    attempt=execution.attempt,
                    terminal_state=execution.terminal_state,
                    error_classification=execution.error_classification,
                    candidate_tree_hash=execution.candidate_tree_hash,
                    commit_hash=execution.commit_hash,
                    active_lease=execution.id in active_leases,
                    checkpoint_count=int(checkpoint_counts.get(execution.id, 0)),
                )
                for execution in session.scalars(
                    select(Execution)
                    .join(Task, Execution.task_id == Task.id)
                    .where(Task.id.like("PRD-TASK-%"))
                    .order_by(Execution.task_id, Execution.attempt, Execution.id)
                )
            )

    def _repair_snapshots(self) -> tuple[RepairSnapshot, ...]:
        if self.session_factory is None:
            return ()
        with self.session_factory() as session:
            snapshots: list[RepairSnapshot] = []
            executions_by_task = _executions_by_task(session)
            for execution in session.scalars(
                select(Execution)
                .join(Task, Execution.task_id == Task.id)
                .where(
                    Task.id.like("PRD-TASK-%"),
                    Execution.status.in_(("failed", "timeout", "cancelled")),
                )
                .order_by(Execution.task_id, Execution.attempt, Execution.id)
            ):
                classification = self.classifier.classify(
                    execution,
                    latest_checkpoint=_latest_checkpoint(session, execution.id),
                )
                repair_attempt_count = _repair_attempt_count(executions_by_task[execution.task_id])
                action = _repair_action(
                    classification.retryability,
                    repair_attempt_count,
                    self.repair_policy.max_autonomous_repair_attempts,
                )
                snapshots.append(
                    RepairSnapshot(
                        failed_execution_id=execution.id,
                        task_id=execution.task_id,
                        category=classification.category,
                        stage=classification.stage,
                        retryability=classification.retryability,
                        reason=classification.reason,
                        recommended_action=action,
                        repair_attempt_count=repair_attempt_count,
                        max_autonomous_repair_attempts=(
                            self.repair_policy.max_autonomous_repair_attempts
                        ),
                        human_gate_required=action == "ESCALATE_HUMAN",
                    )
                )
            return tuple(snapshots)


@dataclass(frozen=True)
class ProductHumanApprovalService:
    artifacts_loader: ArtifactsLoader = load_product_runtime_handoff_artifacts
    session_factory: SessionFactory | None = None

    def reviews(self) -> HumanGateReviewCollectionSnapshot:
        artifacts = self.artifacts_loader()
        lock = artifacts.lock
        reviews = self._review_snapshots(artifacts)
        return HumanGateReviewCollectionSnapshot(
            product_plan_id=str(lock.get("product_plan_id", PRODUCT_PLAN_ID)),
            plan_version=str(lock.get("plan_version", PRODUCT_PLAN_VERSION)),
            reviews=reviews,
            pending_count=sum(1 for review in reviews if _is_pending_gate_status(review.status)),
            approved_count=sum(1 for review in reviews if review.status == "approved"),
            rejected_count=sum(1 for review in reviews if review.status == "rejected"),
            authority=_runtime_authority(),
        )

    def review(self, gate_id: str) -> HumanGateReviewSnapshot:
        for review in self.reviews().reviews:
            if review.gate_id == gate_id:
                return review
        raise ProductApiError(
            status_code=404,
            code="HUMAN_GATE_NOT_FOUND",
            message="human gate is not exposed by the product API boundary",
        )

    def validate_evidence(
        self,
        gate_id: str,
        request: HumanGateEvidenceValidationRequest,
    ) -> HumanGateEvidenceValidationResponse:
        review = self.review(gate_id)
        return self._evidence_validation(
            review=review,
            evidence_refs=request.evidence_refs,
            verification_commands=request.verification_commands,
            scope_task_ids=request.scope_task_ids,
        )

    def approve(
        self,
        gate_id: str,
        request: HumanGateApprovalRequest,
    ) -> HumanGateDecisionResponse:
        return self._decision_response(
            gate_id=gate_id,
            requested_decision="approve",
            request=request.model_dump(mode="python"),
            evidence_refs=request.evidence_refs,
            verification_commands=request.verification_commands,
            scope_task_ids=request.scope_task_ids,
        )

    def reject(
        self,
        gate_id: str,
        request: HumanGateRejectionRequest,
    ) -> HumanGateDecisionResponse:
        return self._decision_response(
            gate_id=gate_id,
            requested_decision="reject",
            request=request.model_dump(mode="python"),
            evidence_refs=request.evidence_refs,
            verification_commands=request.verification_commands,
            scope_task_ids=request.scope_task_ids,
        )

    def scoped_decision(
        self,
        gate_id: str,
        request: HumanGateScopedDecisionRequest,
    ) -> HumanGateDecisionResponse:
        return self._decision_response(
            gate_id=gate_id,
            requested_decision=request.decision,
            request=request.model_dump(mode="python"),
            evidence_refs=request.evidence_refs,
            verification_commands=request.verification_commands,
            scope_task_ids=request.scope_task_ids,
        )

    def dependency_status(self) -> ServiceDependencyStatus:
        return ServiceDependencyStatus(
            name="product_human_approvals",
            boundary="explicit non-mutating human gate review and decision projection",
            status="wired",
        )

    def _review_snapshots(
        self,
        artifacts: ProductRuntimeHandoffArtifacts,
    ) -> tuple[HumanGateReviewSnapshot, ...]:
        if self.session_factory is not None:
            with self.session_factory() as session:
                gates = tuple(
                    session.scalars(
                        select(RuntimeHumanGate)
                        .where(RuntimeHumanGate.plan_id == PRODUCT_PLAN_ID)
                        .order_by(RuntimeHumanGate.id)
                    )
                )
                if gates:
                    return tuple(self._runtime_review_snapshot(gate) for gate in gates)
        return self._generated_review_snapshots(artifacts)

    def _runtime_review_snapshot(self, gate: RuntimeHumanGate) -> HumanGateReviewSnapshot:
        return HumanGateReviewSnapshot(
            gate_id=gate.id,
            task_id=gate.task_id,
            status=gate.status,
            reason=gate.reason,
            risk_level=gate.risk_level,
            approval_boundary=gate.approval_boundary,
            expected_evidence=_json_string_tuple(gate.expected_evidence_json),
            downstream_task_ids=_json_string_tuple(gate.downstream_task_ids_json),
            resume_semantics=gate.resume_semantics,
            authority=_runtime_authority(),
        )

    def _generated_review_snapshots(
        self,
        artifacts: ProductRuntimeHandoffArtifacts,
    ) -> tuple[HumanGateReviewSnapshot, ...]:
        lock = artifacts.lock
        downstream_by_task = _downstream_by_task(artifacts.import_preview)
        return tuple(
            HumanGateReviewSnapshot(
                gate_id=str(gate.get("gate_id", "")),
                task_id=task_id,
                status=str(gate.get("status", "PENDING_NOT_APPROVED")),
                reason=str(gate.get("reason", "")),
                risk_level=str(gate.get("risk", gate.get("risk_level", ""))),
                approval_boundary=str(gate.get("approval_boundary", "")),
                expected_evidence=_string_tuple(gate.get("expected_evidence")),
                downstream_task_ids=downstream_by_task.get(task_id, ()),
                resume_semantics=str(
                    gate.get(
                        "resume_semantics",
                        "explicit approval, then reevaluate readiness before claim",
                    )
                ),
                authority=_runtime_authority(),
            )
            for gate in _dict_tuple(lock.get("human_gate_definitions"))
            if (task_id := str(gate.get("task_id", "")))
        )

    def _evidence_validation(
        self,
        *,
        review: HumanGateReviewSnapshot,
        evidence_refs: tuple[str, ...],
        verification_commands: tuple[str, ...],
        scope_task_ids: tuple[str, ...],
    ) -> HumanGateEvidenceValidationResponse:
        provided_evidence = set(evidence_refs)
        provided_commands = set(verification_commands)
        allowed_scope_task_ids = _allowed_scope_task_ids(review)
        allowed_scope = set(allowed_scope_task_ids)
        missing_evidence = tuple(
            evidence for evidence in review.expected_evidence if evidence not in provided_evidence
        )
        missing_commands = tuple(
            command
            for command in MANDATORY_VERIFICATION_COMMANDS
            if command not in provided_commands
        )
        unknown_scope = tuple(task_id for task_id in scope_task_ids if task_id not in allowed_scope)
        valid = not missing_evidence and not missing_commands and not unknown_scope
        return HumanGateEvidenceValidationResponse(
            gate_id=review.gate_id,
            task_id=review.task_id,
            valid=valid,
            missing_expected_evidence=missing_evidence,
            missing_mandatory_verification_commands=missing_commands,
            unknown_scope_task_ids=unknown_scope,
            allowed_scope_task_ids=allowed_scope_task_ids,
            authority=_runtime_authority(),
        )

    def _decision_response(
        self,
        *,
        gate_id: str,
        requested_decision: Literal["approve", "reject"],
        request: dict[str, Any],
        evidence_refs: tuple[str, ...],
        verification_commands: tuple[str, ...],
        scope_task_ids: tuple[str, ...],
    ) -> HumanGateDecisionResponse:
        review = self.review(gate_id)
        scope = scope_task_ids or (review.task_id,)
        evidence = self._evidence_validation(
            review=review,
            evidence_refs=evidence_refs,
            verification_commands=verification_commands,
            scope_task_ids=scope,
        )
        request_accepted = not (
            evidence.missing_mandatory_verification_commands
            or evidence.unknown_scope_task_ids
            or (requested_decision == "approve" and evidence.missing_expected_evidence)
        )
        return HumanGateDecisionResponse(
            gate_id=review.gate_id,
            task_id=review.task_id,
            requested_decision=requested_decision,
            request_accepted=request_accepted,
            decision_status=(
                "CONTROL_PLANE_REVIEW_REQUIRED" if request_accepted else "EVIDENCE_INCOMPLETE"
            ),
            decision_request_hash=_decision_hash(gate_id, requested_decision, request),
            scope_task_ids=scope,
            evidence_valid=evidence.valid,
            missing_expected_evidence=evidence.missing_expected_evidence,
            missing_mandatory_verification_commands=(
                evidence.missing_mandatory_verification_commands
            ),
            unknown_scope_task_ids=evidence.unknown_scope_task_ids,
            allowed_scope_task_ids=evidence.allowed_scope_task_ids,
            runtime_gate_status_before=review.status,
            runtime_gate_status_after=review.status,
            control_plane_authority=CONTROL_PLANE_AUTHORITY,
            authority=_runtime_authority(),
        )


@dataclass(frozen=True)
class ProductArtifactEvidenceService:
    artifacts_loader: ArtifactsLoader = load_product_runtime_handoff_artifacts
    session_factory: SessionFactory | None = None
    evidence_graphs: ArtifactEvidenceGraphService = field(
        default_factory=ArtifactEvidenceGraphService
    )
    project_id: str = DEFAULT_RUNTIME_PROJECT_ID

    def artifact_metadata(self) -> ArtifactMetadataSnapshot:
        artifacts = self.artifacts_loader()
        lock = artifacts.lock
        graph = self._graph()
        if graph is None:
            return ArtifactMetadataSnapshot(
                project_id=self.project_id,
                product_plan_id=str(lock.get("product_plan_id", PRODUCT_PLAN_ID)),
                plan_version=str(lock.get("plan_version", PRODUCT_PLAN_VERSION)),
                graph_available=False,
                graph_id=None,
                artifact_graph_contract_version=None,
                artifact_graph_schema_version=None,
                evidence_graph_hash=None,
                source_knowledge_graph_hash=None,
                source_project_memory_hash=None,
                requirements_covered=(),
                capabilities_covered=(),
                service_boundaries=(),
                artifacts=(),
                summary={
                    "artifact_record_count": 0,
                    "edge_count": 0,
                    "node_count": 0,
                    "runtime_task_count": 0,
                    "human_gate_count": 0,
                    "output_authority_state": ARTIFACT_EVIDENCE_GRAPH_OUTPUT_AUTHORITY_STATE,
                },
                authority=_runtime_authority(),
            )
        return _artifact_metadata_snapshot(
            graph,
            product_plan_id=str(lock.get("product_plan_id", PRODUCT_PLAN_ID)),
            plan_version=str(lock.get("plan_version", PRODUCT_PLAN_VERSION)),
        )

    def requirement_provenance(self, requirement_id: str) -> ProvenanceTraceSnapshot:
        artifacts = self.artifacts_loader()
        lock = artifacts.lock
        graph = self._graph()
        requirement_node_id = (
            requirement_id
            if requirement_id.startswith("requirement:")
            else f"requirement:{requirement_id}"
        )
        if graph is None:
            raise ProductApiError(
                status_code=404,
                code="PROVENANCE_NOT_AVAILABLE",
                message="artifact evidence graph is not available for this API instance",
            )
        if graph.node_by_id(requirement_node_id) is None:
            raise ProductApiError(
                status_code=404,
                code="PROVENANCE_REQUIREMENT_NOT_FOUND",
                message="requirement provenance is not available",
            )
        trace = graph.trace_for_requirement(requirement_id)
        return ProvenanceTraceSnapshot(
            project_id=graph.project_id,
            product_plan_id=str(lock.get("product_plan_id", PRODUCT_PLAN_ID)),
            plan_version=str(lock.get("plan_version", PRODUCT_PLAN_VERSION)),
            query_id=requirement_id,
            requirement_id=trace.requirement_id,
            requirement_node_id=trace.requirement_node_id,
            evidence_graph_hash=trace.evidence_graph_hash,
            nodes=tuple(_provenance_node_snapshot(node) for node in trace.nodes),
            edges=tuple(_provenance_edge_snapshot(edge) for edge in trace.edges),
            summary=trace.as_dict()["summary"],
            authority=_runtime_authority(),
        )

    def dependency_status(self) -> ServiceDependencyStatus:
        return ServiceDependencyStatus(
            name="artifact_evidence",
            boundary="read-only non-authoritative artifact evidence graph projection",
            status="wired",
        )

    def _graph(self) -> ArtifactEvidenceGraph | None:
        if self.session_factory is None:
            return None
        with self.session_factory() as session:
            return self.evidence_graphs.build(session, project_id=self.project_id)


@dataclass(frozen=True)
class ProductApiServices:
    boundary_service: ProductBoundaryService
    governance_service: GovernanceValidationService
    runtime_service: ProductRuntimeSnapshotService
    status_service: RuntimeStatusService = field(default_factory=RuntimeStatusService.defaults)
    project_review_service: ProjectReviewService = field(default_factory=ProjectReviewService)
    access_profile_service: ProductAccessProfileService = field(
        default_factory=ProductAccessProfileService
    )
    human_approval_service: ProductHumanApprovalService = field(
        default_factory=ProductHumanApprovalService
    )
    artifact_service: ProductArtifactEvidenceService | None = None

    @classmethod
    def defaults(cls) -> ProductApiServices:
        return cls(
            boundary_service=ProductBoundaryService(),
            access_profile_service=ProductAccessProfileService(),
            governance_service=GovernanceValidationService(),
            runtime_service=ProductRuntimeSnapshotService(),
            status_service=RuntimeStatusService.defaults(),
            project_review_service=ProjectReviewService(),
            human_approval_service=ProductHumanApprovalService(),
        )

    def health(self) -> HealthStatus:
        dependencies = (
            self.boundary_service.dependency_status(),
            self.access_profile_service.dependency_status(),
            self.governance_service.dependency_status(),
            self.project_review_service.dependency_status(),
            self.runtime_service.dependency_status(),
            self.status_service.dependency_status(),
            self.human_approval_service.dependency_status(),
            self._artifact_service().dependency_status(),
        )
        return HealthStatus(dependencies=dependencies)

    def boundary(self) -> BoundarySnapshot:
        return self.boundary_service.boundary()

    def access_profile(self) -> ProductAccessProfileSnapshot:
        return self.access_profile_service.profile()

    def validate_governance_request(
        self,
        request: GovernanceValidationRequest,
    ) -> GovernanceValidationResponse:
        return self.governance_service.validate(request)

    def project(self) -> ProjectReview:
        return self.project_review_service.project()

    def intake(self) -> ProjectIntakeReview:
        return self.project_review_service.intake()

    def requirements(self) -> ProjectRequirementsReview:
        return self.project_review_service.requirements()

    def architecture(self) -> ProjectArchitectureReview:
        return self.project_review_service.architecture()

    def capabilities_review(self) -> CapabilityReview:
        return self.project_review_service.capabilities_review()

    def generated_plan(self) -> GeneratedPlanSnapshot:
        return self.runtime_service.generated_plan()

    def generated_dag(self) -> GeneratedDagSnapshot:
        return self.runtime_service.generated_dag()

    def tasks(self) -> TaskCollectionSnapshot:
        return self.runtime_service.tasks()

    def executions(self) -> ExecutionCollectionSnapshot:
        return self.runtime_service.executions()

    def repairs(self) -> RepairCollectionSnapshot:
        return self.runtime_service.repairs()

    def runtime_state(self) -> RuntimeStateSnapshot:
        return self.runtime_service.runtime_state()

    def runtime_status(self) -> RuntimeStatus:
        return self.status_service.status()

    def runtime_component_status(self, name: str) -> RuntimeComponentStatus:
        return self.status_service.component_status(name)

    def artifact_metadata(self) -> ArtifactMetadataSnapshot:
        return self._artifact_service().artifact_metadata()

    def requirement_provenance(self, requirement_id: str) -> ProvenanceTraceSnapshot:
        return self._artifact_service().requirement_provenance(requirement_id)

    def human_gate_reviews(self) -> HumanGateReviewCollectionSnapshot:
        return self.human_approval_service.reviews()

    def human_gate_review(self, gate_id: str) -> HumanGateReviewSnapshot:
        return self.human_approval_service.review(gate_id)

    def validate_human_gate_evidence(
        self,
        gate_id: str,
        request: HumanGateEvidenceValidationRequest,
    ) -> HumanGateEvidenceValidationResponse:
        return self.human_approval_service.validate_evidence(gate_id, request)

    def approve_human_gate(
        self,
        gate_id: str,
        request: HumanGateApprovalRequest,
    ) -> HumanGateDecisionResponse:
        return self.human_approval_service.approve(gate_id, request)

    def reject_human_gate(
        self,
        gate_id: str,
        request: HumanGateRejectionRequest,
    ) -> HumanGateDecisionResponse:
        return self.human_approval_service.reject(gate_id, request)

    def scoped_human_gate_decision(
        self,
        gate_id: str,
        request: HumanGateScopedDecisionRequest,
    ) -> HumanGateDecisionResponse:
        return self.human_approval_service.scoped_decision(gate_id, request)

    def _artifact_service(self) -> ProductArtifactEvidenceService:
        if self.artifact_service is not None:
            return self.artifact_service
        return ProductArtifactEvidenceService(
            artifacts_loader=self.runtime_service.artifacts_loader,
            session_factory=self.runtime_service.session_factory,
        )


def _artifact_metadata_snapshot(
    graph: ArtifactEvidenceGraph,
    *,
    product_plan_id: str,
    plan_version: str,
) -> ArtifactMetadataSnapshot:
    graph_payload = graph.as_dict()
    return ArtifactMetadataSnapshot(
        project_id=graph.project_id,
        product_plan_id=product_plan_id,
        plan_version=plan_version,
        graph_available=True,
        graph_id=graph.graph_id,
        artifact_graph_contract_version=graph.contract_version,
        artifact_graph_schema_version=graph.schema_version,
        evidence_graph_hash=graph.evidence_graph_hash,
        source_knowledge_graph_hash=graph.source_knowledge_graph_hash,
        source_project_memory_hash=graph.source_project_memory_hash,
        requirements_covered=graph.requirements_covered,
        capabilities_covered=graph.capabilities_covered,
        service_boundaries=tuple(
            ArtifactEvidenceServiceBoundarySnapshot(
                service=boundary.service,
                interface_id=boundary.interface_id,
                contract_version=boundary.contract_version,
                schema_version=boundary.schema_version,
            )
            for boundary in graph.service_boundaries
        ),
        artifacts=tuple(
            _artifact_record_metadata(node)
            for node in graph.nodes
            if node.node_type == "artifact_evidence_record"
        ),
        summary=graph_payload["summary"],
        authority=_runtime_authority(),
    )


def _artifact_record_metadata(node: Any) -> ArtifactRecordMetadataSnapshot:
    payload = _mapping(node.payload)
    return ArtifactRecordMetadataSnapshot(
        artifact_id=node.native_id,
        node_id=node.node_id,
        kind=_optional_str(payload.get("kind")),
        source=node.source,
        project_id=_optional_str(payload.get("project_id")),
        task_id=_optional_str(payload.get("task_id")),
        execution_id=_optional_str(payload.get("execution_id")),
        commit_hash=_optional_str(payload.get("commit_hash")),
        tree_hash=_optional_str(payload.get("tree_hash")),
        payload_hash=node.payload_hash,
    )


def _provenance_node_snapshot(node: Any) -> ProvenanceNodeSnapshot:
    return ProvenanceNodeSnapshot(
        node_id=node.node_id,
        node_type=node.node_type,
        native_id=node.native_id,
        source=node.source,
        classification=node.classification,
        payload_hash=node.payload_hash,
    )


def _provenance_edge_snapshot(edge: Any) -> ProvenanceEdgeSnapshot:
    return ProvenanceEdgeSnapshot(
        edge_id=edge.edge_id,
        edge_type=edge.edge_type,
        source_node_id=edge.source_node_id,
        target_node_id=edge.target_node_id,
        provenance_refs=edge.provenance_refs,
        payload_hash=edge.payload_hash,
    )


def _readiness_status() -> RuntimeComponentStatus:
    return RuntimeComponentStatus(
        name="readiness",
        boundary="ai_ent.scheduler.readiness.TaskReadinessService",
        status="available",
        summary="readiness policy is available for passive inspection",
        details={"human_gates": "explicit_control_plane_gate_required"},
    )


def _recovery_status() -> RuntimeComponentStatus:
    return RuntimeComponentStatus(
        name="recovery",
        boundary="ai_ent.scheduler.recovery.SchedulerRecoveryService",
        status="available",
        summary="recovery policy is available for passive inspection",
        details={"actions_performed": False},
    )


def _docker_status() -> RuntimeComponentStatus:
    docker_available = shutil.which("docker") is not None
    compose_available = shutil.which("docker-compose") is not None
    return RuntimeComponentStatus(
        name="docker",
        boundary="passive executable discovery",
        status="available" if docker_available else "unavailable",
        summary="Docker executable discovery completed",
        details={
            "docker_command_available": docker_available,
            "docker_compose_command_available": compose_available,
        },
    )


def _codex_status() -> RuntimeComponentStatus:
    codex_available = shutil.which("codex") is not None
    return RuntimeComponentStatus(
        name="codex",
        boundary="passive executable discovery",
        status="available" if codex_available else "unavailable",
        summary="Codex executable discovery completed",
        details={"codex_command_available": codex_available},
    )


def _postgresql_status() -> RuntimeComponentStatus:
    try:
        settings = load_database_settings()
    except DatabaseConfigError:
        return RuntimeComponentStatus(
            name="postgresql",
            boundary="ai_ent.persistence.database.Database.health",
            status="unknown",
            summary="PostgreSQL configuration is incomplete",
            details={"configured": False},
        )

    database = Database(settings)
    try:
        health = database.health()
    finally:
        database.dispose()
    return RuntimeComponentStatus(
        name="postgresql",
        boundary="ai_ent.persistence.database.Database.health",
        status="available" if health.ok else "unavailable",
        summary="PostgreSQL health probe completed",
        details={
            "configured": True,
            "driver": settings.driver,
            "health": "ok" if health.ok else "failed",
        },
    )


def _component_probe(probes: RuntimeProbeProvider, name: str) -> RuntimeProbe:
    if name == "readiness":
        return probes.readiness
    if name == "recovery":
        return probes.recovery
    if name == "docker":
        return probes.docker
    if name == "codex":
        return probes.codex
    if name == "postgresql":
        return probes.postgresql
    raise ValueError(f"unknown runtime component: {name}")


def _component_name(name: str) -> RuntimeComponentName:
    if name in {"readiness", "recovery", "docker", "codex", "postgresql"}:
        return cast(RuntimeComponentName, name)
    return "readiness"


def _safe_component_status(status: RuntimeComponentStatus) -> RuntimeComponentStatus:
    return RuntimeComponentStatus(
        name=status.name,
        boundary=_safe_probe_text(status.boundary, fallback=f"{status.name} status probe"),
        status=status.status,
        summary=_safe_probe_text(status.summary, fallback="status detail redacted"),
        details=_safe_component_details(status.details),
        passive_probe=status.passive_probe,
        actions_performed=False,
        grants_control_plane_authority=False,
        gate_approval_authority=False,
        verifier_bypass_authority=False,
        commit_boundary_bypass_authority=False,
        scheduling_authority=False,
        runtime_execution_authority=False,
        policy_weakening_authority=False,
        secret_values_exposed=False,
    )


def _safe_probe_text(value: str, *, fallback: str) -> str:
    if is_secret_field_name(value):
        return fallback
    return value


def _safe_component_details(value: object) -> dict[str, object]:
    details = _redact_secret_text_values(redact_secret_details(value))
    if isinstance(details, dict):
        return details
    return {}


def _redact_secret_text_values(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _redact_secret_text_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secret_text_values(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_secret_text_values(item) for item in value]
    if isinstance(value, str) and is_secret_field_name(value):
        return REDACTED
    return value


def _runtime_authority() -> RuntimeAuthoritySnapshot:
    return RuntimeAuthoritySnapshot(mandatory_verification_commands=MANDATORY_VERIFICATION_COMMANDS)


def _productization_dag(artifacts: ProductRuntimeHandoffArtifacts) -> dict[str, Any]:
    return _mapping(artifacts.product_plan.get("productization_dag"))


def _human_gate_definitions(
    artifacts: ProductRuntimeHandoffArtifacts,
) -> tuple[dict[str, Any], ...]:
    return _dict_tuple(artifacts.lock.get("human_gate_definitions"))


def _runtime_dependency_edges(
    artifacts: ProductRuntimeHandoffArtifacts,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(edge[0]), str(edge[1]))
        for edge in artifacts.lock.get("dependency_edges", ())
        if isinstance(edge, list | tuple) and len(edge) == 2
    )


def _downstream_by_task(import_preview: tuple[dict[str, Any], ...]) -> dict[str, tuple[str, ...]]:
    downstream: dict[str, list[str]] = {
        str(contract.get("task_id", "")): [] for contract in import_preview
    }
    for contract in import_preview:
        task_id = str(contract.get("task_id", ""))
        for dependency in _string_tuple(contract.get("depends_on")):
            downstream.setdefault(dependency, []).append(task_id)
    return {
        task_id: tuple(sorted(child for child in children if child))
        for task_id, children in downstream.items()
        if task_id
    }


def _latest_product_import(session: Session) -> RuntimePlanImport | None:
    return session.scalars(
        select(RuntimePlanImport)
        .where(RuntimePlanImport.plan_id == PRODUCT_PLAN_ID)
        .order_by(RuntimePlanImport.imported_at.desc(), RuntimePlanImport.id.desc())
    ).first()


def _product_task_ids(session: Session) -> tuple[str, ...]:
    return tuple(
        session.scalars(select(Task.id).where(Task.id.like("PRD-TASK-%")).order_by(Task.id)).all()
    )


def _count_product_dependencies(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(TaskDependency)
            .where(TaskDependency.task_id.like("PRD-TASK-%"))
        )
        or 0
    )


def _count_product_executions(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(Execution)
            .join(Task, Execution.task_id == Task.id)
            .where(Task.id.like("PRD-TASK-%"))
        )
        or 0
    )


def _count_failed_product_executions(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(Execution)
            .join(Task, Execution.task_id == Task.id)
            .where(
                Task.id.like("PRD-TASK-%"),
                Execution.status.in_(("failed", "timeout", "cancelled")),
            )
        )
        or 0
    )


def _count_active_product_leases(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(TaskLease)
            .join(Task, TaskLease.task_id == Task.id)
            .where(Task.id.like("PRD-TASK-%"), TaskLease.status == "active")
        )
        or 0
    )


def _executions_by_task(session: Session) -> dict[str, tuple[Execution, ...]]:
    grouped: dict[str, list[Execution]] = {}
    for execution in session.scalars(
        select(Execution)
        .join(Task, Execution.task_id == Task.id)
        .where(Task.id.like("PRD-TASK-%"))
        .order_by(Execution.task_id, Execution.attempt, Execution.id)
    ):
        grouped.setdefault(execution.task_id, []).append(execution)
    return {task_id: tuple(executions) for task_id, executions in grouped.items()}


def _latest_checkpoint(session: Session, execution_id: str) -> Checkpoint | None:
    return session.scalars(
        select(Checkpoint)
        .where(Checkpoint.execution_id == execution_id)
        .order_by(Checkpoint.created_at.desc(), Checkpoint.id.desc())
    ).first()


def _repair_attempt_count(executions: tuple[Execution, ...]) -> int:
    return sum(
        1
        for execution in executions
        if execution.attempt > 1 and execution.status == "failed"
    )


def _repair_action(retryability: str, repair_attempt_count: int, max_attempts: int) -> str:
    if retryability == "RECONCILIATION_REQUIRED":
        return "RECONCILE"
    if retryability == "HUMAN_REQUIRED":
        return "ESCALATE_HUMAN"
    if retryability == "NON_RETRYABLE":
        return "BLOCK"
    if retryability == "TRANSIENT":
        return "RETRY_SAME_EXECUTION_STAGE"
    if repair_attempt_count >= max_attempts:
        return "ESCALATE_HUMAN"
    return "REPAIR_WITH_NEW_EXECUTION"


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _dict_tuple(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, dict))


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(str(item) for item in value)


def _json_string_tuple(value: str) -> tuple[str, ...]:
    try:
        return _string_tuple(json.loads(value))
    except json.JSONDecodeError:
        return ()


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _is_pending_gate_status(status: str) -> bool:
    return status in {"pending", "PENDING_NOT_APPROVED"}


def _allowed_scope_task_ids(review: HumanGateReviewSnapshot) -> tuple[str, ...]:
    scoped: list[str] = []
    seen: set[str] = set()
    for task_id in (review.task_id, *review.downstream_task_ids):
        if task_id and task_id not in seen:
            scoped.append(task_id)
            seen.add(task_id)
    return tuple(scoped)


def _decision_hash(
    gate_id: str,
    requested_decision: str,
    request: dict[str, Any],
) -> str:
    payload = {
        "gate_id": gate_id,
        "requested_decision": requested_decision,
        "request": request,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _hash_payload(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (float, str)):
        try:
            return int(value)
        except ValueError:
            return default
    return default
