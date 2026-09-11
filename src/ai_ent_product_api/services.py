from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_ent.external_project import (
    CONTROL_PLANE_AUTHORITY,
    DEFAULT_PROHIBITED_PATHS,
    MANDATORY_VERIFICATION_COMMANDS,
)
from ai_ent.governance_contract import validate_governance_evolution_request
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
from ai_ent.runtime_handoff import DEFAULT_RUNTIME_PROJECT_ID
from ai_ent.scheduler.readiness import TaskReadinessService
from ai_ent.scheduler.repair import FailureClassifier, RepairPolicy
from ai_ent_product_api.schemas import (
    BoundarySnapshot,
    ExecutionCollectionSnapshot,
    ExecutionSnapshot,
    GeneratedDagEdge,
    GeneratedDagSnapshot,
    GeneratedHumanGateSnapshot,
    GeneratedPlanSnapshot,
    GovernanceValidationRequest,
    GovernanceValidationResponse,
    GovernanceValidationSummary,
    HealthStatus,
    RepairCollectionSnapshot,
    RepairSnapshot,
    RuntimeAuthoritySnapshot,
    RuntimeHumanGateStateSnapshot,
    RuntimeStateSnapshot,
    ServiceDependencyStatus,
    TaskCollectionSnapshot,
    TaskSnapshot,
)

ArtifactsLoader = Callable[[], ProductRuntimeHandoffArtifacts]
SessionFactory = Callable[[], Session]


@dataclass(frozen=True)
class ProductBoundaryService:
    def boundary(self) -> BoundarySnapshot:
        return BoundarySnapshot(
            control_plane_authority=CONTROL_PLANE_AUTHORITY,
            mandatory_verification_commands=MANDATORY_VERIFICATION_COMMANDS,
            allowed_operations=(
                "observe_boundary",
                "validate_governance_request",
            ),
            denied_operations=(
                "approve_gate",
                "bypass_commit_boundary",
                "bypass_verifier",
                "commit",
                "execute_runtime",
                "push",
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
class ProductApiServices:
    boundary_service: ProductBoundaryService
    governance_service: GovernanceValidationService
    runtime_service: ProductRuntimeSnapshotService

    @classmethod
    def defaults(cls) -> ProductApiServices:
        return cls(
            boundary_service=ProductBoundaryService(),
            governance_service=GovernanceValidationService(),
            runtime_service=ProductRuntimeSnapshotService(),
        )

    def health(self) -> HealthStatus:
        dependencies = (
            self.boundary_service.dependency_status(),
            self.governance_service.dependency_status(),
            self.runtime_service.dependency_status(),
        )
        return HealthStatus(dependencies=dependencies)

    def boundary(self) -> BoundarySnapshot:
        return self.boundary_service.boundary()

    def validate_governance_request(
        self,
        request: GovernanceValidationRequest,
    ) -> GovernanceValidationResponse:
        return self.governance_service.validate(request)

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
