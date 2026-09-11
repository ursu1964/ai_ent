from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ai_ent.bootstrap.models import BootstrapTask, VerificationSpec
from ai_ent.bootstrap.paths import VENV_PYTHON
from ai_ent.external_project import (
    MANDATORY_VERIFICATION_COMMANDS,
    ExternalProjectFrozenPlan,
    ExternalProjectRuntimeTask,
    validate_external_project_frozen_plan,
)
from ai_ent.persistence.models import (
    BootstrapRun,
    Execution,
    Project,
    RuntimeHumanGate,
    RuntimeHumanGateStatus,
    RuntimePlanImport,
    RuntimeTaskPlanBinding,
    Task,
    TaskDependency,
    TaskLease,
    utc_now,
)
from ai_ent.persistence.repositories.bootstrap import BootstrapRunRepository
from ai_ent.project_manifest import (
    EnvironmentProfile,
    FeasibilityEvaluation,
    FeasibilityFacet,
    FeasibilityPolicyProfile,
    GeneratedImplementationTask,
    ImplementationDryRun,
    ImplementationPlan,
    PlanFeasibilityStatus,
    PolicyDecision,
    TaskFeasibility,
    TaskFeasibilityStatus,
    canonical_bytes,
    dry_run_evaluated_plan,
    dry_run_implementation_plan,
    evaluate_feasibility,
    generate_implementation_plan,
)
from ai_ent.scheduler.guarded import GuardedAutonomousRunner, GuardedRunConfig
from ai_ent.scheduler.readiness import TaskReadinessService

RUNTIME_HANDOFF_IMPORTER_VERSION = "rhi-001.1"
EXTERNAL_RUNTIME_HANDOFF_ADAPTER_VERSION = "erhi-001.1"
EXTERNAL_PROJECT_RUNTIME_IMPORTER_VERSION = "epri-001.1"
EXTERNAL_PROJECT_RUNTIME_RECEIPT_SCHEMA_VERSION = (
    "external-project-runtime-import-receipt-v0.1"
)
RUNTIME_IMPORT_PREFIX = "IMPL-"
DEFAULT_RUNTIME_PROJECT_ID = "PRJ-AI-ENT"
STANDARD_RUNTIME_VERIFICATION_COMMANDS = (
    f"{VENV_PYTHON} -m pytest -q",
    f"{VENV_PYTHON} -m ruff check .",
    f"{VENV_PYTHON} -m pyright",
)

ImportStatus = Literal["IMPORTED", "ALREADY_IMPORTED", "CONFLICT", "BLOCKED"]


@dataclass(frozen=True)
class RuntimeHandoffArtifacts:
    plan: ImplementationPlan
    feasibility: FeasibilityEvaluation
    dry_run: ImplementationDryRun
    accepted_bound_hashes: dict[str, str] | None = None


@dataclass(frozen=True)
class RuntimePlanImportResult:
    status: ImportStatus
    import_id: str
    project_id: str
    plan_id: str
    plan_version: str
    tasks_imported: int
    dependency_edges_imported: int
    human_gates_bound: int
    effective_concurrency: int
    fingerprints_validated: bool
    runtime_ready_tasks: tuple[str, ...]
    gated_not_ready_tasks: tuple[str, ...]
    executions_after_import: int
    active_leases_after_import: int
    blockers: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in {"IMPORTED", "ALREADY_IMPORTED"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "import_id": self.import_id,
            "project_id": self.project_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "tasks_imported": self.tasks_imported,
            "dependency_edges_imported": self.dependency_edges_imported,
            "human_gates_bound": self.human_gates_bound,
            "effective_concurrency": self.effective_concurrency,
            "fingerprints_validated": self.fingerprints_validated,
            "runtime_ready_tasks": list(self.runtime_ready_tasks),
            "gated_not_ready_tasks": list(self.gated_not_ready_tasks),
            "executions_after_import": self.executions_after_import,
            "active_leases_after_import": self.active_leases_after_import,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class ExternalProjectRuntimeBindingReceipt:
    frozen_plan: ExternalProjectFrozenPlan
    result: RuntimePlanImportResult
    importer_version: str
    schema_version: str = EXTERNAL_PROJECT_RUNTIME_RECEIPT_SCHEMA_VERSION

    @property
    def ok(self) -> bool:
        return self.result.ok

    @property
    def receipt_hash(self) -> str:
        return hashlib.sha256(canonical_bytes(self._payload())).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "receipt_hash": self.receipt_hash}

    def _payload(self) -> dict[str, Any]:
        project = self.frozen_plan.project
        validation = validate_external_project_frozen_plan(self.frozen_plan)
        return {
            "record_kind": "external_project_runtime_binding_receipt",
            "schema_version": self.schema_version,
            "importer_version": self.importer_version,
            "ok": self.result.ok,
            "status": self.result.status,
            "import_id": self.result.import_id,
            "project_id": self.result.project_id,
            "plan_id": self.result.plan_id,
            "plan_version": self.result.plan_version,
            "tasks_imported": self.result.tasks_imported,
            "dependency_edges_imported": self.result.dependency_edges_imported,
            "human_gates_bound": self.result.human_gates_bound,
            "effective_concurrency": self.result.effective_concurrency,
            "fingerprints_validated": self.result.fingerprints_validated,
            "runtime_ready_tasks": list(self.result.runtime_ready_tasks),
            "gated_not_ready_tasks": list(self.result.gated_not_ready_tasks),
            "executions_after_import": self.result.executions_after_import,
            "active_leases_after_import": self.result.active_leases_after_import,
            "blockers": list(self.result.blockers),
            "frozen_plan_hash": self.frozen_plan.frozen_plan_hash,
            "project_model_hash": project.model_hash,
            "runtime_binding": project.runtime_binding.as_dict(),
            "workspace_binding": project.workspace.as_dict(),
            "repository_binding": project.repository.as_dict(),
            "human_gate_bindings": _external_project_human_gate_bindings(self.frozen_plan),
            "control_plane_authority": project.runtime_binding.control_plane_authority,
            "grants_control_plane_authority": (
                project.runtime_binding.grants_control_plane_authority
            ),
            "verifier_bypass_authority": project.runtime_binding.verifier_bypass_authority,
            "implicit_human_gate_approval": project.runtime_binding.implicit_human_gate_approval,
            "independent_verification_required": project.plan.independent_verification_required,
            "mandatory_verification_commands": list(MANDATORY_VERIFICATION_COMMANDS),
            "validation": validation.as_dict(),
        }


@dataclass(frozen=True)
class RuntimePlanStatus:
    project_id: str
    plan_id: str | None
    plan_version: str | None
    import_status: str | None
    imported_tasks: int
    dependency_edges: int
    human_gates: int
    pending_human_gates: int
    effective_concurrency: int | None
    ready_tasks: tuple[str, ...]
    gated_not_ready_tasks: tuple[str, ...]
    executions: int
    active_leases: int
    authority_active: bool
    guarded_dry_run: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "import_status": self.import_status,
            "imported_tasks": self.imported_tasks,
            "dependency_edges": self.dependency_edges,
            "human_gates": self.human_gates,
            "pending_human_gates": self.pending_human_gates,
            "effective_concurrency": self.effective_concurrency,
            "ready_tasks": list(self.ready_tasks),
            "gated_not_ready_tasks": list(self.gated_not_ready_tasks),
            "executions": self.executions,
            "active_leases": self.active_leases,
            "authority_active": self.authority_active,
            "guarded_dry_run": self.guarded_dry_run,
        }


class RuntimePlanImportError(RuntimeError):
    """Raised when a frozen plan cannot be imported safely."""


def build_runtime_manifest_tasks(session: Session, project_id: str) -> dict[str, BootstrapTask]:
    tasks = {
        task.id: task
        for task in session.scalars(
            select(Task)
            .join(RuntimeTaskPlanBinding, RuntimeTaskPlanBinding.task_id == Task.id)
            .where(Task.project_id == project_id)
            .order_by(Task.id)
        ).all()
    }
    if not tasks:
        return {}
    bindings = {
        binding.task_id: binding
        for binding in session.scalars(
            select(RuntimeTaskPlanBinding)
            .where(RuntimeTaskPlanBinding.task_id.in_(tuple(tasks)))
            .order_by(RuntimeTaskPlanBinding.task_id)
        ).all()
    }
    dependencies: dict[str, tuple[str, ...]] = {
        task_id: tuple(
            session.scalars(
                select(TaskDependency.depends_on_task_id)
                .where(TaskDependency.task_id == task_id)
                .order_by(TaskDependency.depends_on_task_id)
            ).all()
        )
        for task_id in tasks
    }
    manifest_tasks: dict[str, BootstrapTask] = {}
    for task_id in sorted(tasks):
        task = tasks[task_id]
        binding = bindings.get(task_id)
        if binding is None or task.fingerprint != binding.fingerprint:
            continue
        write_scope = json.loads(binding.write_scope_json)
        acceptance = json.loads(binding.acceptance_json)
        objective_parts = [task.objective or task.title]
        if isinstance(acceptance, dict):
            criteria = acceptance.get("acceptance_criteria", [])
            required_decisions = acceptance.get("required_decisions", [])
            if criteria:
                objective_parts.append("Acceptance criteria:")
                objective_parts.extend(f"- {item}" for item in criteria)
            if required_decisions:
                objective_parts.append("Decision dependencies:")
                objective_parts.extend(f"- {item}" for item in required_decisions)
        elif acceptance:
            objective_parts.append("Acceptance criteria:")
            objective_parts.extend(f"- {item}" for item in acceptance)
        manifest_tasks[task_id] = BootstrapTask(
            id=task.id,
            stage="runtime-plan",
            title=task.title,
            executor="codex",
            depends_on=dependencies[task_id],
            objective="\n".join(objective_parts),
            allowed_paths=tuple(str(item) for item in write_scope.get("allowed", [])),
            prohibited_paths=tuple(str(item) for item in write_scope.get("prohibited", [])),
            outputs=(),
            execution_class=task.execution_class,
            schedulable=task.schedulable,
            verification=VerificationSpec(commands=_runtime_bound_verification_commands(binding)),
        )
    return manifest_tasks


class RuntimePlanImporter:
    def __init__(
        self,
        *,
        importer_version: str = RUNTIME_HANDOFF_IMPORTER_VERSION,
        readiness: TaskReadinessService | None = None,
    ) -> None:
        self.importer_version = importer_version
        self.readiness = readiness or TaskReadinessService()

    def import_frozen_plan(
        self,
        session: Session,
        artifacts: RuntimeHandoffArtifacts,
        *,
        runtime_project_id: str | None = None,
        require_clean_git: bool = True,
        require_codex_command: bool = True,
        repository_root: Path = Path("."),
    ) -> RuntimePlanImportResult:
        blockers = self._preflight_blockers(
            artifacts,
            require_clean_git=require_clean_git,
            require_codex_command=require_codex_command,
            repository_root=repository_root,
        )
        project_id = runtime_project_id or artifacts.dry_run.lock.project_id
        import_id = _import_id(artifacts.dry_run.lock.plan_id, artifacts.dry_run.lock.plan_version)
        if blockers:
            return self._blocked_result(artifacts, import_id, project_id, blockers)

        existing_import = session.get(RuntimePlanImport, import_id)
        if existing_import is not None:
            blockers = self._existing_import_conflicts(session, artifacts, existing_import)
            if blockers:
                return self._conflict_result(artifacts, import_id, project_id, blockers)
            return self._result("ALREADY_IMPORTED", session, artifacts, import_id, project_id)

        existing_runtime_tasks = list(_imported_task_ids(session, project_id))
        if existing_runtime_tasks:
            blocker = f"existing runtime implementation tasks: {', '.join(sorted(existing_runtime_tasks))}"
            return self._conflict_result(
                artifacts,
                import_id,
                project_id,
                (blocker,),
            )

        try:
            self._import_new(session, artifacts, import_id, project_id, repository_root)
            session.flush()
        except (IntegrityError, SQLAlchemyError) as exc:
            raise RuntimePlanImportError("runtime frozen plan import failed") from exc

        return self._result("IMPORTED", session, artifacts, import_id, project_id)

    def import_external_frozen_plan(
        self,
        session: Session,
        frozen_plan: ExternalProjectFrozenPlan,
        *,
        runtime_project_id: str | None = None,
        require_clean_git: bool = True,
        require_codex_command: bool = True,
        repository_root: Path = Path("."),
    ) -> RuntimePlanImportResult:
        project_id = runtime_project_id or frozen_plan.project.project_id
        import_id = _import_id(
            frozen_plan.project.plan.plan_id,
            frozen_plan.project.plan.version,
        )
        validation = validate_external_project_frozen_plan(frozen_plan)
        if not validation.ok:
            return RuntimePlanImportResult(
                status="BLOCKED",
                import_id=import_id,
                project_id=project_id,
                plan_id=frozen_plan.project.plan.plan_id,
                plan_version=frozen_plan.project.plan.version,
                tasks_imported=0,
                dependency_edges_imported=0,
                human_gates_bound=0,
                effective_concurrency=frozen_plan.effective_concurrency,
                fingerprints_validated=False,
                runtime_ready_tasks=(),
                gated_not_ready_tasks=(),
                executions_after_import=0,
                active_leases_after_import=0,
                blockers=tuple(finding.message for finding in validation.findings),
            )
        artifacts = external_project_runtime_handoff_artifacts(frozen_plan)
        return self.import_frozen_plan(
            session,
            artifacts,
            runtime_project_id=project_id,
            require_clean_git=require_clean_git,
            require_codex_command=require_codex_command,
            repository_root=repository_root,
        )

    def status(
        self,
        session: Session,
        *,
        project_id: str = DEFAULT_RUNTIME_PROJECT_ID,
        include_guarded_dry_run: bool = False,
    ) -> RuntimePlanStatus:
        receipt = self._latest_import(session, project_id)
        task_ids = [task_id for task_id in _imported_task_ids(session, project_id)]
        ready_tasks = tuple(task.id for task in self.readiness.list_ready_tasks(session, project_id=project_id))
        gated = _pending_gate_task_ids(session, project_id)
        guarded: dict[str, Any] | None = None
        if include_guarded_dry_run:
            guarded_result = GuardedAutonomousRunner(persist_summary=False).dry_run(
                session,
                GuardedRunConfig(project_id=project_id, dry_run=True),
            )
            guarded = {
                "dry_run": guarded_result.dry_run,
                "stop_reason": guarded_result.stop_reason,
                "likely_next_task_id": guarded_result.likely_next_task_id,
                "remaining_ready_tasks": list(guarded_result.remaining_ready_tasks),
                "blockers": list(guarded_result.blockers),
            }
        return RuntimePlanStatus(
            project_id=project_id,
            plan_id=receipt.plan_id if receipt else None,
            plan_version=receipt.plan_version if receipt else None,
            import_status=receipt.status if receipt else None,
            imported_tasks=len(task_ids),
            dependency_edges=_dependency_count(session, task_ids),
            human_gates=_human_gate_count(session, project_id),
            pending_human_gates=_pending_gate_count(session, project_id),
            effective_concurrency=receipt.effective_concurrency if receipt else None,
            ready_tasks=ready_tasks,
            gated_not_ready_tasks=gated,
            executions=_execution_count(session, task_ids),
            active_leases=_active_lease_count(session, task_ids),
            authority_active=BootstrapRunRepository().get_active_authority(session, project_id) is not None,
            guarded_dry_run=guarded,
        )

    def _import_new(
        self,
        session: Session,
        artifacts: RuntimeHandoffArtifacts,
        import_id: str,
        project_id: str,
        repository_root: Path,
    ) -> None:
        plan = artifacts.plan
        lock = artifacts.dry_run.lock
        project = session.get(Project, project_id)
        if project is None:
            project = Project(id=project_id, name=f"AI-Enterprise Runtime {lock.plan_id}")
            session.add(project)
            session.flush()

        baseline_commit = _git_head(repository_root)
        run_id = f"run-{import_id}"
        if session.get(BootstrapRun, run_id) is None:
            session.add(
                BootstrapRun(
                    id=run_id,
                    project_id=project_id,
                    status="running",
                    manifest_ref=f"{lock.plan_id}/v{lock.plan_version}",
                    baseline_commit=baseline_commit,
                    current_stage="runtime_plan_imported",
                    started_at=utc_now(),
                )
            )
            session.flush()
        BootstrapRunRepository().activate_postgresql_authority(session, project_id=project_id, run_id=run_id)

        session.add(
            RuntimePlanImport(
                id=import_id,
                project_id=project_id,
                plan_project_id=lock.project_id,
                plan_id=lock.plan_id,
                plan_version=lock.plan_version,
                status="imported",
                imported_at=utc_now(),
                compiled_project_hash=lock.compiled_project_hash,
                capability_resolution_hash=lock.capability_resolution_hash,
                trace_validation_hash=lock.trace_validation_hash,
                implementation_plan_hash=lock.implementation_plan_hash,
                feasibility_hash=lock.feasibility_hash,
                dry_run_hash=lock.dry_run_hash,
                task_fingerprint_hash=_task_fingerprint_hash(lock.task_fingerprints),
                dependency_graph_hash=lock.dependency_graph_hash,
                importer_version=self.importer_version,
                task_count=len(plan.tasks),
                dependency_count=len(plan.dependency_edges),
                human_gate_count=len(lock.human_gate_definitions),
                effective_concurrency=lock.effective_concurrency,
            )
        )
        session.flush()

        feasibility_by_task = {result.task_id: result for result in artifacts.feasibility.task_results}
        for task in sorted(plan.tasks, key=lambda item: item.id):
            session.add(
                Task(
                    id=task.id,
                    project_id=project_id,
                    title=task.title,
                    objective=task.objective,
                    status="pending",
                    execution_class=task.execution["executor"] if task.execution["executor"] == "simulation" else "implementation",
                    schedulable=True,
                    fingerprint=task.fingerprint,
                )
            )
            feasibility = feasibility_by_task[task.id]
            session.add(
                RuntimeTaskPlanBinding(
                    task_id=task.id,
                    import_id=import_id,
                    plan_id=lock.plan_id,
                    plan_version=lock.plan_version,
                    fingerprint=task.fingerprint,
                    risk_level=task.risk["level"],
                    agent_role=task.execution["agent_role"],
                    model_profile=task.execution["model_profile"],
                    executor=task.execution["executor"],
                    verification_profile=task.verification["profile"],
                    feasibility_status=feasibility.status,
                    policy_decision=feasibility.policy_decision,
                    implements_json=_canonical_text(task.implements),
                    write_scope_json=_canonical_text(task.write_scope),
                    acceptance_json=_runtime_acceptance_text(task),
                )
            )

        for task_id, depends_on_task_id in sorted(plan.dependency_edges):
            session.add(TaskDependency(task_id=task_id, depends_on_task_id=depends_on_task_id))

        downstream_by_task = _downstream_by_task(plan)
        risk_by_task = {task.id: task.risk["level"] for task in plan.tasks}
        for gate in sorted(lock.human_gate_definitions, key=lambda item: str(item["id"])):
            task_id = str(gate["task_id"])
            session.add(
                RuntimeHumanGate(
                    id=str(gate["id"]),
                    import_id=import_id,
                    task_id=task_id,
                    plan_id=lock.plan_id,
                    plan_version=lock.plan_version,
                    status=_runtime_gate_status(gate),
                    reason=str(gate["reason"]),
                    risk_level=risk_by_task[task_id],
                    approval_boundary=str(gate.get("required_before", "execution")),
                    expected_evidence_json=_canonical_text(["explicit human approval recorded before execution"]),
                    downstream_task_ids_json=_canonical_text(downstream_by_task[task_id]),
                    resume_semantics="resume guarded run from the same PostgreSQL project after approval",
                )
            )

    def _preflight_blockers(
        self,
        artifacts: RuntimeHandoffArtifacts,
        *,
        require_clean_git: bool,
        require_codex_command: bool,
        repository_root: Path,
    ) -> tuple[str, ...]:
        blockers: list[str] = []
        plan = artifacts.plan
        dry_run = artifacts.dry_run
        feasibility = artifacts.feasibility
        lock = dry_run.lock
        if lock.state != "FROZEN" or dry_run.frozen_plan_state != "FROZEN":
            blockers.append("frozen plan state is not FROZEN")
        if dry_run.plan_acceptance_state not in {"READY_FOR_PLAN_ACCEPTANCE", "READY_WITH_EXECUTION_PREREQUISITES"}:
            blockers.append(f"plan acceptance state is {dry_run.plan_acceptance_state}")
        if plan.implementation_plan_hash != lock.implementation_plan_hash:
            blockers.append("implementation plan hash mismatch")
        if feasibility.feasibility_hash != lock.feasibility_hash:
            blockers.append("feasibility hash mismatch")
        if dry_run.dry_run_hash != lock.dry_run_hash:
            blockers.append("dry-run hash mismatch")
        if artifacts.accepted_bound_hashes is not None:
            blockers.extend(_pag_hash_mismatches(artifacts.accepted_bound_hashes, dry_run))
        if len(plan.tasks) != len(lock.task_fingerprints):
            blockers.append("task count does not match frozen lock")
        if len(plan.dependency_edges) != dry_run.dependency_edge_count:
            blockers.append("dependency edge count does not match dry-run")
        if plan.findings:
            blockers.extend(f"plan finding:{finding.finding_id}:{finding.severity}" for finding in plan.findings if finding.severity == "ERROR")
        if any(finding.severity == "ERROR" for finding in dry_run.findings):
            blockers.append("dry-run has ERROR findings")
        for task in plan.tasks:
            if lock.task_fingerprints.get(task.id) != task.fingerprint:
                blockers.append(f"task fingerprint mismatch:{task.id}")
        if require_codex_command:
            command = os.environ.get("AIENT_CODEX_COMMAND", "").strip()
            if not command:
                blockers.append("AIENT_CODEX_COMMAND is not configured")
            elif shutil.which(shlex.split(command)[0]) is None:
                blockers.append("AIENT_CODEX_COMMAND executable cannot be resolved")
        if require_clean_git and not _git_clean(repository_root):
            blockers.append("Git repository is not clean")
        return tuple(sorted(blockers))

    def _existing_import_conflicts(
        self,
        session: Session,
        artifacts: RuntimeHandoffArtifacts,
        existing: RuntimePlanImport,
    ) -> tuple[str, ...]:
        lock = artifacts.dry_run.lock
        conflicts: list[str] = []
        expected = {
            "plan_project_id": lock.project_id,
            "plan_id": lock.plan_id,
            "plan_version": lock.plan_version,
            "compiled_project_hash": lock.compiled_project_hash,
            "capability_resolution_hash": lock.capability_resolution_hash,
            "trace_validation_hash": lock.trace_validation_hash,
            "implementation_plan_hash": lock.implementation_plan_hash,
            "feasibility_hash": lock.feasibility_hash,
            "dry_run_hash": lock.dry_run_hash,
            "dependency_graph_hash": lock.dependency_graph_hash,
            "task_count": len(artifacts.plan.tasks),
            "dependency_count": len(artifacts.plan.dependency_edges),
            "human_gate_count": len(lock.human_gate_definitions),
            "effective_concurrency": lock.effective_concurrency,
        }
        for field, expected_value in expected.items():
            if getattr(existing, field) != expected_value:
                conflicts.append(f"receipt field mismatch:{field}")
        for task in artifacts.plan.tasks:
            persisted = session.get(Task, task.id)
            binding = session.get(RuntimeTaskPlanBinding, task.id)
            if persisted is None or binding is None:
                conflicts.append(f"missing imported task or binding:{task.id}")
                continue
            if persisted.project_id != existing.project_id or persisted.fingerprint != task.fingerprint:
                conflicts.append(f"task mismatch:{task.id}")
            if binding.fingerprint != task.fingerprint:
                conflicts.append(f"binding fingerprint mismatch:{task.id}")
        dependency_edges = {
            (task_id, depends_on)
            for task_id, depends_on in session.execute(
                select(TaskDependency.task_id, TaskDependency.depends_on_task_id).where(
                    TaskDependency.task_id.in_([task.id for task in artifacts.plan.tasks])
                )
            )
        }
        if dependency_edges != set(artifacts.plan.dependency_edges):
            conflicts.append("dependency graph mismatch")
        gate_ids = set(session.scalars(select(RuntimeHumanGate.id).where(RuntimeHumanGate.import_id == existing.id)).all())
        if gate_ids != {str(gate["id"]) for gate in lock.human_gate_definitions}:
            conflicts.append("human gate mismatch")
        return tuple(sorted(conflicts))

    def _result(
        self,
        status: ImportStatus,
        session: Session,
        artifacts: RuntimeHandoffArtifacts,
        import_id: str,
        project_id: str,
    ) -> RuntimePlanImportResult:
        task_ids = tuple(_imported_task_ids(session, project_id))
        return RuntimePlanImportResult(
            status=status,
            import_id=import_id,
            project_id=project_id,
            plan_id=artifacts.dry_run.lock.plan_id,
            plan_version=artifacts.dry_run.lock.plan_version,
            tasks_imported=len(task_ids),
            dependency_edges_imported=_dependency_count(session, task_ids),
            human_gates_bound=_human_gate_count(session, project_id),
            effective_concurrency=artifacts.dry_run.lock.effective_concurrency,
            fingerprints_validated=_fingerprints_match(session, artifacts.plan),
            runtime_ready_tasks=tuple(task.id for task in self.readiness.list_ready_tasks(session, project_id=project_id)),
            gated_not_ready_tasks=_pending_gate_task_ids(session, project_id),
            executions_after_import=_execution_count(session, task_ids),
            active_leases_after_import=_active_lease_count(session, task_ids),
        )

    def _blocked_result(
        self,
        artifacts: RuntimeHandoffArtifacts,
        import_id: str,
        project_id: str,
        blockers: tuple[str, ...],
    ) -> RuntimePlanImportResult:
        return RuntimePlanImportResult(
            status="BLOCKED",
            import_id=import_id,
            project_id=project_id,
            plan_id=artifacts.dry_run.lock.plan_id,
            plan_version=artifacts.dry_run.lock.plan_version,
            tasks_imported=0,
            dependency_edges_imported=0,
            human_gates_bound=0,
            effective_concurrency=artifacts.dry_run.lock.effective_concurrency,
            fingerprints_validated=False,
            runtime_ready_tasks=(),
            gated_not_ready_tasks=(),
            executions_after_import=0,
            active_leases_after_import=0,
            blockers=blockers,
        )

    def _conflict_result(
        self,
        artifacts: RuntimeHandoffArtifacts,
        import_id: str,
        project_id: str,
        blockers: tuple[str, ...],
    ) -> RuntimePlanImportResult:
        return RuntimePlanImportResult(
            status="CONFLICT",
            import_id=import_id,
            project_id=project_id,
            plan_id=artifacts.dry_run.lock.plan_id,
            plan_version=artifacts.dry_run.lock.plan_version,
            tasks_imported=0,
            dependency_edges_imported=0,
            human_gates_bound=0,
            effective_concurrency=artifacts.dry_run.lock.effective_concurrency,
            fingerprints_validated=False,
            runtime_ready_tasks=(),
            gated_not_ready_tasks=(),
            executions_after_import=0,
            active_leases_after_import=0,
            blockers=blockers,
        )

    def _latest_import(self, session: Session, project_id: str) -> RuntimePlanImport | None:
        return session.scalars(
            select(RuntimePlanImport)
            .where(RuntimePlanImport.project_id == project_id)
            .order_by(RuntimePlanImport.imported_at.desc(), RuntimePlanImport.id.desc())
            .limit(1)
        ).first()


class ExternalProjectRuntimeImporter:
    def __init__(
        self,
        *,
        importer_version: str = EXTERNAL_PROJECT_RUNTIME_IMPORTER_VERSION,
        readiness: TaskReadinessService | None = None,
    ) -> None:
        self._runtime_importer = RuntimePlanImporter(
            importer_version=importer_version,
            readiness=readiness,
        )

    @property
    def importer_version(self) -> str:
        return self._runtime_importer.importer_version

    def import_frozen_plan(
        self,
        session: Session,
        frozen_plan: ExternalProjectFrozenPlan,
        *,
        runtime_project_id: str | None = None,
        require_clean_git: bool = True,
        require_codex_command: bool = True,
        repository_root: Path = Path("."),
    ) -> RuntimePlanImportResult:
        return self._runtime_importer.import_external_frozen_plan(
            session,
            frozen_plan,
            runtime_project_id=runtime_project_id,
            require_clean_git=require_clean_git,
            require_codex_command=require_codex_command,
            repository_root=repository_root,
        )

    def import_runtime_binding(
        self,
        session: Session,
        frozen_plan: ExternalProjectFrozenPlan,
        *,
        runtime_project_id: str | None = None,
        require_clean_git: bool = True,
        require_codex_command: bool = True,
        repository_root: Path = Path("."),
    ) -> ExternalProjectRuntimeBindingReceipt:
        result = self.import_frozen_plan(
            session,
            frozen_plan,
            runtime_project_id=runtime_project_id,
            require_clean_git=require_clean_git,
            require_codex_command=require_codex_command,
            repository_root=repository_root,
        )
        return external_project_runtime_import_receipt(
            frozen_plan,
            result,
            importer_version=self.importer_version,
        )

    def status(
        self,
        session: Session,
        *,
        project_id: str,
        include_guarded_dry_run: bool = False,
    ) -> RuntimePlanStatus:
        return self._runtime_importer.status(
            session,
            project_id=project_id,
            include_guarded_dry_run=include_guarded_dry_run,
        )


def load_runtime_handoff_artifacts(
    *,
    manifest_root: Path = Path("manifest/project/ai-ent"),
    output_dir: Path = Path(".build/compiled"),
    accepted_artifact_path: Path | None = Path("artifacts/pag-001/PAG-001.json"),
) -> RuntimeHandoffArtifacts:
    del output_dir
    plan = generate_implementation_plan(manifest_root)
    current_feasibility = evaluate_feasibility(manifest_root)
    current_dry_run = dry_run_implementation_plan(manifest_root)
    accepted_hashes = _pag_accepted_hashes(accepted_artifact_path) if accepted_artifact_path is not None else None
    if accepted_hashes and current_dry_run.dry_run_hash != accepted_hashes.get("dry_run_hash"):
        planning_environment = replace(
            current_feasibility.environment_profile,
            codex_command_configured=False,
            codex_executable_available=True,
        )
        accepted_feasibility = evaluate_feasibility(manifest_root, environment=planning_environment)
        accepted_dry_run = dry_run_implementation_plan(manifest_root, environment=planning_environment)
        return RuntimeHandoffArtifacts(
            plan=plan,
            feasibility=accepted_feasibility,
            dry_run=accepted_dry_run,
            accepted_bound_hashes=accepted_hashes,
        )
    return RuntimeHandoffArtifacts(
        plan=plan,
        feasibility=current_feasibility,
        dry_run=current_dry_run,
        accepted_bound_hashes=accepted_hashes,
    )


def external_project_runtime_handoff_artifacts(
    frozen_plan: ExternalProjectFrozenPlan,
) -> RuntimeHandoffArtifacts:
    validation = validate_external_project_frozen_plan(frozen_plan)
    if not validation.ok:
        messages = "; ".join(finding.message for finding in validation.findings)
        raise RuntimePlanImportError(f"external frozen plan is not importable: {messages}")
    plan = _external_project_implementation_plan(frozen_plan)
    feasibility = _external_project_feasibility(plan, frozen_plan)
    dry_run = dry_run_evaluated_plan(plan, feasibility)
    lock = replace(
        dry_run.lock,
        plan_id=frozen_plan.project.plan.plan_id,
        plan_version=frozen_plan.project.plan.version,
        project_id=frozen_plan.project.project_id,
        effective_concurrency=frozen_plan.effective_concurrency,
        state="FROZEN",
    )
    return RuntimeHandoffArtifacts(
        plan=plan,
        feasibility=feasibility,
        dry_run=replace(
            dry_run,
            frozen_plan_state="FROZEN",
            effective_parallel_width=frozen_plan.effective_concurrency,
            lock=lock,
        ),
    )


def external_project_runtime_import_receipt(
    frozen_plan: ExternalProjectFrozenPlan,
    result: RuntimePlanImportResult,
    *,
    importer_version: str = EXTERNAL_PROJECT_RUNTIME_IMPORTER_VERSION,
) -> ExternalProjectRuntimeBindingReceipt:
    return ExternalProjectRuntimeBindingReceipt(
        frozen_plan=frozen_plan,
        result=result,
        importer_version=importer_version,
    )


def _import_id(plan_id: str, plan_version: str) -> str:
    return f"rhi-{plan_id.lower()}-v{plan_version}"


def _runtime_verification_commands(profile: str) -> tuple[str, ...]:
    if profile == "STANDARD_REGRESSION":
        return STANDARD_RUNTIME_VERIFICATION_COMMANDS
    return STANDARD_RUNTIME_VERIFICATION_COMMANDS


def _runtime_bound_verification_commands(
    binding: RuntimeTaskPlanBinding,
) -> tuple[str, ...]:
    try:
        acceptance_payload = json.loads(binding.acceptance_json)
    except json.JSONDecodeError:
        return _runtime_verification_commands(binding.verification_profile)
    if not isinstance(acceptance_payload, dict):
        return _runtime_verification_commands(binding.verification_profile)
    commands = acceptance_payload.get("verification_commands")
    if not isinstance(commands, list) or not all(
        isinstance(command, str) for command in commands
    ):
        return _runtime_verification_commands(binding.verification_profile)
    return tuple(commands) or _runtime_verification_commands(binding.verification_profile)


def _runtime_acceptance_payload(task: GeneratedImplementationTask) -> dict[str, Any]:
    verification = task.verification
    commands = verification.get("commands")
    payload: dict[str, Any] = {
        "acceptance_criteria": list(verification.get("acceptance_criteria", ())),
        "verification_commands": (
            list(commands)
            if isinstance(commands, list | tuple)
            else list(_runtime_verification_commands(str(verification.get("profile", ""))))
        ),
    }
    runtime_binding = verification.get("runtime_binding")
    if isinstance(runtime_binding, dict):
        payload["runtime_binding"] = runtime_binding
    return payload


def _runtime_acceptance_text(task: GeneratedImplementationTask) -> str:
    return json.dumps(
        _runtime_acceptance_payload(task),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_text(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def _task_fingerprint_hash(fingerprints: dict[str, str]) -> str:
    return hashlib.sha256(canonical_bytes(fingerprints)).hexdigest()


def _git_clean(repository_root: Path) -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repository_root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0 and completed.stdout.strip() == ""


def _git_head(repository_root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _downstream_by_task(plan: ImplementationPlan) -> dict[str, tuple[str, ...]]:
    dependents: dict[str, set[str]] = {task.id: set() for task in plan.tasks}
    for task_id, depends_on in plan.dependency_edges:
        dependents.setdefault(depends_on, set()).add(task_id)

    def collect(task_id: str, seen: set[str] | None = None) -> set[str]:
        seen = seen or set()
        for dependent in dependents.get(task_id, set()):
            if dependent not in seen:
                seen.add(dependent)
                seen.update(collect(dependent, seen))
        return seen

    return {task.id: tuple(sorted(collect(task.id))) for task in plan.tasks}


def _imported_task_ids(session: Session, project_id: str) -> tuple[str, ...]:
    return tuple(
        session.scalars(
            select(RuntimeTaskPlanBinding.task_id)
            .join(RuntimePlanImport, RuntimeTaskPlanBinding.import_id == RuntimePlanImport.id)
            .where(RuntimePlanImport.project_id == project_id)
            .order_by(RuntimeTaskPlanBinding.task_id)
        ).all()
    )


def _external_project_implementation_plan(
    frozen_plan: ExternalProjectFrozenPlan,
) -> ImplementationPlan:
    project = frozen_plan.project
    task_by_id = {task.id: task for task in frozen_plan.tasks}
    tasks = tuple(
        _external_project_generated_task(frozen_plan, task)
        for task in sorted(frozen_plan.tasks, key=lambda item: item.id)
    )
    waves = _topological_waves(task_by_id)
    parallelizable_sets = tuple(
        {
            "id": f"EXT-PARALLEL-{index:03d}",
            "task_ids": wave["task_ids"],
            "reason": "tasks have no unmet dependencies within the same runtime wave",
        }
        for index, wave in enumerate(waves, start=1)
    )
    human_gates = tuple(
        _external_project_gate_definition(frozen_plan, gate)
        for gate in sorted(
            frozen_plan.human_gate_definitions,
            key=lambda item: str(item.get("id", "")),
        )
    )
    plan = ImplementationPlan(
        source_compiled_hash=project.model_hash,
        capability_resolution_hash=_hash_external(
            {
                "project_id": project.project_id,
                "capabilities": sorted(
                    {
                        capability_id
                        for task in frozen_plan.tasks
                        for capability_id in task.capability_ids
                    }
                ),
            }
        ),
        trace_validation_hash=_hash_external(
            {
                "project_id": project.project_id,
                "dependency_edges": [list(edge) for edge in frozen_plan.dependency_edges],
                "task_ids": frozen_plan.task_ids,
            }
        ),
        generator_version=EXTERNAL_RUNTIME_HANDOFF_ADAPTER_VERSION,
        implementation_plan_hash=frozen_plan.frozen_plan_hash,
        epics=(
            {
                "id": f"EXT-EPIC-{project.project_id}",
                "title": project.name,
                "source": "external_project_frozen_plan",
            },
        ),
        features=(
            {
                "id": f"EXT-FEATURE-{project.project_id}",
                "title": project.name,
                "source": "external_project_frozen_plan",
            },
        ),
        tasks=tasks,
        satisfied_units=(),
        dependency_edges=frozen_plan.dependency_edges,
        waves=waves,
        parallelizable_sets=parallelizable_sets,
        critical_path=_critical_path(task_by_id),
        human_gates=human_gates,
        findings=(),
        warning_classifications=(),
    )
    return plan


def _external_project_generated_task(
    frozen_plan: ExternalProjectFrozenPlan,
    task: ExternalProjectRuntimeTask,
) -> GeneratedImplementationTask:
    return GeneratedImplementationTask(
        id=task.id,
        title=task.title,
        objective=task.objective,
        implements={
            "requirements": tuple(sorted(task.requirement_ids)),
            "capabilities": tuple(sorted(task.capability_ids)),
            "components": tuple(sorted(task.component_ids)),
            "interfaces": tuple(sorted(task.interface_ids)),
        },
        depends_on=tuple(sorted(task.depends_on)),
        write_scope={
            "allowed": tuple(sorted(task.allowed_paths)),
            "prohibited": tuple(sorted(task.prohibited_paths)),
        },
        execution={
            "agent_role": task.agent_role,
            "model_profile": task.model_profile,
            "executor": task.executor,
        },
        verification={
            "profile": task.verification_profile,
            "commands": list(task.verification_commands),
            "acceptance_criteria": list(task.acceptance_criteria),
            "runtime_binding": _external_project_task_runtime_binding(frozen_plan, task),
        },
        risk={"level": task.risk_level, "reason": task.risk_reason},
        provenance=dict(task.provenance),
        fingerprint=task.effective_fingerprint,
        epic_id="EXT-EPIC",
        feature_id="EXT-FEATURE",
        component_id=task.component_ids[0] if task.component_ids else None,
    )


def _external_project_task_runtime_binding(
    frozen_plan: ExternalProjectFrozenPlan,
    task: ExternalProjectRuntimeTask,
) -> dict[str, Any]:
    project = frozen_plan.project
    return {
        "binding_id": project.runtime_binding.binding_id,
        "project_id": project.project_id,
        "workspace_id": project.workspace.workspace_id,
        "repository_id": project.repository.repository_id,
        "plan_id": project.plan.plan_id,
        "plan_version": project.plan.version,
        "task_id": task.id,
        "workspace_root": project.workspace.root_path,
        "allowed_roots": list(project.workspace.allowed_roots),
        "prohibited_paths": list(project.workspace.prohibited_paths),
        "repository": project.repository.as_dict(),
        "control_plane_authority": project.runtime_binding.control_plane_authority,
        "grants_control_plane_authority": project.runtime_binding.grants_control_plane_authority,
        "verifier_bypass_authority": project.runtime_binding.verifier_bypass_authority,
        "implicit_human_gate_approval": project.runtime_binding.implicit_human_gate_approval,
        "independent_verification_required": project.plan.independent_verification_required,
        "pending_human_gates": list(project.runtime_binding.pending_human_gates),
        "approved_human_gates": list(project.runtime_binding.approved_human_gates),
        "mandatory_verification_commands": list(MANDATORY_VERIFICATION_COMMANDS),
    }


def _external_project_human_gate_bindings(
    frozen_plan: ExternalProjectFrozenPlan,
) -> list[dict[str, Any]]:
    approved = set(frozen_plan.project.runtime_binding.approved_human_gates)
    pending = set(frozen_plan.project.runtime_binding.pending_human_gates)
    bindings: list[dict[str, Any]] = []
    for gate in sorted(
        frozen_plan.human_gate_definitions,
        key=lambda item: str(item.get("id", "")),
    ):
        gate_id = str(gate.get("id", ""))
        if gate_id in approved:
            state = "approved"
        elif gate_id in pending:
            state = "pending"
        else:
            state = "unbound"
        bindings.append(
            {
                "gate_id": gate_id,
                "task_id": str(gate.get("task_id", "")),
                "state": state,
                "required_before": str(gate.get("required_before", "execution")),
            }
        )
    return bindings


def _external_project_feasibility(
    plan: ImplementationPlan,
    frozen_plan: ExternalProjectFrozenPlan,
) -> FeasibilityEvaluation:
    gate_ids_by_task: dict[str, tuple[str, ...]] = {}
    pending_gate_definitions = [
        gate
        for gate in plan.human_gates
        if _runtime_gate_status(gate) == "pending"
    ]
    gate_task_ids = {str(gate.get("task_id", "")) for gate in pending_gate_definitions}
    for task_id in sorted(gate_task_ids):
        gate_ids_by_task[task_id] = tuple(
            sorted(
                str(gate["id"])
                for gate in pending_gate_definitions
                if str(gate.get("task_id", "")) == task_id
            )
        )
    task_results = tuple(
        _external_project_task_feasibility(task, gate_ids_by_task.get(task.id, ()))
        for task in plan.tasks
    )
    status: PlanFeasibilityStatus = (
        "READY_WITH_HUMAN_GATES"
        if frozen_plan.human_gate_definitions
        else "READY_FOR_DRY_RUN"
    )
    environment = EnvironmentProfile(
        profile_id="external-project-runtime-import",
        repository_path=frozen_plan.project.workspace.root_path,
        git_available=True,
        postgresql_available=True,
        alembic_available=True,
        docker_available=True,
        docker_compose_available=True,
        codex_command_configured=True,
        codex_executable_available=True,
        python_path=str(VENV_PYTHON),
        python_available=True,
        ram_mb=None,
        gpu_available=False,
        external_network="not_required",
        configured_secret_names=(),
    )
    policy = FeasibilityPolicyProfile(
        profile_id="external-project-runtime-import-policy",
        agent_roles=tuple(sorted({task.execution["agent_role"] for task in plan.tasks})),
        model_profiles=tuple(sorted({task.execution["model_profile"] for task in plan.tasks})),
        executors=tuple(sorted({task.execution["executor"] for task in plan.tasks})),
        verification_profiles=tuple(
            sorted({str(task.verification["profile"]) for task in plan.tasks})
        ),
        auto_allowed_risks=("LOW",),
        guarded_allowed_risks=("LOW", "MEDIUM", "HIGH"),
        prohibited_path_patterns=tuple(frozen_plan.project.workspace.prohibited_paths),
        max_parallel_width=frozen_plan.effective_concurrency,
    )
    payload = {
        "implementation_plan_hash": plan.implementation_plan_hash,
        "source_compiled_hash": plan.source_compiled_hash,
        "capability_resolution_hash": plan.capability_resolution_hash,
        "trace_validation_hash": plan.trace_validation_hash,
        "evaluator_version": EXTERNAL_RUNTIME_HANDOFF_ADAPTER_VERSION,
        "plan_status": status,
        "environment_profile": environment.as_dict(),
        "policy_profile": policy.as_dict(),
        "task_results": [result.as_dict() for result in task_results],
        "human_gates": list(plan.human_gates),
        "feasible_parallel_width": frozen_plan.effective_concurrency,
        "technical_blockers": [],
        "findings": [],
    }
    return FeasibilityEvaluation(
        implementation_plan_hash=plan.implementation_plan_hash,
        source_compiled_hash=plan.source_compiled_hash,
        capability_resolution_hash=plan.capability_resolution_hash,
        trace_validation_hash=plan.trace_validation_hash,
        evaluator_version=EXTERNAL_RUNTIME_HANDOFF_ADAPTER_VERSION,
        feasibility_hash=_hash_external(payload),
        plan_status=status,
        environment_profile=environment,
        policy_profile=policy,
        task_results=task_results,
        human_gates=plan.human_gates,
        feasible_parallel_width=frozen_plan.effective_concurrency,
        technical_blockers=(),
        findings=(),
    )


def _external_project_gate_definition(
    frozen_plan: ExternalProjectFrozenPlan,
    gate: dict[str, Any],
) -> dict[str, Any]:
    gate_id = str(gate.get("id", ""))
    approved = set(frozen_plan.project.runtime_binding.approved_human_gates)
    pending = set(frozen_plan.project.runtime_binding.pending_human_gates)
    status = "approved" if gate_id in approved else "pending"
    if gate_id not in approved and gate_id not in pending:
        status = _runtime_gate_status(gate)
    return {**gate, "status": status}


def _runtime_gate_status(gate: dict[str, Any]) -> RuntimeHumanGateStatus:
    raw_status = str(gate.get("status", "pending")).lower()
    if raw_status == "approved":
        return "approved"
    if raw_status == "rejected":
        return "rejected"
    return "pending"


def _external_project_task_feasibility(
    task: GeneratedImplementationTask,
    gate_ids: tuple[str, ...],
) -> TaskFeasibility:
    has_gate = bool(gate_ids)
    status: TaskFeasibilityStatus = "HUMAN_APPROVAL_REQUIRED" if has_gate else "FEASIBLE"
    policy_decision: PolicyDecision = (
        "HUMAN_APPROVAL_REQUIRED" if has_gate else "GUARDED_ALLOWED"
    )
    return TaskFeasibility(
        task_id=task.id,
        status=status,
        reasons=(
            ("human approval gate required before execution",)
            if has_gate
            else ("task is feasible under current guarded policy",)
        ),
        blockers=(),
        conditions=(),
        required_human_gates=gate_ids,
        agent_feasibility=FeasibilityFacet(
            "AVAILABLE",
            ("external project agent role is importable",),
        ),
        model_feasibility=FeasibilityFacet(
            "AVAILABLE",
            ("external project model profile is importable",),
        ),
        tool_feasibility=FeasibilityFacet("AVAILABLE", ("executor codex is available",)),
        infrastructure_feasibility=FeasibilityFacet(
            "AVAILABLE",
            ("runtime infrastructure is provided by the control plane",),
        ),
        resource_feasibility=FeasibilityFacet(
            "AVAILABLE",
            ("declared task resources fit the runtime import profile",),
        ),
        verification_feasibility=FeasibilityFacet(
            "AVAILABLE",
            ("mandatory independent verification is configured",),
        ),
        policy_decision=policy_decision,
        dependency_feasibility=FeasibilityFacet(
            "AVAILABLE",
            ("dependency references are present in frozen plan",),
        ),
    )


def _topological_waves(
    task_by_id: dict[str, ExternalProjectRuntimeTask],
) -> tuple[dict[str, Any], ...]:
    remaining = set(task_by_id)
    completed: set[str] = set()
    waves: list[dict[str, Any]] = []
    while remaining:
        ready = tuple(
            sorted(
                task_id
                for task_id in remaining
                if set(task_by_id[task_id].depends_on) <= completed
            )
        )
        if not ready:
            ready = tuple(sorted(remaining))
        waves.append({"id": f"EXT-WAVE-{len(waves) + 1:03d}", "task_ids": list(ready)})
        completed.update(ready)
        remaining.difference_update(ready)
    return tuple(waves)


def _critical_path(
    task_by_id: dict[str, ExternalProjectRuntimeTask],
) -> tuple[str, ...]:
    memo: dict[str, tuple[str, ...]] = {}

    def path_to(task_id: str) -> tuple[str, ...]:
        if task_id in memo:
            return memo[task_id]
        dependencies = tuple(
            dependency_id
            for dependency_id in sorted(task_by_id[task_id].depends_on)
            if dependency_id in task_by_id
        )
        if not dependencies:
            memo[task_id] = (task_id,)
            return memo[task_id]
        best = max((path_to(dependency_id) for dependency_id in dependencies), key=len)
        memo[task_id] = (*best, task_id)
        return memo[task_id]

    if not task_by_id:
        return ()
    return max((path_to(task_id) for task_id in sorted(task_by_id)), key=len)


def _hash_external(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _dependency_count(session: Session, task_ids: tuple[str, ...] | list[str]) -> int:
    if not task_ids:
        return 0
    return int(
        session.scalar(
            select(func.count())
            .select_from(TaskDependency)
            .where(TaskDependency.task_id.in_(task_ids), TaskDependency.depends_on_task_id.in_(task_ids))
        )
        or 0
    )


def _human_gate_count(session: Session, project_id: str) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(RuntimeHumanGate)
            .join(Task, RuntimeHumanGate.task_id == Task.id)
            .where(Task.project_id == project_id)
        )
        or 0
    )


def _pending_gate_count(session: Session, project_id: str) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(RuntimeHumanGate)
            .join(Task, RuntimeHumanGate.task_id == Task.id)
            .where(Task.project_id == project_id, RuntimeHumanGate.status == "pending")
        )
        or 0
    )


def _pending_gate_task_ids(session: Session, project_id: str) -> tuple[str, ...]:
    return tuple(
        session.scalars(
            select(RuntimeHumanGate.task_id)
            .join(Task, RuntimeHumanGate.task_id == Task.id)
            .where(Task.project_id == project_id, RuntimeHumanGate.status == "pending")
            .order_by(RuntimeHumanGate.task_id)
        ).all()
    )


def _execution_count(session: Session, task_ids: tuple[str, ...] | list[str]) -> int:
    if not task_ids:
        return 0
    return int(session.scalar(select(func.count()).select_from(Execution).where(Execution.task_id.in_(task_ids))) or 0)


def _active_lease_count(session: Session, task_ids: tuple[str, ...] | list[str]) -> int:
    if not task_ids:
        return 0
    return int(
        session.scalar(
            select(func.count())
            .select_from(TaskLease)
            .where(TaskLease.task_id.in_(task_ids), TaskLease.status == "active")
        )
        or 0
    )


def _fingerprints_match(session: Session, plan: ImplementationPlan) -> bool:
    for task in plan.tasks:
        persisted = session.get(Task, task.id)
        binding = session.get(RuntimeTaskPlanBinding, task.id)
        if persisted is None or binding is None:
            return False
        if persisted.fingerprint != task.fingerprint or binding.fingerprint != task.fingerprint:
            return False
    return True


def _pag_accepted_hashes(path: Path) -> dict[str, str] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("gate_id") != "PAG-001":
        return None
    if payload.get("gate_result") not in {"ACCEPTED", "ACCEPTED_WITH_PREREQUISITES"}:
        return None
    hashes = payload.get("bound_hashes", {})
    if not isinstance(hashes, dict):
        return None
    return {str(key): str(value) for key, value in hashes.items()}


def _pag_hash_mismatches(hashes: dict[str, str], dry_run: ImplementationDryRun) -> list[str]:
    observed = {
        "compiled_project_hash": dry_run.compiled_project_hash,
        "capability_resolution_hash": dry_run.capability_resolution_hash,
        "trace_validation_hash": dry_run.trace_validation_hash,
        "implementation_plan_hash": dry_run.implementation_plan_hash,
        "feasibility_hash": dry_run.feasibility_hash,
        "dry_run_hash": dry_run.dry_run_hash,
        "dependency_graph_hash": dry_run.lock.dependency_graph_hash,
    }
    return [
        f"PAG-001 hash mismatch:{name}"
        for name, value in sorted(observed.items())
        if hashes.get(name) != value
    ]
