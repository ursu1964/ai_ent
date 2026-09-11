from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from ai_ent.bootstrap.codex import CodexConfig, build_execution_package
from ai_ent.bootstrap.manifest import load_tasks
from ai_ent.bootstrap.models import BootstrapTask, ExecutionPackage
from ai_ent.bootstrap.paths import ROOT
from ai_ent.bootstrap.worktree import task_worktree_path
from ai_ent.persistence.models import Execution, RuntimeTaskPlanBinding, Task, TaskLease
from ai_ent.persistence.repositories.executions import ExecutionRepository
from ai_ent.scheduler.claiming import ClaimResult, TaskClaimingService
from ai_ent.scheduler.readiness import TaskReadinessService

SchedulerIterationStatus = Literal[
    "NO_READY_TASK",
    "PACKAGE_READY",
    "CLAIM_CONTENTION",
    "BLOCKED",
    "ERROR",
]


@dataclass(frozen=True)
class SchedulerIterationResult:
    status: SchedulerIterationStatus
    project_id: str
    task: Task | None = None
    execution: Execution | None = None
    lease: TaskLease | None = None
    package: ExecutionPackage | None = None
    claim: ClaimResult | None = None
    reason: str | None = None

    @property
    def package_ready(self) -> bool:
        return self.status == "PACKAGE_READY"


@dataclass(frozen=True)
class ExecutionTimeoutPolicy:
    standard_timeout_seconds: int = 900
    high_complexity_timeout_seconds: int = 1800

    def __post_init__(self) -> None:
        if self.standard_timeout_seconds <= 0:
            raise ValueError("standard timeout must be positive")
        if self.high_complexity_timeout_seconds <= 0:
            raise ValueError("high-complexity timeout must be positive")
        if self.high_complexity_timeout_seconds < self.standard_timeout_seconds:
            raise ValueError("high-complexity timeout must be at least the standard timeout")

    @classmethod
    def for_standard_timeout(cls, timeout_seconds: int) -> ExecutionTimeoutPolicy:
        return cls(
            standard_timeout_seconds=timeout_seconds,
            high_complexity_timeout_seconds=timeout_seconds,
        )

    @classmethod
    def for_codex_default(cls, timeout_seconds: int) -> ExecutionTimeoutPolicy:
        return cls(
            standard_timeout_seconds=timeout_seconds,
            high_complexity_timeout_seconds=max(timeout_seconds * 2, 1800),
        )

    @property
    def maximum_timeout_seconds(self) -> int:
        return max(self.standard_timeout_seconds, self.high_complexity_timeout_seconds)

    def timeout_for_task(self, task: Task) -> int:
        binding = task.runtime_plan_binding
        if binding is not None and _requires_high_complexity_timeout(binding):
            return self.high_complexity_timeout_seconds
        return self.standard_timeout_seconds


class ExecutionPackageFactory:
    def __init__(
        self,
        *,
        repository_path: Path = ROOT,
        timeout_seconds: int | None = None,
        timeout_policy: ExecutionTimeoutPolicy | None = None,
        manifest_tasks: dict[str, BootstrapTask] | None = None,
    ) -> None:
        self.repository_path = repository_path
        self.timeout_policy = timeout_policy or (
            ExecutionTimeoutPolicy.for_standard_timeout(timeout_seconds)
            if timeout_seconds is not None
            else None
        )
        self.manifest_tasks = manifest_tasks

    def build(
        self,
        persisted_task: Task,
        *,
        execution_id: str,
        session: Session | None = None,
        project_id: str | None = None,
    ) -> ExecutionPackage:
        manifest_tasks = self._manifest_tasks_for(
            persisted_task,
            session=session,
            project_id=project_id,
        )
        manifest_task = manifest_tasks.get(persisted_task.id)
        if manifest_task is None:
            if persisted_task.runtime_plan_binding is not None:
                raise ValueError(f"runtime task is not imported from a valid frozen plan: {persisted_task.id}")
            raise ValueError(f"manifest task not found: {persisted_task.id}")
        if persisted_task.title != manifest_task.title:
            raise ValueError(f"manifest/db task mismatch: {persisted_task.id}")
        if persisted_task.execution_class != manifest_task.execution_class:
            raise ValueError(f"manifest/db execution class mismatch: {persisted_task.id}")
        if persisted_task.schedulable != manifest_task.schedulable:
            raise ValueError(f"manifest/db schedulable mismatch: {persisted_task.id}")
        return build_execution_package(
            manifest_task,
            repository_path=self.repository_path,
            worktree_path=task_worktree_path(persisted_task.id),
            execution_id=execution_id,
            timeout_seconds=(
                self.timeout_policy.timeout_for_task(persisted_task)
                if self.timeout_policy is not None
                else CodexConfig.from_env().default_timeout_seconds
            ),
        )

    def _manifest_tasks_for(
        self,
        persisted_task: Task,
        *,
        session: Session | None,
        project_id: str | None,
    ) -> dict[str, BootstrapTask]:
        if self.manifest_tasks is not None:
            return self.manifest_tasks
        if persisted_task.runtime_plan_binding is not None:
            if session is None:
                raise ValueError(f"runtime manifest task loader requires a database session: {persisted_task.id}")
            from ai_ent.runtime_handoff import build_runtime_manifest_tasks

            return build_runtime_manifest_tasks(session, project_id or persisted_task.project_id)
        return load_tasks()


def _requires_high_complexity_timeout(binding: RuntimeTaskPlanBinding) -> bool:
    risk = binding.risk_level.upper()
    model_profile = binding.model_profile.lower().replace("_", "-")
    verification_profile = binding.verification_profile.upper()
    return (
        risk == "HIGH"
        or "high-risk" in model_profile
        or verification_profile == "FULL_REGRESSION_SECURITY"
    )


class SchedulerIterationService:
    def __init__(
        self,
        *,
        readiness: TaskReadinessService | None = None,
        claiming: TaskClaimingService | None = None,
        executions: ExecutionRepository | None = None,
        package_factory: ExecutionPackageFactory | None = None,
        owner_id: str = "scheduler",
        lease_duration: timedelta = timedelta(minutes=30),
    ) -> None:
        self.readiness = readiness or TaskReadinessService()
        self.claiming = claiming or TaskClaimingService()
        self.executions = executions or ExecutionRepository()
        self.package_factory = package_factory or ExecutionPackageFactory()
        self.owner_id = owner_id
        self.lease_duration = lease_duration

    def run_once(self, session: Session, *, project_id: str, limit: int = 1) -> SchedulerIterationResult:
        ready_tasks = self.readiness.list_ready_tasks(session, project_id=project_id, limit=max(limit, 1))
        if not ready_tasks:
            return SchedulerIterationResult(status="NO_READY_TASK", project_id=project_id)

        task = ready_tasks[0]
        claim = self.claiming.claim_task(
            session,
            task_id=task.id,
            owner_id=self.owner_id,
            lease_duration=self.lease_duration,
        )
        if not claim.claimed:
            status: SchedulerIterationStatus = (
                "CLAIM_CONTENTION" if claim.status == "ALREADY_LEASED" else "BLOCKED"
            )
            return SchedulerIterationResult(status=status, project_id=project_id, task=task, claim=claim, reason=claim.status)
        if claim.execution is None or claim.lease is None:
            return SchedulerIterationResult(
                status="ERROR",
                project_id=project_id,
                task=task,
                claim=claim,
                reason="claim_missing_execution_or_lease",
            )

        try:
            package = self.package_factory.build(
                task,
                execution_id=claim.execution.id,
                session=session,
                project_id=project_id,
            )
        except ValueError as exc:
            self.claiming.release(session, lease_id=claim.lease.id, owner_id=self.owner_id)
            self.executions.update_lifecycle(
                session,
                claim.execution.id,
                status="failed",
                terminal_state="failure",
                error_classification="package_construction_failed",
            )
            task.status = "blocked"
            session.flush()
            return SchedulerIterationResult(
                status="ERROR",
                project_id=project_id,
                task=task,
                execution=claim.execution,
                lease=claim.lease,
                claim=claim,
                reason=str(exc),
            )

        return SchedulerIterationResult(
            status="PACKAGE_READY",
            project_id=project_id,
            task=task,
            execution=claim.execution,
            lease=claim.lease,
            package=package,
            claim=claim,
        )
