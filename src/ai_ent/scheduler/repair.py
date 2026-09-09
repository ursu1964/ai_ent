from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from sqlalchemy.orm import Session

from ai_ent.bootstrap.codex import build_execution_package
from ai_ent.bootstrap.manifest import load_tasks
from ai_ent.bootstrap.models import BootstrapTask, ExecutionPackage
from ai_ent.persistence.models import Checkpoint, Execution, Task
from ai_ent.persistence.repositories.checkpoints import CheckpointRepository
from ai_ent.persistence.repositories.executions import ExecutionRepository
from ai_ent.persistence.repositories.tasks import TaskRepository
from ai_ent.scheduler.claiming import TaskClaimingService, database_now

FailureCategory = Literal[
    "IMPLEMENTATION_DEFECT",
    "VERIFICATION_FAILURE",
    "EXECUTOR_FAILURE",
    "TIMEOUT",
    "ENVIRONMENT_FAILURE",
    "LEASE_OWNERSHIP_FAILURE",
    "REPOSITORY_WORKTREE_FAILURE",
    "CONFIGURATION_FAILURE",
    "POLICY_SECURITY_FAILURE",
    "DB_FINALIZATION_FAILURE",
    "IRRECOVERABLE_SYSTEM_FAILURE",
]
FailureStage = Literal[
    "CLAIM",
    "WORKTREE_PREPARATION",
    "EXECUTOR",
    "CANDIDATE_FREEZE",
    "SCOPE_VALIDATION",
    "VERIFICATION",
    "COMMIT",
    "DB_FINALIZATION",
    "LEASE_FINALIZATION",
]
Retryability = Literal[
    "TRANSIENT",
    "REPAIRABLE",
    "NON_RETRYABLE",
    "RECONCILIATION_REQUIRED",
    "HUMAN_REQUIRED",
]
RepairAction = Literal[
    "RETRY_SAME_EXECUTION_STAGE",
    "REPAIR_WITH_NEW_EXECUTION",
    "REPLAN",
    "BLOCK",
    "ESCALATE_HUMAN",
    "RECONCILE",
]


@dataclass(frozen=True)
class RepairPolicy:
    max_autonomous_repair_attempts: int = 2
    repair_lease_duration: timedelta = timedelta(minutes=30)
    repair_owner_id: str = "repair-planner"


@dataclass(frozen=True)
class FailureClassification:
    category: FailureCategory
    stage: FailureStage
    retryability: Retryability
    reason: str


@dataclass(frozen=True)
class RepairDecision:
    classification: FailureClassification
    action: RepairAction
    failed_execution: Execution
    task: Task
    repair_attempt_count: int
    max_attempts: int
    next_execution: Execution | None = None
    next_package: ExecutionPackage | None = None
    human_reason: str | None = None

    @property
    def next_execution_allowed(self) -> bool:
        return self.next_execution is not None and self.next_package is not None


class FailureClassifier:
    def classify(
        self,
        execution: Execution,
        *,
        latest_checkpoint: Checkpoint | None = None,
    ) -> FailureClassification:
        error = (execution.error_classification or "").lower()
        terminal = (execution.terminal_state or "").lower()
        checkpoint_status = _checkpoint_status(latest_checkpoint)

        if checkpoint_status == "scope_failed" or "scope" in error:
            return FailureClassification(
                "POLICY_SECURITY_FAILURE",
                "SCOPE_VALIDATION",
                "HUMAN_REQUIRED",
                execution.error_classification or "scope validation failed",
            )
        if checkpoint_status == "verification_failed" or "verification" in error:
            return FailureClassification(
                "VERIFICATION_FAILURE",
                "VERIFICATION",
                "REPAIRABLE",
                execution.error_classification or "verification failed",
            )
        if checkpoint_status == "commit_denied" or "commit" in error:
            return FailureClassification(
                "REPOSITORY_WORKTREE_FAILURE",
                "COMMIT",
                "NON_RETRYABLE",
                execution.error_classification or "commit denied",
            )
        if "db_pending" in error or "db" in error:
            return FailureClassification(
                "DB_FINALIZATION_FAILURE",
                "DB_FINALIZATION",
                "RECONCILIATION_REQUIRED",
                execution.error_classification or "database finalization pending",
            )
        if execution.status == "timeout" or terminal == "timeout" or "timeout" in error:
            return FailureClassification("TIMEOUT", "EXECUTOR", "TRANSIENT", execution.error_classification or "timeout")
        if terminal == "not_configured" or "not_configured" in error:
            return FailureClassification(
                "CONFIGURATION_FAILURE",
                "EXECUTOR",
                "HUMAN_REQUIRED",
                execution.error_classification or "executor not configured",
            )
        if "worktree" in error:
            return FailureClassification(
                "REPOSITORY_WORKTREE_FAILURE",
                "WORKTREE_PREPARATION",
                "TRANSIENT",
                execution.error_classification or "worktree failure",
            )
        if "lease" in error or "ownership" in error:
            return FailureClassification(
                "LEASE_OWNERSHIP_FAILURE",
                "LEASE_FINALIZATION",
                "TRANSIENT",
                execution.error_classification or "lease ownership failure",
            )
        if "environment" in error or "postgres" in error or "docker" in error:
            return FailureClassification(
                "ENVIRONMENT_FAILURE",
                "EXECUTOR",
                "TRANSIENT",
                execution.error_classification or "environment failure",
            )
        if execution.status in {"failed", "cancelled"}:
            return FailureClassification(
                "EXECUTOR_FAILURE",
                "EXECUTOR",
                "REPAIRABLE",
                execution.error_classification or terminal or "executor failed",
            )
        return FailureClassification(
            "IRRECOVERABLE_SYSTEM_FAILURE",
            "EXECUTOR",
            "HUMAN_REQUIRED",
            execution.error_classification or "unclassified failure",
        )


class RepairPlanner:
    def __init__(
        self,
        *,
        policy: RepairPolicy | None = None,
        classifier: FailureClassifier | None = None,
        tasks: TaskRepository | None = None,
        executions: ExecutionRepository | None = None,
        checkpoints: CheckpointRepository | None = None,
        claiming: TaskClaimingService | None = None,
        manifest_tasks: dict[str, BootstrapTask] | None = None,
    ) -> None:
        self.policy = policy or RepairPolicy()
        self.classifier = classifier or FailureClassifier()
        self.tasks = tasks or TaskRepository()
        self.executions = executions or ExecutionRepository()
        self.checkpoints = checkpoints or CheckpointRepository()
        self.claiming = claiming or TaskClaimingService()
        self.manifest_tasks = manifest_tasks

    def plan(
        self,
        session: Session,
        *,
        execution_id: str,
    ) -> RepairDecision:
        execution = self.executions.require(session, execution_id)
        task = self.tasks.require(session, execution.task_id)
        latest_checkpoint = self.checkpoints.latest_for_execution(session, execution.id)
        classification = self.classifier.classify(execution, latest_checkpoint=latest_checkpoint)
        repair_attempt_count = _repair_attempt_count(self.executions.list_by_task(session, task.id))
        action = self._action_for(classification, repair_attempt_count)

        if action != "REPAIR_WITH_NEW_EXECUTION":
            human_reason = None
            if action == "ESCALATE_HUMAN":
                human_reason = classification.reason
            return RepairDecision(
                classification=classification,
                action=action,
                failed_execution=execution,
                task=task,
                repair_attempt_count=repair_attempt_count,
                max_attempts=self.policy.max_autonomous_repair_attempts,
                human_reason=human_reason,
            )

        active_lease = self.claiming.leases.active_for_task(session, task.id)
        if active_lease is not None:
            active_lease.status = "released"
            active_lease.released_at = database_now(session)
        task.status = "pending"
        session.flush()

        claim = self.claiming.claim_task(
            session,
            task_id=task.id,
            owner_id=self.policy.repair_owner_id,
            lease_duration=self.policy.repair_lease_duration,
        )
        if not claim.claimed or claim.execution is None:
            return RepairDecision(
                classification=classification,
                action="BLOCK",
                failed_execution=execution,
                task=task,
                repair_attempt_count=repair_attempt_count,
                max_attempts=self.policy.max_autonomous_repair_attempts,
                human_reason=claim.status,
            )

        manifest_task = self._manifest_task(task)
        package = build_execution_package(
            _repair_task(manifest_task, execution, latest_checkpoint, classification),
            execution_id=claim.execution.id,
        )
        return RepairDecision(
            classification=classification,
            action=action,
            failed_execution=execution,
            task=task,
            repair_attempt_count=repair_attempt_count,
            max_attempts=self.policy.max_autonomous_repair_attempts,
            next_execution=claim.execution,
            next_package=package,
        )

    def _action_for(self, classification: FailureClassification, repair_attempt_count: int) -> RepairAction:
        if classification.retryability == "RECONCILIATION_REQUIRED":
            return "RECONCILE"
        if classification.retryability == "HUMAN_REQUIRED":
            return "ESCALATE_HUMAN"
        if classification.retryability == "NON_RETRYABLE":
            return "BLOCK"
        if classification.retryability == "TRANSIENT":
            return "RETRY_SAME_EXECUTION_STAGE"
        if repair_attempt_count >= self.policy.max_autonomous_repair_attempts:
            return "ESCALATE_HUMAN"
        return "REPAIR_WITH_NEW_EXECUTION"

    def _manifest_task(self, task: Task) -> BootstrapTask:
        manifest_tasks = self.manifest_tasks or load_tasks()
        manifest_task = manifest_tasks.get(task.id)
        if manifest_task is None:
            raise ValueError(f"manifest task not found: {task.id}")
        return manifest_task


def _checkpoint_status(checkpoint: Checkpoint | None) -> str | None:
    if checkpoint is None:
        return None
    try:
        state = json.loads(checkpoint.state)
    except json.JSONDecodeError:
        return None
    status = state.get("status")
    return status if isinstance(status, str) else None


def _checkpoint_findings(checkpoint: Checkpoint | None) -> tuple[str, ...]:
    if checkpoint is None:
        return ()
    try:
        state = json.loads(checkpoint.state)
    except json.JSONDecodeError:
        return ()
    findings = state.get("findings")
    if not isinstance(findings, list):
        return ()
    return tuple(item for item in findings if isinstance(item, str))


def _checkpoint_changed_files(checkpoint: Checkpoint | None) -> tuple[str, ...]:
    if checkpoint is None:
        return ()
    try:
        state = json.loads(checkpoint.state)
    except json.JSONDecodeError:
        return ()
    changed_files = state.get("changed_files")
    if not isinstance(changed_files, list):
        return ()
    return tuple(item for item in changed_files if isinstance(item, str))


def _repair_attempt_count(executions: list[Execution]) -> int:
    return sum(1 for execution in executions if execution.attempt > 1 and execution.status == "failed")


def _repair_task(
    task: BootstrapTask,
    execution: Execution,
    checkpoint: Checkpoint | None,
    classification: FailureClassification,
) -> BootstrapTask:
    findings = _checkpoint_findings(checkpoint)
    changed_files = _checkpoint_changed_files(checkpoint)
    repair_context = "\n".join(
        [
            task.objective or "",
            "",
            "Repair context:",
            f"- Previous execution: {execution.id}",
            f"- Failure category: {classification.category}",
            f"- Failure stage: {classification.stage}",
            f"- Failure reason: {classification.reason}",
            *(f"- Changed file: {path}" for path in changed_files),
            *(f"- Finding: {finding}" for finding in findings),
            "Do not broaden allowed paths or weaken policy.",
        ]
    )
    return BootstrapTask(
        id=task.id,
        stage=task.stage,
        title=task.title,
        executor=task.executor,
        depends_on=task.depends_on,
        objective=repair_context,
        allowed_paths=task.allowed_paths,
        outputs=task.outputs,
        execution_class=task.execution_class,
        schedulable=task.schedulable,
        simulation_result=task.simulation_result,
        verification=task.verification,
    )
