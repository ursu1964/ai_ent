from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ai_ent.bootstrap.models import ExecutionPackage
from ai_ent.canonical_project_model import CanonicalProjectModelService
from ai_ent.knowledge_graph import KnowledgeGraph, KnowledgeGraphService
from ai_ent.manifest_compiler import ManifestCompilerService
from ai_ent.persistence.models import RuntimePlanImport, RuntimeTaskPlanBinding, Task
from ai_ent.project_manifest import (
    GENERATED_MARKER,
    PROJECT_MANIFEST_ROOT,
    EnvironmentProfile,
    FeasibilityEvaluation,
    FeasibilityPolicyProfile,
    ImplementationDryRun,
    ImplementationPlan,
    canonical_bytes,
    default_feasibility_policy,
    dry_run_evaluated_plan,
    evaluate_implementation_plan_feasibility,
    generate_compiled_implementation_plan,
    resolve_compiled_capabilities,
    validate_compiled_traces,
)
from ai_ent.runtime_handoff import (
    DEFAULT_RUNTIME_PROJECT_ID,
    RuntimeHandoffArtifacts,
    RuntimePlanImporter,
    RuntimePlanImportResult,
    build_runtime_manifest_tasks,
)
from ai_ent.scheduler.iteration import ExecutionPackageFactory

EXECUTION_PLANNER_CONTRACT_VERSION = "c17.1"
EXECUTION_PLANNER_OUTPUT_SCHEMA_VERSION = "execution-planner-output-v0.1"


@dataclass(frozen=True)
class ExecutionPlannerResult:
    contract_version: str
    schema_version: str
    knowledge_graph: KnowledgeGraph
    implementation_plan: ImplementationPlan
    feasibility: FeasibilityEvaluation
    dry_run: ImplementationDryRun

    @property
    def output_hash(self) -> str:
        return hashlib.sha256(canonical_bytes(self._payload())).hexdigest()

    @property
    def validation_passed(self) -> bool:
        return self.implementation_plan.ok and self.feasibility.ok and self.dry_run.ok

    @property
    def ready_for_runtime_import(self) -> bool:
        return self.validation_passed and self.dry_run.frozen_plan_state == "FROZEN"

    def task_ids_for_requirement(self, requirement_id: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                task.id
                for task in self.implementation_plan.tasks
                if requirement_id in task.implements["requirements"]
            )
        )

    def as_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["output_hash"] = self.output_hash
        return payload

    def _payload(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "component_id": "CMP-004",
            "capability_id": "C17",
            "contract_version": self.contract_version,
            "schema_version": self.schema_version,
            "input_boundary": {
                "service": "knowledge-graph",
                "contract_version": self.knowledge_graph.contract_version,
                "graph_id": self.knowledge_graph.graph_id,
                "graph_hash": self.knowledge_graph.graph_hash,
                "source_compiled_hash": self.knowledge_graph.source_compiled_hash,
            },
            "output_boundary": {
                "service": "runtime-handoff",
                "plan_id": self.dry_run.lock.plan_id,
                "plan_version": self.dry_run.lock.plan_version,
                "dry_run_hash": self.dry_run.dry_run_hash,
                "frozen_plan_state": self.dry_run.frozen_plan_state,
                "plan_acceptance_state": self.dry_run.plan_acceptance_state,
            },
            "implementation_plan": self.implementation_plan.as_dict(),
            "feasibility": self.feasibility.as_dict(),
            "dry_run": self.dry_run.as_dict(),
            "summary": {
                "validation_passed": self.validation_passed,
                "ready_for_runtime_import": self.ready_for_runtime_import,
                "task_count": len(self.implementation_plan.tasks),
                "dependency_edge_count": len(self.implementation_plan.dependency_edges),
                "human_gate_count": len(self.implementation_plan.human_gates),
                "execution_batch_count": len(self.dry_run.execution_batches),
                "effective_parallel_width": self.dry_run.effective_parallel_width,
            },
        }


@dataclass(frozen=True)
class WorkerPackagePlan:
    package: ExecutionPackage
    plan_id: str
    plan_version: str
    task_fingerprint: str
    dry_run_hash: str

    @property
    def package_hash(self) -> str:
        return hashlib.sha256(canonical_bytes(self._payload())).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["package_hash"] = self.package_hash
        return payload

    def _payload(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "component_id": "CMP-004",
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "task_id": self.package.task_id,
            "execution_id": self.package.execution_id,
            "task_fingerprint": self.task_fingerprint,
            "dry_run_hash": self.dry_run_hash,
            "repository_path": str(self.package.repository_path),
            "worktree_path": str(self.package.worktree_path),
            "allowed_paths": list(self.package.allowed_paths),
            "prohibited_paths": list(self.package.prohibited_paths),
            "python_path": str(self.package.python_path),
            "timeout_seconds": self.package.timeout_seconds,
            "instructions_hash": hashlib.sha256(
                self.package.instructions.encode("utf-8")
            ).hexdigest(),
        }


class ExecutionPlannerService:
    """Service boundary for the C17/CMP-004 execution planner contract."""

    def __init__(
        self,
        *,
        compiler: ManifestCompilerService | None = None,
        knowledge_graphs: KnowledgeGraphService | None = None,
        importer: RuntimePlanImporter | None = None,
        contract_version: str = EXECUTION_PLANNER_CONTRACT_VERSION,
        schema_version: str = EXECUTION_PLANNER_OUTPUT_SCHEMA_VERSION,
    ) -> None:
        self.compiler = compiler or ManifestCompilerService()
        self.knowledge_graphs = knowledge_graphs or KnowledgeGraphService()
        self.importer = importer or RuntimePlanImporter()
        self.contract_version = contract_version
        self.schema_version = schema_version

    def plan(
        self,
        root: Path = PROJECT_MANIFEST_ROOT,
        *,
        environment: EnvironmentProfile | None = None,
        policy: FeasibilityPolicyProfile | None = None,
    ) -> ExecutionPlannerResult:
        compilation = self.compiler.compile_for_task_dag(root)
        canonical_model = CanonicalProjectModelService().from_compilation(compilation)
        knowledge_graph = self.knowledge_graphs.from_model(canonical_model)
        resolution = resolve_compiled_capabilities(compilation)
        trace = validate_compiled_traces(compilation, resolution)
        implementation_plan = generate_compiled_implementation_plan(compilation, resolution, trace)
        feasibility_policy = policy or default_feasibility_policy(implementation_plan)
        feasibility = evaluate_implementation_plan_feasibility(
            implementation_plan,
            environment or _deterministic_default_environment(root),
            feasibility_policy,
        )
        dry_run = dry_run_evaluated_plan(implementation_plan, feasibility)
        return ExecutionPlannerResult(
            contract_version=self.contract_version,
            schema_version=self.schema_version,
            knowledge_graph=knowledge_graph,
            implementation_plan=implementation_plan,
            feasibility=feasibility,
            dry_run=dry_run,
        )

    def import_plan(
        self,
        session: Session,
        result: ExecutionPlannerResult,
        *,
        runtime_project_id: str | None = None,
        require_clean_git: bool = True,
        require_codex_command: bool = True,
        repository_root: Path = Path("."),
    ) -> RuntimePlanImportResult:
        return self.importer.import_frozen_plan(
            session,
            self._artifacts(result),
            runtime_project_id=runtime_project_id,
            require_clean_git=require_clean_git,
            require_codex_command=require_codex_command,
            repository_root=repository_root,
        )

    def build_worker_package(
        self,
        session: Session,
        *,
        task_id: str,
        execution_id: str,
        project_id: str = DEFAULT_RUNTIME_PROJECT_ID,
        repository_path: Path = Path("."),
        timeout_seconds: int = 900,
    ) -> WorkerPackagePlan:
        manifest_tasks = build_runtime_manifest_tasks(session, project_id)
        manifest_task = manifest_tasks.get(task_id)
        if manifest_task is None:
            raise ValueError(f"runtime task is not imported from a valid frozen plan: {task_id}")
        task_row = _runtime_task(session, task_id)
        binding, receipt = _runtime_binding_and_receipt(session, task_id)
        package = ExecutionPackageFactory(
            repository_path=repository_path,
            timeout_seconds=timeout_seconds,
            manifest_tasks=manifest_tasks,
        ).build(task_row, execution_id=execution_id)
        return WorkerPackagePlan(
            package=package,
            plan_id=binding.plan_id,
            plan_version=binding.plan_version,
            task_fingerprint=binding.fingerprint,
            dry_run_hash=receipt.dry_run_hash,
        )

    def _artifacts(self, result: ExecutionPlannerResult) -> RuntimeHandoffArtifacts:
        return RuntimeHandoffArtifacts(
            plan=result.implementation_plan,
            feasibility=result.feasibility,
            dry_run=result.dry_run,
        )


ExecutionPlanner = ExecutionPlannerService


def create_execution_plan(
    root: Path = PROJECT_MANIFEST_ROOT,
    *,
    environment: EnvironmentProfile | None = None,
    policy: FeasibilityPolicyProfile | None = None,
) -> ExecutionPlannerResult:
    return ExecutionPlannerService().plan(root, environment=environment, policy=policy)


def _deterministic_default_environment(root: Path) -> EnvironmentProfile:
    return EnvironmentProfile(
        profile_id="execution-planner-default",
        repository_path=str(root),
        git_available=True,
        postgresql_available=True,
        alembic_available=True,
        docker_available=True,
        docker_compose_available=True,
        codex_command_configured=False,
        codex_executable_available=True,
        python_path=str(Path("aient/bin/python")),
        python_available=True,
        ram_mb=8192,
        gpu_available=False,
        external_network="available",
        configured_secret_names=(),
    )


def _runtime_task(session: Session, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if task is None:
        raise ValueError(f"runtime task not found: {task_id}")
    return task


def _runtime_binding_and_receipt(
    session: Session,
    task_id: str,
) -> tuple[RuntimeTaskPlanBinding, RuntimePlanImport]:
    binding = session.get(RuntimeTaskPlanBinding, task_id)
    if binding is None:
        raise ValueError(f"runtime task binding not found: {task_id}")
    receipt = session.get(RuntimePlanImport, binding.import_id)
    if receipt is None:
        raise ValueError(f"runtime plan import not found: {binding.import_id}")
    if receipt.status != "imported":
        raise ValueError(f"runtime plan import is not active: {receipt.id}")
    return binding, receipt
