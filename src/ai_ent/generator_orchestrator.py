from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.orm import Session

from ai_ent.deterministic_validator import DeterministicValidatorService
from ai_ent.execution_planner import (
    EXECUTION_PLANNER_CONTRACT_VERSION,
    EXECUTION_PLANNER_OUTPUT_SCHEMA_VERSION,
    ExecutionPlannerService,
    WorkerPackagePlan,
)
from ai_ent.project_manifest import GENERATED_MARKER, canonical_bytes
from ai_ent.project_memory import (
    PROJECT_MEMORY_CONTRACT_VERSION,
    PROJECT_MEMORY_SCHEMA_VERSION,
    ProjectMemoryService,
    ProjectMemorySnapshot,
)
from ai_ent.runtime_handoff import (
    DEFAULT_RUNTIME_PROJECT_ID,
    RuntimePlanImporter,
    RuntimePlanStatus,
)

GENERATOR_ORCHESTRATOR_CONTRACT_VERSION = "c20.1"
GENERATOR_ORCHESTRATOR_OUTPUT_SCHEMA_VERSION = "generator-orchestrator-output-v0.1"
WORKER_PACKAGE_INTERFACE_ID = "IF-004"
EVIDENCE_RECORDING_INTERFACE_ID = "IF-005"
NON_AUTHORITATIVE_OUTPUT_STATE = "NON_AUTHORITATIVE"
C18_GENERATOR_REQUIREMENTS = ("FR-005", "INT-002", "SEC-002")
C20_GENERATOR_REQUIREMENTS = (
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
C20_STOP_CONDITIONS = (
    "no_ready_task",
    "pending_human_gate",
    "reconciliation_required",
    "failure_limit",
    "time_limit",
    "task_limit",
    "crash_recovery_before_resume",
    "runtime_error",
)

GeneratorAdapterKind = Literal["model", "tool", "service"]
GeneratorAuthority = Literal[
    "generate",
    "complete_task",
    "commit",
    "push",
    "schedule",
    "verify",
]
GeneratorOrchestrationStatus = Literal["READY", "BLOCKED"]

FORBIDDEN_GENERATOR_AUTHORITIES: tuple[GeneratorAuthority, ...] = (
    "complete_task",
    "commit",
    "push",
    "schedule",
    "verify",
)


@dataclass(frozen=True)
class GeneratorBoundary:
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
class GeneratorContract:
    generator_id: str
    name: str
    adapter_kind: GeneratorAdapterKind
    input_boundary: GeneratorBoundary
    output_boundary: GeneratorBoundary
    capabilities: tuple[str, ...]
    allowed_authorities: tuple[GeneratorAuthority, ...] = ("generate",)

    @property
    def contract_hash(self) -> str:
        return hashlib.sha256(canonical_bytes(self._payload())).hexdigest()

    @property
    def forbidden_authorities(self) -> tuple[GeneratorAuthority, ...]:
        return tuple(
            authority
            for authority in FORBIDDEN_GENERATOR_AUTHORITIES
            if authority in self.allowed_authorities
        )

    def as_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["contract_hash"] = self.contract_hash
        return payload

    def _payload(self) -> dict[str, Any]:
        return {
            "generator_id": self.generator_id,
            "name": self.name,
            "adapter_kind": self.adapter_kind,
            "input_boundary": self.input_boundary.as_dict(),
            "output_boundary": self.output_boundary.as_dict(),
            "capabilities": list(self.capabilities),
            "allowed_authorities": list(self.allowed_authorities),
            "forbidden_authorities": list(self.forbidden_authorities),
        }


@dataclass(frozen=True)
class GeneratorWorkOrder:
    orchestration_id: str
    generator_id: str
    contract_hash: str
    task_id: str
    execution_id: str
    plan_id: str
    plan_version: str
    task_fingerprint: str
    package_hash: str
    input_hash: str
    allowed_paths: tuple[str, ...]
    prohibited_paths: tuple[str, ...]
    denied_authorities: tuple[GeneratorAuthority, ...]
    instructions_hash: str
    timeout_seconds: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "orchestration_id": self.orchestration_id,
            "generator_id": self.generator_id,
            "contract_hash": self.contract_hash,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "task_fingerprint": self.task_fingerprint,
            "package_hash": self.package_hash,
            "input_hash": self.input_hash,
            "allowed_paths": list(self.allowed_paths),
            "prohibited_paths": list(self.prohibited_paths),
            "denied_authorities": list(self.denied_authorities),
            "instructions_hash": self.instructions_hash,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class GeneratorEvidenceRecord:
    evidence_id: str
    orchestration_id: str
    generator_id: str
    task_id: str
    execution_id: str
    package_hash: str
    input_hash: str
    output_authority_state: str
    validation_required: str

    def as_dict(self) -> dict[str, str]:
        return {
            "evidence_id": self.evidence_id,
            "orchestration_id": self.orchestration_id,
            "generator_id": self.generator_id,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "package_hash": self.package_hash,
            "input_hash": self.input_hash,
            "output_authority_state": self.output_authority_state,
            "validation_required": self.validation_required,
        }


@dataclass(frozen=True)
class GeneratorRuntimeEvidence:
    evidence_id: str
    orchestration_id: str
    project_id: str
    task_id: str
    execution_id: str
    plan_id: str
    plan_version: str
    runtime_status_hash: str
    project_memory_hash: str
    deterministic_validation_hash: str
    requirements_covered: tuple[str, ...]
    capabilities_covered: tuple[str, ...]
    service_boundaries: tuple[GeneratorBoundary, ...]
    human_gate_ids: tuple[str, ...]
    human_gate_state: str
    stop_conditions: tuple[str, ...]
    worker_package_hash: str | None
    work_order_input_hash: str | None
    output_authority_state: str = NON_AUTHORITATIVE_OUTPUT_STATE

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "orchestration_id": self.orchestration_id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "runtime_status_hash": self.runtime_status_hash,
            "project_memory_hash": self.project_memory_hash,
            "deterministic_validation_hash": self.deterministic_validation_hash,
            "requirements_covered": list(self.requirements_covered),
            "capabilities_covered": list(self.capabilities_covered),
            "service_boundaries": [boundary.as_dict() for boundary in self.service_boundaries],
            "human_gate_ids": list(self.human_gate_ids),
            "human_gate_state": self.human_gate_state,
            "stop_conditions": list(self.stop_conditions),
            "worker_package_hash": self.worker_package_hash,
            "work_order_input_hash": self.work_order_input_hash,
            "output_authority_state": self.output_authority_state,
        }


@dataclass(frozen=True)
class RuntimeTaskContext:
    requirements: tuple[str, ...]
    capabilities: tuple[str, ...]
    human_gate_ids: tuple[str, ...]
    human_gate_state: str
    plan_id: str | None
    plan_version: str | None


@dataclass(frozen=True)
class GeneratorOrchestrationResult:
    contract_version: str
    schema_version: str
    status: GeneratorOrchestrationStatus
    orchestration_id: str
    generator_contracts: tuple[GeneratorContract, ...]
    work_orders: tuple[GeneratorWorkOrder, ...]
    worker_package_plans: tuple[WorkerPackagePlan, ...]
    evidence_records: tuple[GeneratorEvidenceRecord, ...]
    requirements_covered: tuple[str, ...] = C18_GENERATOR_REQUIREMENTS
    capabilities_covered: tuple[str, ...] = ("C18",)
    runtime_evidence: tuple[GeneratorRuntimeEvidence, ...] = ()
    blockers: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "READY"

    @property
    def output_hash(self) -> str:
        return hashlib.sha256(canonical_bytes(self._payload())).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["output_hash"] = self.output_hash
        return payload

    def _payload(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "component_id": "CMP-005",
            "capability_id": "C20" if "C20" in self.capabilities_covered else "C18",
            "requirements": list(self.requirements_covered),
            "contract_version": self.contract_version,
            "schema_version": self.schema_version,
            "status": self.status,
            "orchestration_id": self.orchestration_id,
            "input_boundary": {
                "service": "execution-planner",
                "contract_version": EXECUTION_PLANNER_CONTRACT_VERSION,
                "schema_version": EXECUTION_PLANNER_OUTPUT_SCHEMA_VERSION,
                "interface_id": WORKER_PACKAGE_INTERFACE_ID,
            },
            "output_boundary": {
                "service": "artifact-evidence-graph",
                "contract_version": self.contract_version,
                "schema_version": self.schema_version,
                "interface_id": EVIDENCE_RECORDING_INTERFACE_ID,
            },
            "generator_contracts": [
                contract.as_dict()
                for contract in sorted(self.generator_contracts, key=lambda item: item.generator_id)
            ],
            "work_orders": [
                work_order.as_dict()
                for work_order in sorted(self.work_orders, key=lambda item: item.orchestration_id)
            ],
            "worker_package_plans": [
                plan.as_dict()
                for plan in sorted(
                    self.worker_package_plans,
                    key=lambda item: (item.package.task_id, item.package.execution_id),
                )
            ],
            "evidence_records": [
                evidence.as_dict()
                for evidence in sorted(self.evidence_records, key=lambda item: item.evidence_id)
            ],
            "runtime_evidence": [
                evidence.as_dict()
                for evidence in sorted(self.runtime_evidence, key=lambda item: item.evidence_id)
            ],
            "blockers": list(self.blockers),
            "summary": {
                "ready": self.ok,
                "work_order_count": len(self.work_orders),
                "evidence_record_count": len(self.evidence_records),
                "runtime_evidence_count": len(self.runtime_evidence),
                "requirements_covered": list(self.requirements_covered),
                "capabilities_covered": list(self.capabilities_covered),
                "human_approval_required": any(
                    evidence.human_gate_state != "approved" for evidence in self.runtime_evidence
                ),
                "output_authority_state": NON_AUTHORITATIVE_OUTPUT_STATE,
                "completion_authority_granted": _authority_granted(
                    self.generator_contracts,
                    "complete_task",
                ),
                "commit_authority_granted": _authority_granted(self.generator_contracts, "commit"),
                "push_authority_granted": _authority_granted(self.generator_contracts, "push"),
                "schedule_authority_granted": _authority_granted(
                    self.generator_contracts,
                    "schedule",
                ),
                "verification_authority_granted": _authority_granted(
                    self.generator_contracts,
                    "verify",
                ),
            },
        }


class GeneratorOrchestratorService:
    """Service boundary for coordinating specialized C18 and C20 generators."""

    def __init__(
        self,
        *,
        planner: ExecutionPlannerService | None = None,
        runtime_importer: RuntimePlanImporter | None = None,
        project_memory: ProjectMemoryService | None = None,
        validator: DeterministicValidatorService | None = None,
        generator_contracts: tuple[GeneratorContract, ...] | None = None,
        contract_version: str = GENERATOR_ORCHESTRATOR_CONTRACT_VERSION,
        schema_version: str = GENERATOR_ORCHESTRATOR_OUTPUT_SCHEMA_VERSION,
    ) -> None:
        self.planner = planner or ExecutionPlannerService()
        self.runtime_importer = runtime_importer or RuntimePlanImporter()
        self.project_memory = project_memory or ProjectMemoryService()
        self.validator = validator or DeterministicValidatorService()
        contracts = generator_contracts or default_generator_contracts()
        self.generator_contracts = tuple(sorted(contracts, key=lambda item: item.generator_id))
        self.contract_version = contract_version
        self.schema_version = schema_version

    def coordinate_task(
        self,
        session: Session,
        *,
        task_id: str,
        execution_id: str,
        generator_id: str = "codex-generator",
        project_id: str = DEFAULT_RUNTIME_PROJECT_ID,
        repository_path: Path = Path("."),
        timeout_seconds: int = 900,
        required_capability: str = "C18",
        requirements_covered: tuple[str, ...] = C18_GENERATOR_REQUIREMENTS,
    ) -> GeneratorOrchestrationResult:
        contract = self._contract(generator_id)
        orchestration_id = _orchestration_id(generator_id, task_id, execution_id)
        blockers = list(
            _contract_blockers(
                contract,
                contract_version=self.contract_version,
                schema_version=self.schema_version,
            )
        )
        package_plan: WorkerPackagePlan | None = None
        if contract is None:
            blockers.append(f"generator contract not registered:{generator_id}")
        elif required_capability not in contract.capabilities:
            blockers.append(f"generator contract does not declare {required_capability} capability:{generator_id}")

        if not blockers:
            try:
                package_plan = self.planner.build_worker_package(
                    session,
                    task_id=task_id,
                    execution_id=execution_id,
                    project_id=project_id,
                    repository_path=repository_path,
                    timeout_seconds=timeout_seconds,
                )
            except ValueError as exc:
                blockers.append(str(exc))

        work_orders: tuple[GeneratorWorkOrder, ...] = ()
        evidence_records: tuple[GeneratorEvidenceRecord, ...] = ()
        worker_package_plans: tuple[WorkerPackagePlan, ...] = ()
        if contract is not None and package_plan is not None and not blockers:
            work_order = _work_order(orchestration_id, contract, package_plan)
            work_orders = (work_order,)
            evidence_records = (_evidence_record(work_order),)
            worker_package_plans = (package_plan,)

        capabilities_covered = (required_capability,)
        return GeneratorOrchestrationResult(
            contract_version=self.contract_version,
            schema_version=self.schema_version,
            status="BLOCKED" if blockers else "READY",
            orchestration_id=orchestration_id,
            generator_contracts=(contract,) if contract is not None else (),
            work_orders=work_orders,
            worker_package_plans=worker_package_plans,
            evidence_records=evidence_records,
            requirements_covered=requirements_covered,
            capabilities_covered=capabilities_covered,
            blockers=tuple(sorted(blockers)),
        )

    def coordinate_runtime_task(
        self,
        session: Session,
        *,
        task_id: str,
        execution_id: str,
        generator_id: str = "codex-generator",
        project_id: str = DEFAULT_RUNTIME_PROJECT_ID,
        repository_path: Path = Path("."),
        manifest_root: Path = Path("manifest/project/ai-ent"),
        timeout_seconds: int = 900,
    ) -> GeneratorOrchestrationResult:
        contract = self._contract(generator_id)
        orchestration_id = _orchestration_id(generator_id, task_id, execution_id)
        blockers = list(
            _contract_blockers(
                contract,
                contract_version=self.contract_version,
                schema_version=self.schema_version,
            )
        )
        if contract is None:
            blockers.append(f"generator contract not registered:{generator_id}")
        elif "C20" not in contract.capabilities:
            blockers.append(f"generator contract does not declare C20 capability:{generator_id}")

        runtime_status = self.runtime_importer.status(session, project_id=project_id)
        snapshot: ProjectMemorySnapshot | None = None
        task_context = RuntimeTaskContext(
            requirements=(),
            capabilities=(),
            human_gate_ids=(),
            human_gate_state="none",
            plan_id=runtime_status.plan_id,
            plan_version=runtime_status.plan_version,
        )
        if runtime_status.import_status != "imported":
            blockers.append(f"runtime plan is not imported:{project_id}")
        else:
            snapshot = self.project_memory.snapshot(session, project_id=project_id)
            task_context = _runtime_task_context(snapshot, task_id, runtime_status)
            if not task_context.requirements:
                blockers.append(f"runtime task binding not found:{task_id}")
            elif set(C20_GENERATOR_REQUIREMENTS) - set(task_context.requirements):
                missing = sorted(set(C20_GENERATOR_REQUIREMENTS) - set(task_context.requirements))
                blockers.append(f"C20 requirement coverage missing:{task_id}:{','.join(missing)}")
            if "C20" not in task_context.capabilities:
                blockers.append(f"runtime task does not implement C20:{task_id}")
            if task_context.human_gate_state != "approved":
                blockers.append(f"human approval required:{task_id}:{task_context.human_gate_state}")

        validation = self.validator.validate(manifest_root)
        if not validation.ok:
            blockers.append("deterministic validation failed")

        package_plan: WorkerPackagePlan | None = None
        work_orders: tuple[GeneratorWorkOrder, ...] = ()
        evidence_records: tuple[GeneratorEvidenceRecord, ...] = ()
        worker_package_plans: tuple[WorkerPackagePlan, ...] = ()
        if contract is not None and not blockers:
            try:
                package_plan = self.planner.build_worker_package(
                    session,
                    task_id=task_id,
                    execution_id=execution_id,
                    project_id=project_id,
                    repository_path=repository_path,
                    timeout_seconds=timeout_seconds,
                )
            except ValueError as exc:
                blockers.append(str(exc))

        if contract is not None and package_plan is not None and not blockers:
            work_order = _work_order(orchestration_id, contract, package_plan)
            work_orders = (work_order,)
            evidence_records = (_evidence_record(work_order),)
            worker_package_plans = (package_plan,)

        runtime_evidence = ()
        if contract is not None and snapshot is not None:
            runtime_evidence = (
                _runtime_evidence(
                    orchestration_id=orchestration_id,
                    project_id=project_id,
                    task_id=task_id,
                    execution_id=execution_id,
                    runtime_status=runtime_status,
                    snapshot=snapshot,
                    validation_hash=validation.report_hash,
                    task_context=task_context,
                    package_plan=package_plan,
                    work_order=work_orders[0] if work_orders else None,
                ),
            )

        requirements = task_context.requirements or C20_GENERATOR_REQUIREMENTS
        capabilities = task_context.capabilities or ("C20",)
        return GeneratorOrchestrationResult(
            contract_version=self.contract_version,
            schema_version=self.schema_version,
            status="BLOCKED" if blockers else "READY",
            orchestration_id=orchestration_id,
            generator_contracts=(contract,) if contract is not None else (),
            work_orders=work_orders,
            worker_package_plans=worker_package_plans,
            evidence_records=evidence_records,
            requirements_covered=requirements,
            capabilities_covered=capabilities,
            runtime_evidence=runtime_evidence,
            blockers=tuple(sorted(blockers)),
        )

    def _contract(self, generator_id: str) -> GeneratorContract | None:
        return next(
            (
                contract
                for contract in self.generator_contracts
                if contract.generator_id == generator_id
            ),
            None,
        )


GeneratorOrchestrator = GeneratorOrchestratorService


def default_generator_contracts() -> tuple[GeneratorContract, ...]:
    return (
        GeneratorContract(
            generator_id="codex-generator",
            name="Bounded Codex generator worker",
            adapter_kind="tool",
            input_boundary=GeneratorBoundary(
                service="execution-planner",
                interface_id=WORKER_PACKAGE_INTERFACE_ID,
                contract_version=EXECUTION_PLANNER_CONTRACT_VERSION,
                schema_version=EXECUTION_PLANNER_OUTPUT_SCHEMA_VERSION,
            ),
            output_boundary=GeneratorBoundary(
                service="artifact-evidence-graph",
                interface_id=EVIDENCE_RECORDING_INTERFACE_ID,
                contract_version=GENERATOR_ORCHESTRATOR_CONTRACT_VERSION,
                schema_version=GENERATOR_ORCHESTRATOR_OUTPUT_SCHEMA_VERSION,
            ),
            capabilities=("C18", "C20"),
            allowed_authorities=("generate",),
        ),
    )


def coordinate_generator_task(
    session: Session,
    *,
    task_id: str,
    execution_id: str,
    generator_id: str = "codex-generator",
    project_id: str = DEFAULT_RUNTIME_PROJECT_ID,
    repository_path: Path = Path("."),
    timeout_seconds: int = 900,
) -> GeneratorOrchestrationResult:
    return GeneratorOrchestratorService().coordinate_task(
        session,
        task_id=task_id,
        execution_id=execution_id,
        generator_id=generator_id,
        project_id=project_id,
        repository_path=repository_path,
        timeout_seconds=timeout_seconds,
    )


def _contract_blockers(
    contract: GeneratorContract | None,
    *,
    contract_version: str,
    schema_version: str,
) -> tuple[str, ...]:
    if contract is None:
        return ()
    blockers: list[str] = []
    if contract.input_boundary.service != "execution-planner":
        blockers.append(f"generator input boundary is not execution-planner:{contract.generator_id}")
    if contract.input_boundary.interface_id != WORKER_PACKAGE_INTERFACE_ID:
        blockers.append(f"generator input interface is not {WORKER_PACKAGE_INTERFACE_ID}:{contract.generator_id}")
    if contract.input_boundary.contract_version != EXECUTION_PLANNER_CONTRACT_VERSION:
        blockers.append(
            f"generator input contract is not {EXECUTION_PLANNER_CONTRACT_VERSION}:{contract.generator_id}"
        )
    if contract.input_boundary.schema_version != EXECUTION_PLANNER_OUTPUT_SCHEMA_VERSION:
        blockers.append(
            f"generator input schema is not {EXECUTION_PLANNER_OUTPUT_SCHEMA_VERSION}:{contract.generator_id}"
        )
    if contract.output_boundary.service != "artifact-evidence-graph":
        blockers.append(f"generator output boundary is not artifact-evidence-graph:{contract.generator_id}")
    if contract.output_boundary.interface_id != EVIDENCE_RECORDING_INTERFACE_ID:
        blockers.append(f"generator output interface is not {EVIDENCE_RECORDING_INTERFACE_ID}:{contract.generator_id}")
    if contract.output_boundary.contract_version != contract_version:
        blockers.append(
            f"generator output contract is not {contract_version}:{contract.generator_id}"
        )
    if contract.output_boundary.schema_version != schema_version:
        blockers.append(
            f"generator output schema is not {schema_version}:{contract.generator_id}"
        )
    for authority in contract.forbidden_authorities:
        blockers.append(f"forbidden authority granted:{contract.generator_id}:{authority}")
    return tuple(blockers)


def _work_order(
    orchestration_id: str,
    contract: GeneratorContract,
    package_plan: WorkerPackagePlan,
) -> GeneratorWorkOrder:
    package = package_plan.package
    input_payload = {
        "contract_hash": contract.contract_hash,
        "package_hash": package_plan.package_hash,
        "task_id": package.task_id,
        "execution_id": package.execution_id,
        "task_fingerprint": package_plan.task_fingerprint,
        "plan_id": package_plan.plan_id,
        "plan_version": package_plan.plan_version,
        "dry_run_hash": package_plan.dry_run_hash,
        "allowed_paths": list(package.allowed_paths),
        "prohibited_paths": list(package.prohibited_paths),
        "timeout_seconds": package.timeout_seconds,
    }
    return GeneratorWorkOrder(
        orchestration_id=orchestration_id,
        generator_id=contract.generator_id,
        contract_hash=contract.contract_hash,
        task_id=package.task_id,
        execution_id=package.execution_id,
        plan_id=package_plan.plan_id,
        plan_version=package_plan.plan_version,
        task_fingerprint=package_plan.task_fingerprint,
        package_hash=package_plan.package_hash,
        input_hash=hashlib.sha256(canonical_bytes(input_payload)).hexdigest(),
        allowed_paths=package.allowed_paths,
        prohibited_paths=package.prohibited_paths,
        denied_authorities=FORBIDDEN_GENERATOR_AUTHORITIES,
        instructions_hash=hashlib.sha256(package.instructions.encode("utf-8")).hexdigest(),
        timeout_seconds=package.timeout_seconds,
    )


def _evidence_record(work_order: GeneratorWorkOrder) -> GeneratorEvidenceRecord:
    evidence_payload = {
        "orchestration_id": work_order.orchestration_id,
        "generator_id": work_order.generator_id,
        "task_id": work_order.task_id,
        "execution_id": work_order.execution_id,
        "package_hash": work_order.package_hash,
        "input_hash": work_order.input_hash,
        "output_authority_state": NON_AUTHORITATIVE_OUTPUT_STATE,
    }
    evidence_hash = hashlib.sha256(canonical_bytes(evidence_payload)).hexdigest()
    return GeneratorEvidenceRecord(
        evidence_id=f"GEN-EVID-{evidence_hash[:12]}",
        orchestration_id=work_order.orchestration_id,
        generator_id=work_order.generator_id,
        task_id=work_order.task_id,
        execution_id=work_order.execution_id,
        package_hash=work_order.package_hash,
        input_hash=work_order.input_hash,
        output_authority_state=NON_AUTHORITATIVE_OUTPUT_STATE,
        validation_required="deterministic validation and scheduler finalization before completion",
    )


def _runtime_task_context(
    snapshot: ProjectMemorySnapshot,
    task_id: str,
    runtime_status: RuntimePlanStatus,
) -> RuntimeTaskContext:
    task_binding = next(
        (
            entry
            for entry in snapshot.entries
            if entry.source == "runtime-task-binding" and entry.task_id == task_id
        ),
        None,
    )
    implements = task_binding.payload.get("implements", {}) if task_binding is not None else {}
    gates = tuple(
        entry
        for entry in snapshot.entries
        if entry.source == "runtime-human-gate" and entry.task_id == task_id
    )
    gate_ids = tuple(sorted(str(entry.payload["gate_id"]) for entry in gates))
    gate_states = {str(entry.payload.get("status", "")) for entry in gates}
    if not gates or gate_states == {"approved"}:
        gate_state = "approved"
    elif "rejected" in gate_states:
        gate_state = "rejected"
    else:
        gate_state = "pending"
    return RuntimeTaskContext(
        requirements=_string_tuple(implements.get("requirements", ())),
        capabilities=_string_tuple(implements.get("capabilities", ())),
        human_gate_ids=gate_ids,
        human_gate_state=gate_state,
        plan_id=runtime_status.plan_id,
        plan_version=runtime_status.plan_version,
    )


def _runtime_evidence(
    *,
    orchestration_id: str,
    project_id: str,
    task_id: str,
    execution_id: str,
    runtime_status: RuntimePlanStatus,
    snapshot: ProjectMemorySnapshot,
    validation_hash: str,
    task_context: RuntimeTaskContext,
    package_plan: WorkerPackagePlan | None,
    work_order: GeneratorWorkOrder | None,
) -> GeneratorRuntimeEvidence:
    status_payload = runtime_status.as_dict()
    payload = {
        "orchestration_id": orchestration_id,
        "project_id": project_id,
        "task_id": task_id,
        "execution_id": execution_id,
        "plan_id": task_context.plan_id,
        "plan_version": task_context.plan_version,
        "runtime_status_hash": hashlib.sha256(canonical_bytes(status_payload)).hexdigest(),
        "project_memory_hash": snapshot.memory_hash,
        "deterministic_validation_hash": validation_hash,
        "requirements_covered": list(task_context.requirements),
        "capabilities_covered": list(task_context.capabilities),
        "human_gate_ids": list(task_context.human_gate_ids),
        "human_gate_state": task_context.human_gate_state,
        "worker_package_hash": package_plan.package_hash if package_plan is not None else None,
        "work_order_input_hash": work_order.input_hash if work_order is not None else None,
    }
    evidence_hash = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    return GeneratorRuntimeEvidence(
        evidence_id=f"GEN-RUNTIME-EVID-{evidence_hash[:12]}",
        orchestration_id=orchestration_id,
        project_id=project_id,
        task_id=task_id,
        execution_id=execution_id,
        plan_id=task_context.plan_id or "",
        plan_version=task_context.plan_version or "",
        runtime_status_hash=str(payload["runtime_status_hash"]),
        project_memory_hash=snapshot.memory_hash,
        deterministic_validation_hash=validation_hash,
        requirements_covered=task_context.requirements,
        capabilities_covered=task_context.capabilities,
        service_boundaries=_c20_service_boundaries(),
        human_gate_ids=task_context.human_gate_ids,
        human_gate_state=task_context.human_gate_state,
        stop_conditions=C20_STOP_CONDITIONS,
        worker_package_hash=package_plan.package_hash if package_plan is not None else None,
        work_order_input_hash=work_order.input_hash if work_order is not None else None,
    )


def _c20_service_boundaries() -> tuple[GeneratorBoundary, ...]:
    return (
        GeneratorBoundary(
            service="execution-planner",
            interface_id=WORKER_PACKAGE_INTERFACE_ID,
            contract_version=EXECUTION_PLANNER_CONTRACT_VERSION,
            schema_version=EXECUTION_PLANNER_OUTPUT_SCHEMA_VERSION,
        ),
        GeneratorBoundary(
            service="runtime-kernel",
            interface_id=WORKER_PACKAGE_INTERFACE_ID,
            contract_version=GENERATOR_ORCHESTRATOR_CONTRACT_VERSION,
            schema_version=GENERATOR_ORCHESTRATOR_OUTPUT_SCHEMA_VERSION,
        ),
        GeneratorBoundary(
            service="project-memory",
            interface_id=EVIDENCE_RECORDING_INTERFACE_ID,
            contract_version=PROJECT_MEMORY_CONTRACT_VERSION,
            schema_version=PROJECT_MEMORY_SCHEMA_VERSION,
        ),
        GeneratorBoundary(
            service="artifact-evidence-graph",
            interface_id=EVIDENCE_RECORDING_INTERFACE_ID,
            contract_version=GENERATOR_ORCHESTRATOR_CONTRACT_VERSION,
            schema_version=GENERATOR_ORCHESTRATOR_OUTPUT_SCHEMA_VERSION,
        ),
        GeneratorBoundary(
            service="deterministic-validator",
            interface_id=EVIDENCE_RECORDING_INTERFACE_ID,
            contract_version="c03.1",
            schema_version="deterministic-validation-report-v0.1",
        ),
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(sorted(str(item) for item in value))
    return ()


def _orchestration_id(generator_id: str, task_id: str, execution_id: str) -> str:
    payload = {
        "generator_id": generator_id,
        "task_id": task_id,
        "execution_id": execution_id,
        "contract_version": GENERATOR_ORCHESTRATOR_CONTRACT_VERSION,
    }
    digest = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    return f"GEN-ORCH-{digest[:12]}"


def _authority_granted(
    contracts: tuple[GeneratorContract, ...],
    authority: GeneratorAuthority,
) -> bool:
    return any(authority in contract.allowed_authorities for contract in contracts)
