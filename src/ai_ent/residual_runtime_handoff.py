from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

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
from ai_ent.project_manifest import EnvironmentProfile, canonical_bytes
from ai_ent.residual_dag import (
    ResidualFindingSeverity,
    ResidualGeneratedTask,
    ResidualImplementationPlan,
    ResidualPlanFinding,
    ResidualTaskType,
    generate_residual_implementation_plan,
)
from ai_ent.residual_dry_run import ResidualDryRun, dry_run_residual_plan
from ai_ent.residual_feasibility import ResidualFeasibilityEvaluation, evaluate_residual_feasibility
from ai_ent.residual_plan_acceptance import load_residual_plan_acceptance
from ai_ent.runtime_handoff import DEFAULT_RUNTIME_PROJECT_ID
from ai_ent.scheduler.guarded import GuardedAutonomousRunner, GuardedRunConfig
from ai_ent.scheduler.readiness import TaskReadinessService

RESIDUAL_RUNTIME_HANDOFF_IMPORTER_VERSION = "rhi-002.1"
RESIDUAL_IMPORT_PREFIX = "RES-"
RESIDUAL_PLAN_ID = "RESIDUAL-PLAN-a918c449cfe5"
RESIDUAL_PLAN_VERSION = "1"
ResidualImportStatus = Literal["IMPORTED", "ALREADY_IMPORTED", "CONFLICT", "BLOCKED"]


@dataclass(frozen=True)
class ResidualRuntimeHandoffArtifacts:
    plan: ResidualImplementationPlan
    feasibility: ResidualFeasibilityEvaluation
    dry_run: ResidualDryRun
    rpg_acceptance: dict[str, Any]
    accepted_bound_hashes: dict[str, str]


@dataclass(frozen=True)
class ResidualRuntimeImportResult:
    status: ResidualImportStatus
    import_id: str
    project_id: str
    residual_plan_id: str
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
    prior_plan_preserved: bool
    guarded_dry_run: dict[str, Any] | None = None
    blockers: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in {"IMPORTED", "ALREADY_IMPORTED"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "import_id": self.import_id,
            "project_id": self.project_id,
            "residual_plan_id": self.residual_plan_id,
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
            "prior_plan_preserved": self.prior_plan_preserved,
            "guarded_dry_run": self.guarded_dry_run,
            "blockers": list(self.blockers),
        }


class ResidualRuntimePlanImporter:
    def __init__(
        self,
        *,
        importer_version: str = RESIDUAL_RUNTIME_HANDOFF_IMPORTER_VERSION,
        readiness: TaskReadinessService | None = None,
    ) -> None:
        self.importer_version = importer_version
        self.readiness = readiness or TaskReadinessService()

    def import_residual_plan(
        self,
        session: Session,
        artifacts: ResidualRuntimeHandoffArtifacts,
        *,
        runtime_project_id: str = DEFAULT_RUNTIME_PROJECT_ID,
        require_clean_git: bool = True,
        require_codex_command: bool = True,
        include_guarded_dry_run: bool = False,
        repository_root: Path = Path("."),
    ) -> ResidualRuntimeImportResult:
        import_id = _import_id(artifacts.dry_run.lock.residual_plan_id, artifacts.dry_run.lock.plan_version)
        blockers = self._preflight_blockers(
            session,
            artifacts,
            project_id=runtime_project_id,
            require_clean_git=require_clean_git,
            require_codex_command=require_codex_command,
            repository_root=repository_root,
        )
        if blockers:
            return self._blocked_result(artifacts, import_id, runtime_project_id, blockers)

        existing = session.get(RuntimePlanImport, import_id)
        if existing is not None:
            conflicts = self._existing_import_conflicts(session, artifacts, existing)
            if conflicts:
                return self._conflict_result(session, artifacts, import_id, runtime_project_id, conflicts)
            return self._result(
                "ALREADY_IMPORTED",
                session,
                artifacts,
                import_id,
                runtime_project_id,
                include_guarded_dry_run=include_guarded_dry_run,
            )

        existing_residual_tasks = tuple(
            session.scalars(
                select(Task.id).where(Task.project_id == runtime_project_id, Task.id.like(f"{RESIDUAL_IMPORT_PREFIX}%")).order_by(Task.id)
            ).all()
        )
        if existing_residual_tasks:
            return self._conflict_result(
                session,
                artifacts,
                import_id,
                runtime_project_id,
                (f"existing residual runtime tasks: {', '.join(existing_residual_tasks)}",),
            )

        try:
            self._import_new(session, artifacts, import_id, runtime_project_id, repository_root)
            session.flush()
        except (IntegrityError, SQLAlchemyError) as exc:
            session.rollback()
            raise RuntimeError("residual runtime plan import failed") from exc

        return self._result(
            "IMPORTED",
            session,
            artifacts,
            import_id,
            runtime_project_id,
            include_guarded_dry_run=include_guarded_dry_run,
        )

    def _import_new(
        self,
        session: Session,
        artifacts: ResidualRuntimeHandoffArtifacts,
        import_id: str,
        project_id: str,
        repository_root: Path,
    ) -> None:
        plan = artifacts.plan
        lock = artifacts.dry_run.lock
        project = session.get(Project, project_id)
        if project is None:
            project = Project(id=project_id, name=f"AI-Enterprise Runtime {project_id}")
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
                    manifest_ref=f"{lock.residual_plan_id}/v{lock.plan_version}",
                    baseline_commit=baseline_commit,
                    current_stage="residual_runtime_plan_imported",
                    started_at=utc_now(),
                )
            )
            session.flush()
        BootstrapRunRepository().activate_postgresql_authority(session, project_id=project_id, run_id=run_id)

        session.add(
            RuntimePlanImport(
                id=import_id,
                project_id=project_id,
                plan_project_id=project_id,
                plan_id=lock.residual_plan_id,
                plan_version=lock.plan_version,
                status="imported",
                imported_at=utc_now(),
                compiled_project_hash=lock.post_compiled_hash,
                capability_resolution_hash=lock.post_capability_hash,
                trace_validation_hash=lock.post_trace_hash,
                implementation_plan_hash=lock.residual_plan_hash,
                feasibility_hash=lock.residual_feasibility_hash,
                dry_run_hash=lock.residual_dry_run_hash,
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
                    execution_class="implementation",
                    schedulable=True,
                    fingerprint=task.fingerprint,
                )
            )
            feasibility = feasibility_by_task[task.id]
            implements = {
                "requirements": list(task.requirements),
                "capabilities": list(task.capabilities),
                "components": list(task.components),
                "interfaces": list(task.interfaces),
                "residual_gap_refs": list(task.gap_ids),
                "task_type": task.task_type,
            }
            session.add(
                RuntimeTaskPlanBinding(
                    task_id=task.id,
                    import_id=import_id,
                    plan_id=lock.residual_plan_id,
                    plan_version=lock.plan_version,
                    fingerprint=task.fingerprint,
                    risk_level=task.risk["level"],
                    agent_role=task.execution["agent_role"],
                    model_profile=task.execution["model_profile"],
                    executor=task.execution["executor"],
                    verification_profile=str(task.verification["profile"]),
                    feasibility_status=feasibility.feasibility_status,
                    policy_decision=feasibility.policy_decision,
                    implements_json=_canonical_text(implements),
                    write_scope_json=_canonical_text(task.write_scope),
                    acceptance_json=_canonical_text(task.verification["acceptance_criteria"]),
                )
            )

        for task_id, depends_on_task_id in _runtime_dependency_edges(plan):
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
                    plan_id=lock.residual_plan_id,
                    plan_version=lock.plan_version,
                    status="pending",
                    reason=str(gate["reason"]),
                    risk_level=risk_by_task[task_id],
                    approval_boundary=str(gate.get("required_before", "execution")),
                    expected_evidence_json=_canonical_text(["explicit human approval recorded before residual execution"]),
                    downstream_task_ids_json=_canonical_text(downstream_by_task[task_id]),
                    resume_semantics="resume guarded residual run from the same PostgreSQL project after approval",
                )
            )

    def _preflight_blockers(
        self,
        session: Session,
        artifacts: ResidualRuntimeHandoffArtifacts,
        *,
        project_id: str,
        require_clean_git: bool,
        require_codex_command: bool,
        repository_root: Path,
    ) -> tuple[str, ...]:
        blockers: list[str] = []
        plan = artifacts.plan
        feasibility = artifacts.feasibility
        dry_run = artifacts.dry_run
        lock = dry_run.lock
        acceptance = artifacts.rpg_acceptance
        if acceptance.get("gate_id") != "RPG-001":
            blockers.append("RPG-001 acceptance artifact missing")
        if acceptance.get("gate_result") not in {"ACCEPTED", "ACCEPTED_WITH_PREREQUISITES"}:
            blockers.append(f"RPG-001 result is {acceptance.get('gate_result')}")
        if lock.state != "FROZEN" or dry_run.frozen_plan_state != "FROZEN":
            blockers.append("residual plan state is not FROZEN")
        if lock.residual_plan_id != RESIDUAL_PLAN_ID or lock.plan_version != RESIDUAL_PLAN_VERSION:
            blockers.append("residual plan ID/version mismatch")
        if dry_run.plan_acceptance_state not in {"READY_FOR_RESIDUAL_PLAN_ACCEPTANCE", "READY_WITH_EXECUTION_PREREQUISITES"}:
            blockers.append(f"residual plan acceptance state is {dry_run.plan_acceptance_state}")
        blockers.extend(_accepted_hash_mismatches(artifacts.accepted_bound_hashes, dry_run))
        if plan.residual_plan_hash != lock.residual_plan_hash or feasibility.residual_plan_hash != lock.residual_plan_hash:
            blockers.append("residual plan hash mismatch")
        if feasibility.residual_feasibility_hash != lock.residual_feasibility_hash:
            blockers.append("residual feasibility hash mismatch")
        if dry_run.residual_dry_run_hash != lock.residual_dry_run_hash:
            blockers.append("residual dry-run hash mismatch")
        if len(plan.tasks) != 7 or len(lock.task_fingerprints) != 7:
            blockers.append("residual task count mismatch")
        if len(plan.dependency_edges) != 6:
            blockers.append("residual dependency edge count mismatch")
        if len(lock.human_gate_definitions) != 2:
            blockers.append("residual human gate count mismatch")
        if any(finding.severity == "ERROR" for finding in dry_run.findings):
            blockers.append("residual dry-run has ERROR findings")
        for task in plan.tasks:
            if lock.task_fingerprints.get(task.id) != task.fingerprint:
                blockers.append(f"residual task fingerprint mismatch:{task.id}")
        prior_plan = _prior_plan_receipt(session, project_id)
        if prior_plan is None:
            blockers.append("prior PLAN-1a75a2e3c5a7/v1 receipt missing")
        elif not _prior_plan_complete(session, project_id):
            blockers.append("prior PLAN-1a75a2e3c5a7/v1 is not complete")
        if BootstrapRunRepository().get_active_authority(session, project_id) is None:
            blockers.append("PostgreSQL authority is not active")
        if require_codex_command:
            command = os.environ.get("AIENT_CODEX_COMMAND", "").strip()
            if not command:
                blockers.append("AIENT_CODEX_COMMAND is not configured")
            elif shutil.which(shlex.split(command)[0]) is None:
                blockers.append("AIENT_CODEX_COMMAND executable cannot be resolved")
        if require_clean_git and not _git_clean(repository_root):
            blockers.append("Git repository is not clean")
        return tuple(sorted(set(blockers)))

    def _existing_import_conflicts(
        self,
        session: Session,
        artifacts: ResidualRuntimeHandoffArtifacts,
        existing: RuntimePlanImport,
    ) -> tuple[str, ...]:
        lock = artifacts.dry_run.lock
        conflicts: list[str] = []
        expected = {
            "project_id": existing.project_id,
            "plan_id": lock.residual_plan_id,
            "plan_version": lock.plan_version,
            "compiled_project_hash": lock.post_compiled_hash,
            "capability_resolution_hash": lock.post_capability_hash,
            "trace_validation_hash": lock.post_trace_hash,
            "implementation_plan_hash": lock.residual_plan_hash,
            "feasibility_hash": lock.residual_feasibility_hash,
            "dry_run_hash": lock.residual_dry_run_hash,
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
                conflicts.append(f"missing imported residual task or binding:{task.id}")
                continue
            if persisted.project_id != existing.project_id or persisted.fingerprint != task.fingerprint:
                conflicts.append(f"residual task mismatch:{task.id}")
            if binding.import_id != existing.id or binding.fingerprint != task.fingerprint:
                conflicts.append(f"residual binding mismatch:{task.id}")
            implements = json.loads(binding.implements_json)
            if implements.get("task_type") != task.task_type:
                conflicts.append(f"residual task type mismatch:{task.id}")
        dependency_edges = {
            (task_id, depends_on)
            for task_id, depends_on in session.execute(
                select(TaskDependency.task_id, TaskDependency.depends_on_task_id).where(
                    TaskDependency.task_id.in_([task.id for task in artifacts.plan.tasks])
                )
            )
        }
        if dependency_edges != set(_runtime_dependency_edges(artifacts.plan)):
            conflicts.append("residual dependency graph mismatch")
        gate_ids = set(session.scalars(select(RuntimeHumanGate.id).where(RuntimeHumanGate.import_id == existing.id)).all())
        if gate_ids != {str(gate["id"]) for gate in lock.human_gate_definitions}:
            conflicts.append("residual human gate mismatch")
        return tuple(sorted(conflicts))

    def _result(
        self,
        status: ResidualImportStatus,
        session: Session,
        artifacts: ResidualRuntimeHandoffArtifacts,
        import_id: str,
        project_id: str,
        *,
        include_guarded_dry_run: bool,
    ) -> ResidualRuntimeImportResult:
        task_ids = _residual_task_ids(session, project_id)
        guarded: dict[str, Any] | None = None
        if include_guarded_dry_run:
            guarded_result = GuardedAutonomousRunner(persist_summary=False).dry_run(
                session,
                GuardedRunConfig(project_id=project_id, max_tasks_per_run=1, max_repairs_per_task=1, dry_run=True),
            )
            guarded = {
                "dry_run": guarded_result.dry_run,
                "stop_reason": guarded_result.stop_reason,
                "likely_next_task_id": guarded_result.likely_next_task_id,
                "remaining_ready_tasks": list(guarded_result.remaining_ready_tasks),
                "blockers": list(guarded_result.blockers),
            }
        return ResidualRuntimeImportResult(
            status=status,
            import_id=import_id,
            project_id=project_id,
            residual_plan_id=artifacts.dry_run.lock.residual_plan_id,
            plan_version=artifacts.dry_run.lock.plan_version,
            tasks_imported=len(task_ids),
            dependency_edges_imported=_dependency_count(session, task_ids),
            human_gates_bound=_human_gate_count(session, import_id),
            effective_concurrency=artifacts.dry_run.lock.effective_concurrency,
            fingerprints_validated=_fingerprints_match(session, artifacts.plan),
            runtime_ready_tasks=tuple(
                task.id
                for task in self.readiness.list_ready_tasks(session, project_id=project_id)
                if task.id.startswith(RESIDUAL_IMPORT_PREFIX)
            ),
            gated_not_ready_tasks=_pending_gate_task_ids(session, import_id),
            executions_after_import=_execution_count(session, task_ids),
            active_leases_after_import=_active_lease_count(session, task_ids),
            prior_plan_preserved=_prior_plan_complete(session, project_id),
            guarded_dry_run=guarded,
        )

    def _blocked_result(
        self,
        artifacts: ResidualRuntimeHandoffArtifacts,
        import_id: str,
        project_id: str,
        blockers: tuple[str, ...],
    ) -> ResidualRuntimeImportResult:
        return ResidualRuntimeImportResult(
            status="BLOCKED",
            import_id=import_id,
            project_id=project_id,
            residual_plan_id=artifacts.dry_run.lock.residual_plan_id,
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
            prior_plan_preserved=False,
            blockers=blockers,
        )

    def _conflict_result(
        self,
        session: Session,
        artifacts: ResidualRuntimeHandoffArtifacts,
        import_id: str,
        project_id: str,
        blockers: tuple[str, ...],
    ) -> ResidualRuntimeImportResult:
        return ResidualRuntimeImportResult(
            status="CONFLICT",
            import_id=import_id,
            project_id=project_id,
            residual_plan_id=artifacts.dry_run.lock.residual_plan_id,
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
            prior_plan_preserved=_prior_plan_complete(session=session, project_id=project_id),
            blockers=blockers,
        )


def load_residual_runtime_handoff_artifacts(
    *,
    manifest_root: Path = Path("manifest/project/ai-ent"),
    pir_artifact: Path = Path("artifacts/pir-001/PIR-001.json"),
    acceptance_artifact_path: Path = Path("artifacts/rpg-001/RPG-001.json"),
    frozen_plan_artifact: Path = Path(".build/compiled/residual-implementation-plan.json"),
    accepted_feasibility_artifact: Path = Path(".build/compiled/residual-feasibility-report.json"),
) -> ResidualRuntimeHandoffArtifacts:
    acceptance = load_residual_plan_acceptance(acceptance_artifact_path)
    accepted_hashes = _accepted_hashes(acceptance)
    if frozen_plan_artifact.exists():
        plan = _load_frozen_residual_plan(frozen_plan_artifact)
    else:
        plan = generate_residual_implementation_plan(manifest_root=manifest_root, pir_artifact=pir_artifact)
    current_feasibility = evaluate_residual_feasibility(plan=plan, manifest_root=manifest_root, pir_artifact=pir_artifact)
    current_dry_run = dry_run_residual_plan(
        plan=plan,
        feasibility=current_feasibility,
        manifest_root=manifest_root,
        pir_artifact=pir_artifact,
        expected_residual_plan_hash=accepted_hashes.get("residual_plan_hash"),
    )
    if (
        current_feasibility.residual_feasibility_hash != accepted_hashes.get("residual_feasibility_hash")
        or current_dry_run.residual_dry_run_hash != accepted_hashes.get("residual_dry_run_hash")
    ):
        planning_environment = _accepted_environment_profile(accepted_feasibility_artifact)
        accepted_feasibility = evaluate_residual_feasibility(
            plan=plan,
            environment=planning_environment,
            manifest_root=manifest_root,
            pir_artifact=pir_artifact,
        )
        accepted_dry_run = dry_run_residual_plan(
            plan=plan,
            feasibility=accepted_feasibility,
            manifest_root=manifest_root,
            pir_artifact=pir_artifact,
            expected_residual_plan_hash=accepted_hashes.get("residual_plan_hash"),
        )
        return ResidualRuntimeHandoffArtifacts(
            plan=plan,
            feasibility=accepted_feasibility,
            dry_run=accepted_dry_run,
            rpg_acceptance=acceptance,
            accepted_bound_hashes=accepted_hashes,
        )
    return ResidualRuntimeHandoffArtifacts(
        plan=plan,
        feasibility=current_feasibility,
        dry_run=current_dry_run,
        rpg_acceptance=acceptance,
        accepted_bound_hashes=accepted_hashes,
    )


def _accepted_hashes(payload: dict[str, Any]) -> dict[str, str]:
    if payload.get("gate_result") not in {"ACCEPTED", "ACCEPTED_WITH_PREREQUISITES"}:
        return {}
    hashes = payload.get("bound_hashes", {})
    if not isinstance(hashes, dict):
        return {}
    return {str(key): str(value) for key, value in hashes.items()}


def _accepted_environment_profile(path: Path) -> EnvironmentProfile:
    payload = json.loads(path.read_text(encoding="utf-8"))
    environment = payload.get("environment_profile")
    if not isinstance(environment, dict):
        raise TypeError(f"accepted residual feasibility artifact lacks environment_profile: {path}")
    values = dict(environment)
    secrets = values.get("configured_secret_names", ())
    values["configured_secret_names"] = tuple(str(item) for item in secrets)
    return EnvironmentProfile(**values)


def _load_frozen_residual_plan(path: Path) -> ResidualImplementationPlan:
    payload = json.loads(path.read_text(encoding="utf-8"))
    tasks = tuple(_residual_task_from_payload(task) for task in payload["tasks"])
    return ResidualImplementationPlan(
        generator_version=str(payload["generator_version"]),
        head=str(payload["head"]),
        post_compiled_hash=str(payload["post_compiled_hash"]),
        post_capability_hash=str(payload["post_capability_hash"]),
        post_trace_hash=str(payload["post_trace_hash"]),
        pir_residual_gap_hash=str(payload["pir_residual_gap_hash"]),
        residual_plan_hash=str(payload["residual_plan_hash"]),
        tasks=tasks,
        dependency_edges=tuple((str(edge[0]), str(edge[1])) for edge in payload["dependency_edges"]),
        waves=tuple(cast(dict[str, Any], wave) for wave in payload["waves"]),
        critical_path=tuple(str(task_id) for task_id in payload["critical_path"]),
        human_gates=tuple(cast(dict[str, Any], gate) for gate in payload["human_gates"]),
        findings=tuple(
            ResidualPlanFinding(
                finding_id=str(finding["finding_id"]),
                severity=cast(ResidualFindingSeverity, str(finding["severity"])),
                subject_id=str(finding["subject_id"]),
                message=str(finding["message"]),
            )
            for finding in payload["findings"]
        ),
    )


def _residual_task_from_payload(payload: dict[str, Any]) -> ResidualGeneratedTask:
    implements = cast(dict[str, list[str]], payload["implements"])
    return ResidualGeneratedTask(
        id=str(payload["id"]),
        title=str(payload["title"]),
        objective=str(payload["objective"]),
        task_type=cast(ResidualTaskType, str(payload["task_type"])),
        gap_ids=tuple(str(item) for item in payload["gap_ids"]),
        capabilities=tuple(str(item) for item in implements["capabilities"]),
        requirements=tuple(str(item) for item in implements["requirements"]),
        components=tuple(str(item) for item in implements["components"]),
        interfaces=tuple(str(item) for item in implements["interfaces"]),
        depends_on=tuple(str(item) for item in payload["depends_on"]),
        write_scope={
            "allowed": tuple(str(item) for item in payload["write_scope"]["allowed"]),
            "prohibited": tuple(str(item) for item in payload["write_scope"]["prohibited"]),
        },
        risk={str(key): str(value) for key, value in payload["risk"].items()},
        execution={str(key): str(value) for key, value in payload["execution"].items()},
        verification=cast(dict[str, Any], payload["verification"]),
        provenance={str(key): str(value) for key, value in payload["provenance"].items()},
        fingerprint=str(payload["fingerprint"]),
    )


def _accepted_hash_mismatches(hashes: dict[str, str], dry_run: ResidualDryRun) -> list[str]:
    observed = {
        "post_compiled_hash": dry_run.post_compiled_hash,
        "post_capability_hash": dry_run.post_capability_hash,
        "post_trace_hash": dry_run.post_trace_hash,
        "pir_residual_gap_hash": dry_run.pir_residual_gap_hash,
        "residual_plan_hash": dry_run.residual_plan_hash,
        "residual_feasibility_hash": dry_run.residual_feasibility_hash,
        "residual_dry_run_hash": dry_run.residual_dry_run_hash,
        "dependency_graph_hash": dry_run.lock.dependency_graph_hash,
        "task_fingerprint_hash": _task_fingerprint_hash(dry_run.lock.task_fingerprints),
    }
    return [f"RPG-001 hash mismatch:{name}" for name, value in sorted(observed.items()) if hashes.get(name) != value]


def _import_id(plan_id: str, plan_version: str) -> str:
    return f"rhi-{plan_id.lower()}-v{plan_version}"


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


def _downstream_by_task(plan: ResidualImplementationPlan) -> dict[str, tuple[str, ...]]:
    dependents: dict[str, set[str]] = {task.id: set() for task in plan.tasks}
    for task in plan.tasks:
        for depends_on in task.depends_on:
            dependents.setdefault(depends_on, set()).add(task.id)

    def collect(task_id: str, seen: set[str] | None = None) -> set[str]:
        seen = seen or set()
        for dependent in dependents.get(task_id, set()):
            if dependent not in seen:
                seen.add(dependent)
                seen.update(collect(dependent, seen))
        return seen

    return {task.id: tuple(sorted(collect(task.id))) for task in plan.tasks}


def _runtime_dependency_edges(plan: ResidualImplementationPlan) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((task.id, depends_on) for task in plan.tasks for depends_on in task.depends_on))


def _residual_task_ids(session: Session, project_id: str) -> tuple[str, ...]:
    return tuple(
        session.scalars(
            select(Task.id)
            .where(Task.project_id == project_id, Task.id.like(f"{RESIDUAL_IMPORT_PREFIX}%"))
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


def _human_gate_count(session: Session, import_id: str) -> int:
    return int(session.scalar(select(func.count()).select_from(RuntimeHumanGate).where(RuntimeHumanGate.import_id == import_id)) or 0)


def _pending_gate_task_ids(session: Session, import_id: str) -> tuple[str, ...]:
    return tuple(
        session.scalars(
            select(RuntimeHumanGate.task_id)
            .where(RuntimeHumanGate.import_id == import_id, RuntimeHumanGate.status == "pending")
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
            select(func.count()).select_from(TaskLease).where(TaskLease.task_id.in_(task_ids), TaskLease.status == "active")
        )
        or 0
    )


def _fingerprints_match(session: Session, plan: ResidualImplementationPlan) -> bool:
    for task in plan.tasks:
        persisted = session.get(Task, task.id)
        binding = session.get(RuntimeTaskPlanBinding, task.id)
        if persisted is None or binding is None:
            return False
        if persisted.fingerprint != task.fingerprint or binding.fingerprint != task.fingerprint:
            return False
    return True


def _prior_plan_receipt(session: Session, project_id: str) -> RuntimePlanImport | None:
    return session.scalars(
        select(RuntimePlanImport).where(
            RuntimePlanImport.project_id == project_id,
            RuntimePlanImport.plan_id == "PLAN-1a75a2e3c5a7",
            RuntimePlanImport.plan_version == "1",
        )
    ).first()


def _prior_plan_complete(session: Session | None, project_id: str) -> bool:
    if session is None:
        return False
    impl_task_ids = tuple(
        session.scalars(select(Task.id).where(Task.project_id == project_id, Task.id.like("IMPL-%")).order_by(Task.id)).all()
    )
    if len(impl_task_ids) != 13:
        return False
    passed = int(session.scalar(select(func.count()).select_from(Task).where(Task.id.in_(impl_task_ids), Task.status == "passed")) or 0)
    return passed == 13
