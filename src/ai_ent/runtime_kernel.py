from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.orm import Session

from ai_ent.deterministic_validator import DeterministicValidatorService
from ai_ent.project_manifest import GENERATED_MARKER, canonical_bytes, compile_project_manifest
from ai_ent.project_memory import (
    PROJECT_MEMORY_CONTRACT_VERSION,
    PROJECT_MEMORY_SCHEMA_VERSION,
    ProjectMemoryService,
    ProjectMemorySnapshot,
)
from ai_ent.runtime_handoff import (
    DEFAULT_RUNTIME_PROJECT_ID,
    RUNTIME_HANDOFF_IMPORTER_VERSION,
    RuntimePlanImporter,
    RuntimePlanStatus,
)
from ai_ent.scheduler.readiness import ReadinessDecision, TaskReadinessService

RUNTIME_KERNEL_CONTRACT_VERSION = "c20.1"
RUNTIME_KERNEL_OUTPUT_SCHEMA_VERSION = "runtime-kernel-output-v0.1"
RUNTIME_KERNEL_SERVICE_NAME = "runtime-kernel"
RUNTIME_KERNEL_COMPONENT_ID = "CMP-006"
RUNTIME_KERNEL_CAPABILITY_ID = "C20"
WORKER_PACKAGE_INTERFACE_ID = "IF-004"
EVIDENCE_RECORDING_INTERFACE_ID = "IF-005"

DEFAULT_MANIFEST_ROOT = Path("manifest/project/ai-ent")


def runtime_requirements_for_capability(
    manifest_root: Path = DEFAULT_MANIFEST_ROOT,
    *,
    capability_id: str = RUNTIME_KERNEL_CAPABILITY_ID,
) -> tuple[str, ...]:
    compiled = compile_project_manifest(manifest_root).compiled
    return tuple(
        sorted(
            str(requirement["id"])
            for requirement in compiled.requirements
            if requirement.get("classification") == "NORMATIVE"
            and capability_id in {str(item) for item in requirement.get("capabilities", [])}
        )
    )


C20_RUNTIME_KERNEL_REQUIREMENTS = runtime_requirements_for_capability()
C20_RUNTIME_STOP_CONDITIONS = (
    "no_ready_task",
    "pending_human_gate",
    "reconciliation_required",
    "failure_limit",
    "time_limit",
    "task_limit",
    "crash_recovery_before_resume",
    "runtime_error",
)

RuntimeKernelStatus = Literal["READY", "BLOCKED"]


@dataclass(frozen=True)
class RuntimeKernelBoundary:
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
class RuntimeTaskContext:
    requirements: tuple[str, ...]
    capabilities: tuple[str, ...]
    human_gate_ids: tuple[str, ...]
    human_gate_state: str
    plan_id: str | None
    plan_version: str | None


@dataclass(frozen=True)
class RuntimeKernelEvaluation:
    contract_version: str
    schema_version: str
    status: RuntimeKernelStatus
    project_id: str
    task_id: str
    execution_id: str
    plan_id: str
    plan_version: str
    requirements_covered: tuple[str, ...]
    capabilities_covered: tuple[str, ...]
    task_context: RuntimeTaskContext
    service_boundaries: tuple[RuntimeKernelBoundary, ...]
    runtime_status_hash: str
    project_memory_hash: str
    deterministic_validation_hash: str
    readiness_status: str
    readiness_reasons: tuple[str, ...]
    ready_task_ids: tuple[str, ...]
    gated_not_ready_task_ids: tuple[str, ...]
    active_stop_conditions: tuple[str, ...]
    supported_stop_conditions: tuple[str, ...] = C20_RUNTIME_STOP_CONDITIONS
    worker_package_permitted: bool = False
    blockers: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "READY"

    @property
    def stop_conditions(self) -> tuple[str, ...]:
        return self.supported_stop_conditions

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
            "component_id": RUNTIME_KERNEL_COMPONENT_ID,
            "capability_id": RUNTIME_KERNEL_CAPABILITY_ID,
            "requirements": list(self.requirements_covered),
            "contract_version": self.contract_version,
            "schema_version": self.schema_version,
            "service": RUNTIME_KERNEL_SERVICE_NAME,
            "status": self.status,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "input_boundary": {
                "service": "runtime-handoff",
                "runtime_status_hash": self.runtime_status_hash,
                "project_memory_hash": self.project_memory_hash,
                "deterministic_validation_hash": self.deterministic_validation_hash,
            },
            "output_boundary": {
                "service": "execution-planner",
                "interface_id": WORKER_PACKAGE_INTERFACE_ID,
                "worker_package_permitted": self.worker_package_permitted,
            },
            "service_boundaries": [boundary.as_dict() for boundary in self.service_boundaries],
            "task_context": {
                "requirements": list(self.task_context.requirements),
                "capabilities": list(self.task_context.capabilities),
                "human_gate_ids": list(self.task_context.human_gate_ids),
                "human_gate_state": self.task_context.human_gate_state,
                "plan_id": self.task_context.plan_id,
                "plan_version": self.task_context.plan_version,
            },
            "scheduler": {
                "readiness_status": self.readiness_status,
                "readiness_reasons": list(self.readiness_reasons),
                "ready_task_ids": list(self.ready_task_ids),
                "gated_not_ready_task_ids": list(self.gated_not_ready_task_ids),
            },
            "stop_conditions": {
                "active": list(self.active_stop_conditions),
                "supported": list(self.supported_stop_conditions),
            },
            "blockers": list(self.blockers),
            "summary": {
                "ready": self.ok,
                "worker_package_permitted": self.worker_package_permitted,
                "requirements_covered": list(self.requirements_covered),
                "capabilities_covered": list(self.capabilities_covered),
                "human_gate_state": self.task_context.human_gate_state,
                "active_stop_conditions": list(self.active_stop_conditions),
            },
        }


class RuntimeKernelService:
    """Deterministic CMP-006 boundary for runtime task admission and evidence."""

    def __init__(
        self,
        *,
        runtime_importer: RuntimePlanImporter | None = None,
        project_memory: ProjectMemoryService | None = None,
        validator: DeterministicValidatorService | None = None,
        readiness: TaskReadinessService | None = None,
        contract_version: str = RUNTIME_KERNEL_CONTRACT_VERSION,
        schema_version: str = RUNTIME_KERNEL_OUTPUT_SCHEMA_VERSION,
    ) -> None:
        self.runtime_importer = runtime_importer or RuntimePlanImporter()
        self.project_memory = project_memory or ProjectMemoryService()
        self.validator = validator or DeterministicValidatorService()
        self.readiness = readiness or TaskReadinessService()
        self.contract_version = contract_version
        self.schema_version = schema_version

    def evaluate_task(
        self,
        session: Session,
        *,
        task_id: str,
        execution_id: str,
        project_id: str = DEFAULT_RUNTIME_PROJECT_ID,
        manifest_root: Path = Path("manifest/project/ai-ent"),
        tasks_attempted_this_run: int = 0,
        max_tasks_per_run: int = 1,
        failures_this_run: int = 0,
        max_failures_per_run: int = 1,
        elapsed_wall_clock_seconds: float = 0.0,
        max_wall_clock_seconds: float = 900.0,
        crash_recovery_required: bool = False,
    ) -> RuntimeKernelEvaluation:
        runtime_status = self.runtime_importer.status(session, project_id=project_id)
        runtime_status_hash = hashlib.sha256(canonical_bytes(runtime_status.as_dict())).hexdigest()
        validation = self.validator.validate(manifest_root)
        blockers: list[str] = []

        if runtime_status.import_status != "imported":
            blockers.append(f"runtime plan is not imported:{project_id}")
            snapshot = ProjectMemorySnapshot(project_id=project_id, entries=())
            task_context = _empty_context(runtime_status)
            readiness = ReadinessDecision(task_id=task_id, status="NOT_FOUND", reasons=("task_missing",))
        else:
            snapshot = self.project_memory.snapshot(session, project_id=project_id)
            task_context = runtime_task_context(snapshot, task_id, runtime_status)
            readiness = self.readiness.evaluate_task(session, task_id)
            if not task_context.requirements:
                blockers.append(f"runtime task binding not found:{task_id}")
            else:
                expected_requirements = runtime_requirements_for_capability(manifest_root)
                missing = sorted(set(expected_requirements) - set(task_context.requirements))
                if missing:
                    blockers.append(f"C20 requirement coverage missing:{task_id}:{','.join(missing)}")
            if "C20" not in task_context.capabilities:
                blockers.append(f"runtime task does not implement C20:{task_id}")
            if task_context.human_gate_state != "approved":
                blockers.append(f"human approval required:{task_id}:{task_context.human_gate_state}")
            if not readiness.ready:
                blockers.append(f"runtime task is not ready:{task_id}:{readiness.status}")

        if not validation.ok:
            blockers.append("deterministic validation failed")
        if tasks_attempted_this_run >= max_tasks_per_run:
            blockers.append("runtime task limit reached")
        if failures_this_run >= max_failures_per_run:
            blockers.append("runtime failure limit reached")
        if elapsed_wall_clock_seconds >= max_wall_clock_seconds:
            blockers.append("runtime wall-clock limit reached")
        if crash_recovery_required:
            blockers.append("crash recovery required before resume")

        active_stop_conditions = _active_stop_conditions(
            runtime_status=runtime_status,
            task_context=task_context,
            readiness=readiness,
            validation_ok=validation.ok,
            imported=runtime_status.import_status == "imported",
            tasks_attempted_this_run=tasks_attempted_this_run,
            max_tasks_per_run=max_tasks_per_run,
            failures_this_run=failures_this_run,
            max_failures_per_run=max_failures_per_run,
            elapsed_wall_clock_seconds=elapsed_wall_clock_seconds,
            max_wall_clock_seconds=max_wall_clock_seconds,
            crash_recovery_required=crash_recovery_required,
        )
        blockers_tuple = tuple(sorted(dict.fromkeys(blockers)))
        worker_package_permitted = not blockers_tuple and readiness.ready
        requirements = task_context.requirements or runtime_requirements_for_capability(manifest_root)
        capabilities = task_context.capabilities or ("C20",)
        return RuntimeKernelEvaluation(
            contract_version=self.contract_version,
            schema_version=self.schema_version,
            status="READY" if worker_package_permitted else "BLOCKED",
            project_id=project_id,
            task_id=task_id,
            execution_id=execution_id,
            plan_id=task_context.plan_id or "",
            plan_version=task_context.plan_version or "",
            requirements_covered=requirements,
            capabilities_covered=capabilities,
            task_context=task_context,
            service_boundaries=runtime_kernel_service_boundaries(),
            runtime_status_hash=runtime_status_hash,
            project_memory_hash=snapshot.memory_hash,
            deterministic_validation_hash=validation.report_hash,
            readiness_status=readiness.status,
            readiness_reasons=readiness.reasons,
            ready_task_ids=runtime_status.ready_tasks,
            gated_not_ready_task_ids=runtime_status.gated_not_ready_tasks,
            active_stop_conditions=active_stop_conditions,
            worker_package_permitted=worker_package_permitted,
            blockers=blockers_tuple,
        )


RuntimeKernel = RuntimeKernelService


def runtime_task_context(
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


def runtime_kernel_service_boundaries() -> tuple[RuntimeKernelBoundary, ...]:
    return (
        RuntimeKernelBoundary(
            service="runtime-handoff",
            interface_id=WORKER_PACKAGE_INTERFACE_ID,
            contract_version=RUNTIME_HANDOFF_IMPORTER_VERSION,
            schema_version="runtime-plan-import-v0.1",
        ),
        RuntimeKernelBoundary(
            service=RUNTIME_KERNEL_SERVICE_NAME,
            interface_id=WORKER_PACKAGE_INTERFACE_ID,
            contract_version=RUNTIME_KERNEL_CONTRACT_VERSION,
            schema_version=RUNTIME_KERNEL_OUTPUT_SCHEMA_VERSION,
        ),
        RuntimeKernelBoundary(
            service="project-memory",
            interface_id=EVIDENCE_RECORDING_INTERFACE_ID,
            contract_version=PROJECT_MEMORY_CONTRACT_VERSION,
            schema_version=PROJECT_MEMORY_SCHEMA_VERSION,
        ),
        RuntimeKernelBoundary(
            service="deterministic-validator",
            interface_id=EVIDENCE_RECORDING_INTERFACE_ID,
            contract_version="c03.1",
            schema_version="deterministic-validation-report-v0.1",
        ),
        RuntimeKernelBoundary(
            service="scheduler-readiness",
            interface_id=WORKER_PACKAGE_INTERFACE_ID,
            contract_version="runtime-readiness-v0.1",
            schema_version="readiness-decision-v0.1",
        ),
        RuntimeKernelBoundary(
            service="artifact-evidence-graph",
            interface_id=EVIDENCE_RECORDING_INTERFACE_ID,
            contract_version=RUNTIME_KERNEL_CONTRACT_VERSION,
            schema_version=RUNTIME_KERNEL_OUTPUT_SCHEMA_VERSION,
        ),
    )


def _empty_context(runtime_status: RuntimePlanStatus) -> RuntimeTaskContext:
    return RuntimeTaskContext(
        requirements=(),
        capabilities=(),
        human_gate_ids=(),
        human_gate_state="none",
        plan_id=runtime_status.plan_id,
        plan_version=runtime_status.plan_version,
    )


def _active_stop_conditions(
    *,
    runtime_status: RuntimePlanStatus,
    task_context: RuntimeTaskContext,
    readiness: ReadinessDecision,
    validation_ok: bool,
    imported: bool,
    tasks_attempted_this_run: int,
    max_tasks_per_run: int,
    failures_this_run: int,
    max_failures_per_run: int,
    elapsed_wall_clock_seconds: float,
    max_wall_clock_seconds: float,
    crash_recovery_required: bool,
) -> tuple[str, ...]:
    active: list[str] = []
    if not runtime_status.ready_tasks:
        active.append("no_ready_task")
    if task_context.human_gate_state == "pending":
        active.append("pending_human_gate")
    if readiness.status == "INVALID_GRAPH_STATE" or not validation_ok:
        active.append("reconciliation_required")
    if failures_this_run >= max_failures_per_run:
        active.append("failure_limit")
    if elapsed_wall_clock_seconds >= max_wall_clock_seconds:
        active.append("time_limit")
    if tasks_attempted_this_run >= max_tasks_per_run:
        active.append("task_limit")
    if crash_recovery_required:
        active.append("crash_recovery_before_resume")
    if not imported or readiness.status == "NOT_FOUND":
        active.append("runtime_error")
    return tuple(condition for condition in C20_RUNTIME_STOP_CONDITIONS if condition in set(active))


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(sorted(str(item) for item in value))
    return ()
