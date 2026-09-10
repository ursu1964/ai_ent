from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

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
from ai_ent.product_dry_run import EXPECTED_PRD_DEC_001_HASH
from ai_ent.product_plan_acceptance import EXPECTED_PRODUCTIZATION_PLAN_HASH
from ai_ent.product_plan_final_acceptance import EXPECTED_PRODUCT_DRY_RUN_HASH, PRODUCT_PLAN_ID
from ai_ent.project_manifest import GENERATED_MARKER, canonical_bytes
from ai_ent.runtime_handoff import DEFAULT_RUNTIME_PROJECT_ID
from ai_ent.scheduler.readiness import TaskReadinessService

PRODUCT_RUNTIME_HANDOFF_IMPORTER_VERSION = "phi-001.1"
PRODUCT_PLAN_VERSION = "1"
EXPECTED_PPA_ACCEPTANCE_HASH = "cf97940d5848f8c651232e23b733325f4dd274aa7a45078abab0039d80e638e9"
EXPECTED_PFE_FEASIBILITY_HASH = "25cbfa87927d3486e096364c76c532459f75081d2819b0dd2961dfdcbc4855f9"
EXPECTED_PPG_ACCEPTANCE_HASH = "0b05721c30780eb07ad0eb0e71b27fff96dfe7f5614073441e27ced4defcc9a7"
PRODUCT_IMPORT_PREFIX = "PRD-TASK-"
ProductImportStatus = Literal["IMPORTED", "ALREADY_IMPORTED", "CONFLICT", "BLOCKED"]


@dataclass(frozen=True)
class ProductRuntimeHandoffArtifacts:
    product_plan: dict[str, Any]
    import_preview: tuple[dict[str, Any], ...]
    lock: dict[str, Any]
    ppg_acceptance: dict[str, Any]
    pdf: dict[str, Any]


@dataclass(frozen=True)
class ProductRuntimeImportResult:
    status: ProductImportStatus
    import_id: str
    project_id: str
    product_plan_id: str
    plan_version: str
    importer_version: str
    tasks_imported: int
    dependency_edges_imported: int
    human_gates_imported: int
    gates_approved: int
    decision_bindings: dict[str, Any]
    ready_tasks: tuple[str, ...]
    waiting_tasks: tuple[str, ...]
    gated_tasks: tuple[str, ...]
    decision_blocked_tasks: tuple[str, ...]
    product_executions: int
    active_leases: int
    verified_product_commits: int
    deployment_actions: int
    execution_package_compatibility: dict[str, Any]
    decision_readiness_proof: dict[str, Any]
    human_gate_readiness_proof: dict[str, Any]
    guarded_dry_run_result: dict[str, Any] | None
    likely_next_task: str | None
    prior_plan_preservation: dict[str, Any]
    postgresql_authority: str
    idempotent_rerun_result: str | None = None
    blockers: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in {"IMPORTED", "ALREADY_IMPORTED"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "active_leases": self.active_leases,
            "blockers": list(self.blockers),
            "decision_bindings": self.decision_bindings,
            "decision_blocked_tasks": list(self.decision_blocked_tasks),
            "deployment_actions": self.deployment_actions,
            "dependency_edges_imported": self.dependency_edges_imported,
            "decision_readiness_proof": self.decision_readiness_proof,
            "execution_package_compatibility": self.execution_package_compatibility,
            "gated_tasks": list(self.gated_tasks),
            "gates_approved": self.gates_approved,
            "guarded_dry_run_result": self.guarded_dry_run_result,
            "human_gate_readiness_proof": self.human_gate_readiness_proof,
            "human_gates_imported": self.human_gates_imported,
            "idempotent_rerun_result": self.idempotent_rerun_result,
            "import_id": self.import_id,
            "importer_version": self.importer_version,
            "likely_next_task": self.likely_next_task,
            "plan_version": self.plan_version,
            "postgresql_authority": self.postgresql_authority,
            "prior_plan_preservation": self.prior_plan_preservation,
            "product_executions": self.product_executions,
            "product_plan_id": self.product_plan_id,
            "project_id": self.project_id,
            "ready_tasks": list(self.ready_tasks),
            "status": self.status,
            "tasks_imported": self.tasks_imported,
            "verified_product_commits": self.verified_product_commits,
            "waiting_tasks": list(self.waiting_tasks),
        }


class ProductRuntimePlanImporter:
    def __init__(
        self,
        *,
        importer_version: str = PRODUCT_RUNTIME_HANDOFF_IMPORTER_VERSION,
        readiness: TaskReadinessService | None = None,
    ) -> None:
        self.importer_version = importer_version
        self.readiness = readiness or TaskReadinessService()

    def import_product_plan(
        self,
        session: Session,
        artifacts: ProductRuntimeHandoffArtifacts,
        *,
        runtime_project_id: str = DEFAULT_RUNTIME_PROJECT_ID,
        require_clean_git: bool = True,
        require_codex_command: bool = False,
        include_guarded_dry_run: bool = False,
        repository_root: Path = Path("."),
    ) -> ProductRuntimeImportResult:
        import_id = _import_id(artifacts.lock["product_plan_id"], str(artifacts.lock["plan_version"]))
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
                idempotent_rerun_result="ALREADY_IMPORTED / IN_SYNC",
            )

        existing_product_tasks = tuple(
            session.scalars(
                select(Task.id)
                .where(Task.project_id == runtime_project_id, Task.id.like(f"{PRODUCT_IMPORT_PREFIX}%"))
                .order_by(Task.id)
            ).all()
        )
        if existing_product_tasks:
            return self._conflict_result(
                session,
                artifacts,
                import_id,
                runtime_project_id,
                (f"existing product runtime tasks: {', '.join(existing_product_tasks)}",),
            )

        try:
            self._import_new(session, artifacts, import_id, runtime_project_id, repository_root)
            session.flush()
        except (IntegrityError, SQLAlchemyError) as exc:
            session.rollback()
            raise RuntimeError("product runtime plan import failed") from exc

        conflicts = self._existing_import_conflicts(session, artifacts, session.get(RuntimePlanImport, import_id))
        return self._result(
            "IMPORTED",
            session,
            artifacts,
            import_id,
            runtime_project_id,
            include_guarded_dry_run=include_guarded_dry_run,
            idempotent_rerun_result="ALREADY_IMPORTED / IN_SYNC" if not conflicts else f"CONFLICT:{'; '.join(conflicts)}",
        )

    def _import_new(
        self,
        session: Session,
        artifacts: ProductRuntimeHandoffArtifacts,
        import_id: str,
        project_id: str,
        repository_root: Path,
    ) -> None:
        lock = artifacts.lock
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
                    manifest_ref=f"{lock['product_plan_id']}/v{lock['plan_version']}",
                    baseline_commit=baseline_commit,
                    current_stage="product_runtime_plan_imported",
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
                plan_id=str(lock["product_plan_id"]),
                plan_version=str(lock["plan_version"]),
                status="imported",
                imported_at=utc_now(),
                compiled_project_hash=str(lock["accepted_prd_plan_hash"]),
                capability_resolution_hash=str(lock["ppa_acceptance_hash"]),
                trace_validation_hash=str(lock["pfe_feasibility_hash"]),
                implementation_plan_hash=str(lock["prd_dec_001_hash"]),
                feasibility_hash=str(lock["pfe_feasibility_hash"]),
                dry_run_hash=str(lock["product_dry_run_hash"]),
                task_fingerprint_hash=_task_fingerprint_hash(lock["task_fingerprints"]),
                dependency_graph_hash=str(lock["dependency_graph_hash"]),
                importer_version=self.importer_version,
                task_count=len(artifacts.import_preview),
                dependency_count=len(lock["dependency_edges"]),
                human_gate_count=len(lock["human_gate_definitions"]),
                effective_concurrency=int(lock["effective_concurrency"]),
            )
        )
        session.flush()

        title_by_task = _title_by_task(artifacts.product_plan)
        contract_by_task = {str(item["task_id"]): item for item in artifacts.import_preview}
        for task_id in sorted(contract_by_task):
            contract = contract_by_task[task_id]
            session.add(
                Task(
                    id=task_id,
                    project_id=project_id,
                    title=title_by_task.get(task_id, task_id),
                    objective=str(contract["task_id"] + ": " + _objective_for_task(artifacts.product_plan, task_id)),
                    status="pending",
                    execution_class=str(contract["execution_class"]),
                    schedulable=bool(contract["schedulable"]),
                    fingerprint=str(contract["fingerprint"]),
                )
            )
            session.add(
                RuntimeTaskPlanBinding(
                    task_id=task_id,
                    import_id=import_id,
                    plan_id=str(lock["product_plan_id"]),
                    plan_version=str(lock["plan_version"]),
                    fingerprint=str(contract["fingerprint"]),
                    risk_level=str(contract["risk_level"]),
                    agent_role=str(contract["agent_role"]),
                    model_profile=str(contract["model_profile"]),
                    executor=str(contract["executor"]),
                    verification_profile=str(contract["verification_profile"]),
                    feasibility_status=_feasibility_for_task(artifacts, task_id),
                    policy_decision=str(contract["policy_decision"]),
                    implements_json=_canonical_text(
                        {
                            "product_plan_id": lock["product_plan_id"],
                            "task_type": contract["task_type"],
                            "required_tools": contract["required_tools"],
                            "required_infrastructure": contract["required_infrastructure"],
                            "productization": True,
                            "no_e2e_project_hardcoding": True,
                        }
                    ),
                    write_scope_json=_canonical_text(
                        {
                            "allowed": contract["allowed_write_scope"],
                            "prohibited": contract["prohibited_paths"],
                        }
                    ),
                    acceptance_json=_canonical_text(
                        {
                            "acceptance_criteria": contract["acceptance_criteria"],
                            "decision_states": lock["decision_boundaries"],
                            "required_decisions": contract["required_decisions"],
                        }
                    ),
                )
            )

        for task_id, depends_on in _runtime_dependency_edges(lock):
            session.add(TaskDependency(task_id=task_id, depends_on_task_id=depends_on))

        downstream_by_task = _downstream_by_task(artifacts.import_preview)
        for gate in sorted(lock["human_gate_definitions"], key=lambda item: str(item["gate_id"])):
            task_id = str(gate["task_id"])
            session.add(
                RuntimeHumanGate(
                    id=str(gate["gate_id"]),
                    import_id=import_id,
                    task_id=task_id,
                    plan_id=str(lock["product_plan_id"]),
                    plan_version=str(lock["plan_version"]),
                    status="pending",
                    reason=str(gate["reason"]),
                    risk_level=str(gate["risk"]),
                    approval_boundary=str(gate["approval_boundary"]),
                    expected_evidence_json=_canonical_text(gate["expected_evidence"]),
                    downstream_task_ids_json=_canonical_text(downstream_by_task[task_id]),
                    resume_semantics=str(gate.get("resume_semantics", "explicit approval, then reevaluate readiness before claim")),
                )
            )

    def _preflight_blockers(
        self,
        session: Session,
        artifacts: ProductRuntimeHandoffArtifacts,
        *,
        project_id: str,
        require_clean_git: bool,
        require_codex_command: bool,
        repository_root: Path,
    ) -> tuple[str, ...]:
        blockers: list[str] = []
        lock = artifacts.lock
        ppg = artifacts.ppg_acceptance
        pdf = artifacts.pdf
        if ppg.get("gate_id") != "PPG-001":
            blockers.append("PPG-001 acceptance artifact missing")
        if ppg.get("result") not in {"ACCEPTED", "ACCEPTED_WITH_AFFECTED_TASK_DECISIONS"}:
            blockers.append(f"PPG-001 result is {ppg.get('result')}")
        if ppg.get("acceptance_hash") != EXPECTED_PPG_ACCEPTANCE_HASH:
            blockers.append("PPG-001 acceptance hash mismatch")
        if lock.get("product_plan_id") != PRODUCT_PLAN_ID or str(lock.get("plan_version")) != PRODUCT_PLAN_VERSION:
            blockers.append("product plan ID/version mismatch")
        if lock.get("state") != "FROZEN" or pdf.get("frozen_plan_state") != "FROZEN":
            blockers.append("product plan state is not FROZEN")
        if pdf.get("result") not in {"READY_FOR_FROZEN_PRODUCT_PLAN_ACCEPTANCE", "READY_WITH_AFFECTED_TASK_DECISIONS"}:
            blockers.append(f"PDF-001 result is {pdf.get('result')}")
        expected_hashes = {
            "accepted_prd_plan_hash": EXPECTED_PRODUCTIZATION_PLAN_HASH,
            "ppa_acceptance_hash": EXPECTED_PPA_ACCEPTANCE_HASH,
            "pfe_feasibility_hash": EXPECTED_PFE_FEASIBILITY_HASH,
            "prd_dec_001_hash": EXPECTED_PRD_DEC_001_HASH,
            "product_dry_run_hash": EXPECTED_PRODUCT_DRY_RUN_HASH,
        }
        for field, expected in expected_hashes.items():
            if str(lock.get(field)) != expected:
                blockers.append(f"product lock hash mismatch:{field}")
        bound_hashes = ppg.get("bound_hashes", {})
        if isinstance(bound_hashes, dict):
            if bound_hashes.get("accepted_prd_hash") != EXPECTED_PRODUCTIZATION_PLAN_HASH:
                blockers.append("PPG bound PRD hash mismatch")
            if bound_hashes.get("pdf_dry_run_hash") != EXPECTED_PRODUCT_DRY_RUN_HASH:
                blockers.append("PPG bound PDF hash mismatch")
            if bound_hashes.get("pfe_feasibility_hash") != EXPECTED_PFE_FEASIBILITY_HASH:
                blockers.append("PPG bound PFE hash mismatch")
            if bound_hashes.get("prd_dec_001_hash") != EXPECTED_PRD_DEC_001_HASH:
                blockers.append("PPG bound PRD-DEC-001 hash mismatch")
        else:
            blockers.append("PPG bound hashes missing")
        if len(artifacts.import_preview) != 25 or len(lock.get("task_fingerprints", {})) != 25:
            blockers.append("product task count mismatch")
        if len(lock.get("dependency_edges", [])) != 51:
            blockers.append("product dependency edge count mismatch")
        if len(lock.get("human_gate_definitions", [])) != 8:
            blockers.append("product human gate count mismatch")
        if int(lock.get("effective_concurrency", 0)) != 1:
            blockers.append("product effective concurrency mismatch")
        blockers.extend(_graph_blockers(artifacts.import_preview, lock))
        blockers.extend(_decision_boundary_blockers(lock))
        prior = _prior_plan_preservation(session, project_id)
        if not prior["original_plan_complete"]:
            blockers.append("prior PLAN-1a75a2e3c5a7/v1 is not complete")
        if not prior["residual_plan_complete"]:
            blockers.append("prior RESIDUAL-PLAN-a918c449cfe5/v1 is not complete")
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
        if require_clean_git and not _git_synchronized(repository_root):
            blockers.append("Git repository is not synchronized with origin/main")
        return tuple(sorted(set(blockers)))

    def _existing_import_conflicts(
        self,
        session: Session,
        artifacts: ProductRuntimeHandoffArtifacts,
        existing: RuntimePlanImport | None,
    ) -> tuple[str, ...]:
        if existing is None:
            return ("product import receipt missing after import",)
        lock = artifacts.lock
        conflicts: list[str] = []
        expected = {
            "plan_id": PRODUCT_PLAN_ID,
            "plan_version": PRODUCT_PLAN_VERSION,
            "compiled_project_hash": EXPECTED_PRODUCTIZATION_PLAN_HASH,
            "capability_resolution_hash": EXPECTED_PPA_ACCEPTANCE_HASH,
            "trace_validation_hash": EXPECTED_PFE_FEASIBILITY_HASH,
            "implementation_plan_hash": EXPECTED_PRD_DEC_001_HASH,
            "feasibility_hash": EXPECTED_PFE_FEASIBILITY_HASH,
            "dry_run_hash": EXPECTED_PRODUCT_DRY_RUN_HASH,
            "dependency_graph_hash": lock["dependency_graph_hash"],
            "task_count": 25,
            "dependency_count": 51,
            "human_gate_count": 8,
            "effective_concurrency": 1,
        }
        for field, expected_value in expected.items():
            if getattr(existing, field) != expected_value:
                conflicts.append(f"receipt field mismatch:{field}")
        preview_by_task = {str(item["task_id"]): item for item in artifacts.import_preview}
        for task_id, contract in sorted(preview_by_task.items()):
            persisted = session.get(Task, task_id)
            binding = session.get(RuntimeTaskPlanBinding, task_id)
            if persisted is None or binding is None:
                conflicts.append(f"missing imported product task or binding:{task_id}")
                continue
            if persisted.project_id != existing.project_id or persisted.fingerprint != contract["fingerprint"]:
                conflicts.append(f"product task mismatch:{task_id}")
            if binding.import_id != existing.id or binding.fingerprint != contract["fingerprint"]:
                conflicts.append(f"product binding mismatch:{task_id}")
            implements = json.loads(binding.implements_json)
            if implements.get("task_type") != contract["task_type"]:
                conflicts.append(f"product task type mismatch:{task_id}")
            acceptance = json.loads(binding.acceptance_json)
            if acceptance.get("required_decisions") != contract["required_decisions"]:
                conflicts.append(f"product decision binding mismatch:{task_id}")
        dependency_edges = {
            (task_id, depends_on)
            for task_id, depends_on in session.execute(
                select(TaskDependency.task_id, TaskDependency.depends_on_task_id).where(
                    TaskDependency.task_id.in_(tuple(preview_by_task))
                )
            )
        }
        expected_edges = set(_runtime_dependency_edges(lock))
        if dependency_edges != expected_edges:
            conflicts.append("product dependency graph mismatch")
        gate_ids = set(session.scalars(select(RuntimeHumanGate.id).where(RuntimeHumanGate.import_id == existing.id)).all())
        expected_gate_ids = {str(gate["gate_id"]) for gate in lock["human_gate_definitions"]}
        if gate_ids != expected_gate_ids:
            conflicts.append("product human gate mismatch")
        return tuple(sorted(conflicts))

    def _result(
        self,
        status: ProductImportStatus,
        session: Session,
        artifacts: ProductRuntimeHandoffArtifacts,
        import_id: str,
        project_id: str,
        *,
        include_guarded_dry_run: bool,
        idempotent_rerun_result: str | None = None,
    ) -> ProductRuntimeImportResult:
        task_ids = _product_task_ids(session, project_id)
        buckets = _readiness_buckets(session, self.readiness, task_ids)
        guarded = _guarded_dry_run(session, project_id, buckets) if include_guarded_dry_run else None
        decision_proof = _decision_readiness_proof(session, self.readiness, task_ids)
        gate_proof = _human_gate_readiness_proof(session, self.readiness, import_id, task_ids)
        return ProductRuntimeImportResult(
            status=status,
            import_id=import_id,
            project_id=project_id,
            product_plan_id=PRODUCT_PLAN_ID,
            plan_version=PRODUCT_PLAN_VERSION,
            importer_version=self.importer_version,
            tasks_imported=len(task_ids),
            dependency_edges_imported=_dependency_count(session, task_ids),
            human_gates_imported=_human_gate_count(session, import_id),
            gates_approved=_approved_gate_count(session, import_id),
            decision_bindings=artifacts.lock["decision_boundaries"],
            ready_tasks=buckets["READY"],
            waiting_tasks=buckets["WAITING"],
            gated_tasks=buckets["GATED"],
            decision_blocked_tasks=buckets["DECISION_BLOCKED"],
            product_executions=_execution_count(session, task_ids),
            active_leases=_active_lease_count(session, task_ids),
            verified_product_commits=_verified_commit_count(session, task_ids),
            deployment_actions=0,
            execution_package_compatibility=_execution_package_compatibility(artifacts.import_preview),
            decision_readiness_proof=decision_proof,
            human_gate_readiness_proof=gate_proof,
            guarded_dry_run_result=guarded,
            likely_next_task=guarded["likely_next_task"] if guarded else _likely_next(buckets),
            prior_plan_preservation=_prior_plan_preservation(session, project_id),
            postgresql_authority="active" if BootstrapRunRepository().get_active_authority(session, project_id) else "inactive",
            idempotent_rerun_result=idempotent_rerun_result,
        )

    def _blocked_result(
        self,
        artifacts: ProductRuntimeHandoffArtifacts,
        import_id: str,
        project_id: str,
        blockers: tuple[str, ...],
    ) -> ProductRuntimeImportResult:
        return ProductRuntimeImportResult(
            status="BLOCKED",
            import_id=import_id,
            project_id=project_id,
            product_plan_id=PRODUCT_PLAN_ID,
            plan_version=PRODUCT_PLAN_VERSION,
            importer_version=self.importer_version,
            tasks_imported=0,
            dependency_edges_imported=0,
            human_gates_imported=0,
            gates_approved=0,
            decision_bindings=artifacts.lock.get("decision_boundaries", {}),
            ready_tasks=(),
            waiting_tasks=(),
            gated_tasks=(),
            decision_blocked_tasks=(),
            product_executions=0,
            active_leases=0,
            verified_product_commits=0,
            deployment_actions=0,
            execution_package_compatibility={"compatible": False, "issues": list(blockers)},
            decision_readiness_proof={"ok": False, "proofs": [], "issues": list(blockers)},
            human_gate_readiness_proof={"ok": False, "proofs": [], "issues": list(blockers)},
            guarded_dry_run_result=None,
            likely_next_task=None,
            prior_plan_preservation={"original_plan_complete": False, "residual_plan_complete": False},
            postgresql_authority="inactive",
            blockers=blockers,
        )

    def _conflict_result(
        self,
        session: Session,
        artifacts: ProductRuntimeHandoffArtifacts,
        import_id: str,
        project_id: str,
        blockers: tuple[str, ...],
    ) -> ProductRuntimeImportResult:
        return ProductRuntimeImportResult(
            status="CONFLICT",
            import_id=import_id,
            project_id=project_id,
            product_plan_id=PRODUCT_PLAN_ID,
            plan_version=PRODUCT_PLAN_VERSION,
            importer_version=self.importer_version,
            tasks_imported=0,
            dependency_edges_imported=0,
            human_gates_imported=0,
            gates_approved=0,
            decision_bindings=artifacts.lock.get("decision_boundaries", {}),
            ready_tasks=(),
            waiting_tasks=(),
            gated_tasks=(),
            decision_blocked_tasks=(),
            product_executions=0,
            active_leases=0,
            verified_product_commits=0,
            deployment_actions=0,
            execution_package_compatibility={"compatible": False, "issues": list(blockers)},
            decision_readiness_proof={"ok": False, "proofs": [], "issues": list(blockers)},
            human_gate_readiness_proof={"ok": False, "proofs": [], "issues": list(blockers)},
            guarded_dry_run_result=None,
            likely_next_task=None,
            prior_plan_preservation=_prior_plan_preservation(session, project_id),
            postgresql_authority="active" if BootstrapRunRepository().get_active_authority(session, project_id) else "inactive",
            blockers=blockers,
        )


def load_product_runtime_handoff_artifacts(
    *,
    product_plan_path: Path = Path(".build/compiled/productization-plan.json"),
    import_preview_path: Path = Path(".build/compiled/product-task-import-preview.json"),
    lock_path: Path = Path(".build/compiled/product-plan.lock"),
    ppg_path: Path = Path("artifacts/ppg-001/PPG-001.json"),
    pdf_path: Path = Path("artifacts/pdf-001/PDF-001.json"),
) -> ProductRuntimeHandoffArtifacts:
    product_plan = _read_json(product_plan_path)
    preview_payload = _read_json(import_preview_path)
    return ProductRuntimeHandoffArtifacts(
        product_plan=product_plan,
        import_preview=tuple(dict(item) for item in preview_payload["task_import_preview"]),
        lock=_read_json(lock_path),
        ppg_acceptance=_read_json(ppg_path),
        pdf=_read_json(pdf_path),
    )


def write_product_runtime_handoff_evidence(
    result: ProductRuntimeImportResult,
    *,
    artifacts_dir: Path = Path("artifacts"),
    repository_root: Path = Path("."),
) -> None:
    artifact_dir = artifacts_dir / "phi-001"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated": GENERATED_MARKER,
        "phase": "PHI-001",
        "baseline": _git_head(repository_root),
        "result": result.as_dict(),
        "recommendation": "READY_FOR_PRE_001" if result.ok else "PRODUCT_RUNTIME_IMPORT_REMEDIATION_REQUIRED",
    }
    (artifact_dir / "PHI-001.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (artifact_dir / "PHI-001.md").write_text(_markdown(payload), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _import_id(plan_id: str, plan_version: str) -> str:
    return f"phi-{plan_id.lower()}-v{plan_version}"


def _canonical_text(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def _task_fingerprint_hash(fingerprints: dict[str, str]) -> str:
    return hashlib.sha256(canonical_bytes(fingerprints)).hexdigest()


def _title_by_task(plan: dict[str, Any]) -> dict[str, str]:
    return {str(task["task_id"]): str(task["title"]) for task in plan["productization_dag"]["tasks"]}


def _objective_for_task(plan: dict[str, Any], task_id: str) -> str:
    for task in plan["productization_dag"]["tasks"]:
        if task["task_id"] == task_id:
            return str(task["objective"])
    return task_id


def _feasibility_for_task(artifacts: ProductRuntimeHandoffArtifacts, task_id: str) -> str:
    for task in artifacts.ppg_acceptance.get("runtime_snapshot", {}).get("task_statuses", []):
        if task.get("task_id") == task_id:
            return str(task.get("status", "FEASIBLE_WITH_CONDITIONS"))
    for task in _read_task_contracts_from_pdf(artifacts):
        if task.get("task_id") == task_id:
            return str(task.get("feasibility_status", "FEASIBLE_WITH_CONDITIONS"))
    return "FEASIBLE_WITH_CONDITIONS"


def _read_task_contracts_from_pdf(artifacts: ProductRuntimeHandoffArtifacts) -> tuple[dict[str, Any], ...]:
    return tuple(dict(item) for item in artifacts.pdf.get("import_preview", []))


def _downstream_by_task(previews: tuple[dict[str, Any], ...]) -> dict[str, tuple[str, ...]]:
    dependents: dict[str, set[str]] = {str(item["task_id"]): set() for item in previews}
    for item in previews:
        for depends_on in item["depends_on"]:
            dependents.setdefault(str(depends_on), set()).add(str(item["task_id"]))

    def collect(task_id: str, seen: set[str] | None = None) -> set[str]:
        seen = seen or set()
        for dependent in dependents.get(task_id, set()):
            if dependent not in seen:
                seen.add(dependent)
                seen.update(collect(dependent, seen))
        return seen

    return {task_id: tuple(sorted(collect(task_id))) for task_id in dependents}


def _graph_blockers(previews: tuple[dict[str, Any], ...], lock: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    task_ids = {str(item["task_id"]) for item in previews}
    edges = [(str(edge[0]), str(edge[1])) for edge in lock.get("dependency_edges", [])]
    for depends_on, task_id in edges:
        if task_id == depends_on:
            blockers.append(f"self dependency:{task_id}")
        if task_id not in task_ids:
            blockers.append(f"unknown dependency task:{task_id}")
        if depends_on not in task_ids:
            blockers.append(f"unknown dependency target:{depends_on}")
    if _cycle_nodes(task_ids, [(task_id, depends_on) for depends_on, task_id in edges]):
        blockers.append("product dependency cycle")
    fingerprints = lock.get("task_fingerprints", {})
    for item in previews:
        if fingerprints.get(item["task_id"]) != item["fingerprint"]:
            blockers.append(f"product task fingerprint mismatch:{item['task_id']}")
    return blockers


def _runtime_dependency_edges(lock: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(edge[1]), str(edge[0])) for edge in lock["dependency_edges"]))


def _cycle_nodes(task_ids: set[str], edges: list[tuple[str, str]]) -> set[str]:
    dependencies: dict[str, set[str]] = {task_id: set() for task_id in task_ids}
    for task_id, depends_on in edges:
        dependencies.setdefault(task_id, set()).add(depends_on)
    visiting: set[str] = set()
    visited: set[str] = set()
    cycle_members: set[str] = set()

    def visit(task_id: str, path: list[str]) -> None:
        if task_id in visiting:
            cycle_members.update(path[path.index(task_id) :])
            return
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in dependencies.get(task_id, set()):
            visit(dependency, [*path, dependency])
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in sorted(task_ids):
        visit(task_id, [task_id])
    return cycle_members


def _decision_boundary_blockers(lock: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    decisions = lock.get("decision_boundaries", {})
    dec_001 = decisions.get("PRD-DEC-001", {})
    if dec_001.get("state") != "ACCEPTED" or dec_001.get("decision_hash") != EXPECTED_PRD_DEC_001_HASH:
        blockers.append("PRD-DEC-001 is not accepted or hash-bound")
    expected = {
        "PRD-DEC-002": ("PRD-TASK-019", "PRD-TASK-023", "PRD-TASK-025"),
        "PRD-DEC-003": ("PRD-TASK-020", "PRD-TASK-023", "PRD-TASK-025"),
    }
    for decision_id, affected in expected.items():
        state = decisions.get(decision_id, {})
        if state.get("state") != "UNRESOLVED_MUST_RESOLVE_BEFORE_AFFECTED_TASK":
            blockers.append(f"{decision_id} state mismatch")
        if tuple(state.get("affected_tasks", [])) != affected:
            blockers.append(f"{decision_id} affected task mismatch")
    return blockers


def _product_task_ids(session: Session, project_id: str) -> tuple[str, ...]:
    return tuple(
        session.scalars(
            select(Task.id)
            .where(Task.project_id == project_id, Task.id.like(f"{PRODUCT_IMPORT_PREFIX}%"))
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


def _approved_gate_count(session: Session, import_id: str) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(RuntimeHumanGate).where(RuntimeHumanGate.import_id == import_id, RuntimeHumanGate.status == "approved")
        )
        or 0
    )


def _execution_count(session: Session, task_ids: tuple[str, ...] | list[str]) -> int:
    if not task_ids:
        return 0
    return int(session.scalar(select(func.count()).select_from(Execution).where(Execution.task_id.in_(task_ids))) or 0)


def _verified_commit_count(session: Session, task_ids: tuple[str, ...] | list[str]) -> int:
    if not task_ids:
        return 0
    return int(
        session.scalar(
            select(func.count())
            .select_from(Execution)
            .where(Execution.task_id.in_(task_ids), Execution.status == "succeeded", Execution.commit_hash.is_not(None))
        )
        or 0
    )


def _active_lease_count(session: Session, task_ids: tuple[str, ...] | list[str]) -> int:
    if not task_ids:
        return 0
    return int(
        session.scalar(
            select(func.count()).select_from(TaskLease).where(TaskLease.task_id.in_(task_ids), TaskLease.status == "active")
        )
        or 0
    )


def _readiness_buckets(
    session: Session,
    readiness: TaskReadinessService,
    task_ids: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    buckets: dict[str, list[str]] = {"READY": [], "WAITING": [], "GATED": [], "DECISION_BLOCKED": []}
    for task_id in task_ids:
        decision = readiness.evaluate_task(session, task_id)
        if decision.status == "READY":
            buckets["READY"].append(task_id)
        elif decision.status == "DECISION_BLOCKED":
            buckets["DECISION_BLOCKED"].append(task_id)
        elif "pending_human_gate" in decision.reasons:
            buckets["GATED"].append(task_id)
        else:
            buckets["WAITING"].append(task_id)
    return {key: tuple(sorted(value)) for key, value in buckets.items()}


def _decision_readiness_proof(
    session: Session,
    readiness: TaskReadinessService,
    task_ids: tuple[str, ...],
) -> dict[str, Any]:
    expected = {
        "PRD-DEC-002": ("PRD-TASK-019", "PRD-TASK-023", "PRD-TASK-025"),
        "PRD-DEC-003": ("PRD-TASK-020", "PRD-TASK-023", "PRD-TASK-025"),
    }
    proofs: list[dict[str, Any]] = []
    for decision_id, affected_tasks in expected.items():
        for task_id in affected_tasks:
            if task_id not in task_ids:
                proofs.append(
                    {
                        "decision_id": decision_id,
                        "task_id": task_id,
                        "result": "FAIL",
                        "readiness": "NOT_IMPORTED",
                    }
                )
                continue
            nested = session.begin_nested()
            try:
                _satisfy_direct_dependencies(session, task_id)
                _approve_direct_task_gates(session, task_id)
                task = session.get(Task, task_id)
                if task is not None:
                    task.status = "pending"
                session.flush()
                decision = readiness.evaluate_task(session, task_id)
                proofs.append(
                    {
                        "decision_id": decision_id,
                        "task_id": task_id,
                        "result": "PASS" if decision.status == "DECISION_BLOCKED" else "FAIL",
                        "readiness": decision.status,
                        "reasons": list(decision.reasons),
                    }
                )
            finally:
                nested.rollback()
    return {
        "ok": all(proof["result"] == "PASS" for proof in proofs),
        "synthetic_condition": "direct task dependencies passed and direct task gates approved in rolled-back savepoints",
        "proofs": proofs,
    }


def _human_gate_readiness_proof(
    session: Session,
    readiness: TaskReadinessService,
    import_id: str,
    task_ids: tuple[str, ...],
) -> dict[str, Any]:
    gate_rows = session.execute(
        select(RuntimeHumanGate.id, RuntimeHumanGate.task_id)
        .where(RuntimeHumanGate.import_id == import_id, RuntimeHumanGate.status == "pending")
        .order_by(RuntimeHumanGate.id)
    ).all()
    proofs: list[dict[str, Any]] = []
    for gate_id, task_id in gate_rows:
        if task_id not in task_ids:
            proofs.append({"gate_id": gate_id, "task_id": task_id, "result": "FAIL", "readiness": "NOT_IMPORTED"})
            continue
        nested = session.begin_nested()
        try:
            _satisfy_direct_dependencies(session, task_id)
            task = session.get(Task, task_id)
            if task is not None:
                task.status = "pending"
            session.flush()
            decision = readiness.evaluate_task(session, task_id)
            proofs.append(
                {
                    "gate_id": gate_id,
                    "task_id": task_id,
                    "result": "PASS"
                    if decision.status == "NOT_SCHEDULABLE" and "pending_human_gate" in decision.reasons
                    else "FAIL",
                    "readiness": decision.status,
                    "reasons": list(decision.reasons),
                }
            )
        finally:
            nested.rollback()
    return {
        "ok": all(proof["result"] == "PASS" for proof in proofs),
        "synthetic_condition": "direct task dependencies passed in rolled-back savepoints while gates remained pending",
        "proofs": proofs,
    }


def _satisfy_direct_dependencies(session: Session, task_id: str) -> None:
    for dependency in session.scalars(select(TaskDependency).where(TaskDependency.task_id == task_id)).all():
        dependency_task = session.get(Task, dependency.depends_on_task_id)
        if dependency_task is not None:
            dependency_task.status = "passed"


def _approve_direct_task_gates(session: Session, task_id: str) -> None:
    for gate in session.scalars(select(RuntimeHumanGate).where(RuntimeHumanGate.task_id == task_id)).all():
        gate.status = "approved"


def _guarded_dry_run(session: Session, project_id: str, buckets: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    before_executions = int(session.scalar(select(func.count()).select_from(Execution)) or 0)
    before_leases = int(session.scalar(select(func.count()).select_from(TaskLease)) or 0)
    return {
        "dry_run": True,
        "side_effect_free": True,
        "created_executions": int(session.scalar(select(func.count()).select_from(Execution)) or 0) - before_executions,
        "created_leases": int(session.scalar(select(func.count()).select_from(TaskLease)) or 0) - before_leases,
        "project_id": project_id,
        "ready_tasks": list(buckets["READY"]),
        "decision_blocked_tasks": list(buckets["DECISION_BLOCKED"]),
        "gated_tasks": list(buckets["GATED"]),
        "likely_next_task": _likely_next(buckets),
        "limits": {
            "effective_concurrency": 1,
            "max_failures_per_run": 1,
            "max_repairs_per_task": 1,
            "max_tasks_per_run": 1,
        },
    }


def _likely_next(buckets: dict[str, tuple[str, ...]]) -> str | None:
    return buckets["READY"][0] if buckets["READY"] else None


def _execution_package_compatibility(previews: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    required = {
        "acceptance_criteria",
        "agent_role",
        "allowed_write_scope",
        "executor",
        "fingerprint",
        "model_profile",
        "prohibited_paths",
        "required_decisions",
        "task_id",
        "verification_profile",
    }
    issues = [
        f"{item.get('task_id', '<unknown>')} missing {field}"
        for item in previews
        for field in sorted(required)
        if field not in item or item[field] in (None, "", [])
        if field != "required_decisions" or "required_decisions" not in item
    ]
    hardcoded = [
        item["task_id"]
        for item in previews
        if "E2E-TEAM-WORK-TRACKER" in json.dumps(item, sort_keys=True)
    ]
    if hardcoded:
        issues.append(f"E2E project hardcoding present:{','.join(sorted(hardcoded))}")
    return {
        "compatible": not issues,
        "task_count": len(previews),
        "issues": sorted(issues),
        "package_fields": sorted(required),
    }


def _prior_plan_preservation(session: Session, project_id: str) -> dict[str, Any]:
    original_task_ids = tuple(
        session.scalars(select(Task.id).where(Task.project_id == project_id, Task.id.like("IMPL-%")).order_by(Task.id)).all()
    )
    residual_task_ids = tuple(
        session.scalars(select(Task.id).where(Task.project_id == project_id, Task.id.like("RES-%")).order_by(Task.id)).all()
    )
    original_passed = int(session.scalar(select(func.count()).select_from(Task).where(Task.id.in_(original_task_ids), Task.status == "passed")) or 0)
    residual_passed = int(session.scalar(select(func.count()).select_from(Task).where(Task.id.in_(residual_task_ids), Task.status == "passed")) or 0)
    return {
        "original_plan_complete": len(original_task_ids) == 13 and original_passed == 13,
        "original_tasks": len(original_task_ids),
        "original_passed": original_passed,
        "residual_plan_complete": len(residual_task_ids) == 7 and residual_passed == 7,
        "residual_tasks": len(residual_task_ids),
        "residual_passed": residual_passed,
    }


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


def _git_synchronized(repository_root: Path) -> bool:
    head = _git_rev(repository_root, "HEAD")
    upstream = _git_rev(repository_root, "origin/main")
    return head is not None and upstream is not None and head == upstream


def _git_rev(repository_root: Path, revision: str) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", revision],
        cwd=repository_root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


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


def _markdown(payload: dict[str, Any]) -> str:
    result = payload["result"]
    return "\n".join(
        [
            "# PHI-001 Product Runtime Import",
            "",
            f"- Result: {result['status']}",
            f"- Recommendation: {payload['recommendation']}",
            f"- Product plan: {result['product_plan_id']} / v{result['plan_version']}",
            f"- Import ID: {result['import_id']}",
            f"- Tasks imported: {result['tasks_imported']}",
            f"- Dependency edges imported: {result['dependency_edges_imported']}",
            f"- Human gates imported: {result['human_gates_imported']}",
            f"- Executions: {result['product_executions']}",
            f"- Active leases: {result['active_leases']}",
            f"- READY tasks: {', '.join(result['ready_tasks']) or 'none'}",
            f"- GATED tasks: {', '.join(result['gated_tasks']) or 'none'}",
            f"- DECISION_BLOCKED tasks: {', '.join(result['decision_blocked_tasks']) or 'none'}",
            f"- Likely next task: {result['likely_next_task'] or 'none'}",
            f"- Blockers: {', '.join(result['blockers']) or 'none'}",
            "",
        ]
    )
