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
from ai_ent.persistence.models import (
    BootstrapRun,
    Execution,
    Project,
    RuntimeHumanGate,
    RuntimePlanImport,
    RuntimeTaskPlanBinding,
    Task,
    TaskDependency,
    TaskLease,
    utc_now,
)
from ai_ent.persistence.repositories.bootstrap import BootstrapRunRepository
from ai_ent.project_manifest import (
    FeasibilityEvaluation,
    ImplementationDryRun,
    ImplementationPlan,
    canonical_bytes,
    dry_run_implementation_plan,
    evaluate_feasibility,
    generate_implementation_plan,
)
from ai_ent.scheduler.guarded import GuardedAutonomousRunner, GuardedRunConfig
from ai_ent.scheduler.readiness import TaskReadinessService

RUNTIME_HANDOFF_IMPORTER_VERSION = "rhi-001.1"
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
            verification=VerificationSpec(commands=_runtime_verification_commands(binding.verification_profile)),
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

        existing_impl_tasks = list(
            session.scalars(
                select(Task.id).where(Task.project_id == project_id, Task.id.like(f"{RUNTIME_IMPORT_PREFIX}%"))
            ).all()
        )
        if existing_impl_tasks:
            return self._conflict_result(
                artifacts,
                import_id,
                project_id,
                (f"existing runtime implementation tasks: {', '.join(sorted(existing_impl_tasks))}",),
            )

        try:
            self._import_new(session, artifacts, import_id, project_id, repository_root)
            session.flush()
        except (IntegrityError, SQLAlchemyError) as exc:
            raise RuntimePlanImportError("runtime frozen plan import failed") from exc

        return self._result("IMPORTED", session, artifacts, import_id, project_id)

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
                    acceptance_json=_canonical_text(task.verification["acceptance_criteria"]),
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
                    status="pending",
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


def _import_id(plan_id: str, plan_version: str) -> str:
    return f"rhi-{plan_id.lower()}-v{plan_version}"


def _runtime_verification_commands(profile: str) -> tuple[str, ...]:
    if profile == "STANDARD_REGRESSION":
        return STANDARD_RUNTIME_VERIFICATION_COMMANDS
    return STANDARD_RUNTIME_VERIFICATION_COMMANDS


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
            select(Task.id)
            .where(Task.project_id == project_id, Task.id.like(f"{RUNTIME_IMPORT_PREFIX}%"))
            .order_by(Task.id)
        ).all()
    )


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
