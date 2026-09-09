from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from ai_ent.bootstrap.git import candidate_tree_hash, changed_files, require_git
from ai_ent.bootstrap.models import ExecutionPackage
from ai_ent.persistence.models import Checkpoint, Execution, Task, TaskLease
from ai_ent.persistence.repositories.checkpoints import CheckpointRepository
from ai_ent.persistence.repositories.executions import ExecutionRepository
from ai_ent.persistence.repositories.leases import LeaseRepository
from ai_ent.persistence.repositories.tasks import TaskRepository
from ai_ent.scheduler.claiming import TaskClaimingService, database_now
from ai_ent.scheduler.finalization import ExecutionFinalizationResult, ExecutionFinalizer
from ai_ent.scheduler.readiness import TaskReadinessService

RecoveryStage = Literal[
    "AFTER_CLAIM",
    "AFTER_WORKTREE_CREATED",
    "DURING_EXECUTOR",
    "AFTER_EXECUTOR_BEFORE_VERIFICATION",
    "AFTER_VERIFICATION_BEFORE_COMMIT",
    "AFTER_COMMIT_BEFORE_DB_FINALIZATION",
    "DURING_REPAIR",
    "BETWEEN_TASKS",
    "UNKNOWN",
]
RecoveryAction = Literal[
    "RESUME_EXECUTION",
    "RESUME_FINALIZATION",
    "COMPLETE_DB_RECONCILIATION",
    "REPAIR_RECONCILIATION",
    "RELEASE_STALE_LEASE",
    "BLOCK",
    "HUMAN_REQUIRED",
    "READY_TO_CONTINUE",
]


@dataclass(frozen=True)
class RecoveryResult:
    interrupted_task_id: str | None
    execution_id: str | None
    detected_stage: RecoveryStage
    evidence: tuple[str, ...]
    action: RecoveryAction
    action_performed: bool = False
    remaining_blocker: str | None = None
    safe_to_continue: bool = False
    finalization: ExecutionFinalizationResult | None = None


class SchedulerRecoveryService:
    def __init__(
        self,
        *,
        tasks: TaskRepository | None = None,
        executions: ExecutionRepository | None = None,
        leases: LeaseRepository | None = None,
        checkpoints: CheckpointRepository | None = None,
        claiming: TaskClaimingService | None = None,
        readiness: TaskReadinessService | None = None,
        finalizer: ExecutionFinalizer | None = None,
    ) -> None:
        self.tasks = tasks or TaskRepository()
        self.executions = executions or ExecutionRepository()
        self.leases = leases or LeaseRepository()
        self.checkpoints = checkpoints or CheckpointRepository()
        self.claiming = claiming or TaskClaimingService()
        self.readiness = readiness or TaskReadinessService()
        self.finalizer = finalizer or ExecutionFinalizer()

    def recover_execution(
        self,
        session: Session,
        *,
        package: ExecutionPackage,
        owner_id: str,
    ) -> RecoveryResult:
        execution = self.executions.get(session, package.execution_id)
        task = self.tasks.get(session, package.task_id)
        if execution is None or task is None:
            return RecoveryResult(
                package.task_id,
                package.execution_id,
                "UNKNOWN",
                ("missing execution or task",),
                "BLOCK",
                remaining_blocker="missing_execution_or_task",
            )
        lease = self.leases.active_for_task(session, task.id)
        checkpoint = self.checkpoints.latest_for_execution(session, execution.id)
        evidence = _execution_evidence(execution, task, lease, checkpoint, package.worktree_path)

        if execution.commit_hash and task.status != "passed":
            return self._complete_db_reconciliation(session, task=task, execution=execution, lease=lease, evidence=evidence)
        if execution.commit_hash and task.status == "passed" and lease is None:
            return RecoveryResult(
                task.id,
                execution.id,
                "AFTER_COMMIT_BEFORE_DB_FINALIZATION",
                evidence,
                "READY_TO_CONTINUE",
                safe_to_continue=True,
            )
        if task.status == "passed" and lease is not None:
            self.claiming.complete(session, lease_id=lease.id, owner_id=lease.owner_id)
            return RecoveryResult(
                task.id,
                execution.id,
                "AFTER_COMMIT_BEFORE_DB_FINALIZATION",
                evidence,
                "RELEASE_STALE_LEASE",
                action_performed=True,
                safe_to_continue=True,
            )
        if execution.status == "succeeded" and execution.commit_hash is None:
            if checkpoint is not None and _checkpoint_status(checkpoint) == "verification_pass":
                expected_tree = checkpoint.tree_hash
                if expected_tree and _worktree_tree(package.worktree_path) != expected_tree:
                    return RecoveryResult(
                        task.id,
                        execution.id,
                        "AFTER_VERIFICATION_BEFORE_COMMIT",
                        evidence + ("candidate tree changed after verification",),
                        "BLOCK",
                        remaining_blocker="stale_verified_candidate",
                    )
            finalization = self.finalizer.finalize(session, package=package, owner_id=owner_id)
            return RecoveryResult(
                task.id,
                execution.id,
                "AFTER_EXECUTOR_BEFORE_VERIFICATION",
                evidence,
                "RESUME_FINALIZATION",
                action_performed=True,
                safe_to_continue=finalization.completed,
                finalization=finalization,
                remaining_blocker=None if finalization.completed else finalization.status,
            )
        if execution.status == "running":
            if not package.worktree_path.exists():
                return RecoveryResult(task.id, execution.id, "AFTER_CLAIM", evidence, "RESUME_EXECUTION", safe_to_continue=True)
            if not changed_files(package.worktree_path):
                return RecoveryResult(
                    task.id,
                    execution.id,
                    "AFTER_WORKTREE_CREATED",
                    evidence,
                    "RESUME_EXECUTION",
                    safe_to_continue=True,
                )
            return RecoveryResult(
                task.id,
                execution.id,
                "DURING_EXECUTOR",
                evidence + ("worktree has unpersisted changes",),
                "HUMAN_REQUIRED",
                remaining_blocker="uncertain_executor_process_state",
            )
        if execution.status in {"failed", "timeout", "cancelled"} and execution.attempt > 1:
            return RecoveryResult(
                task.id,
                execution.id,
                "DURING_REPAIR",
                evidence,
                "REPAIR_RECONCILIATION",
                safe_to_continue=False,
                remaining_blocker=execution.error_classification,
            )
        return RecoveryResult(task.id, execution.id, "UNKNOWN", evidence, "BLOCK", remaining_blocker="unclassified_recovery_state")

    def recover_project(self, session: Session, *, project_id: str) -> RecoveryResult:
        running_tasks = self.tasks.list_by_status(session, "running", project_id=project_id)
        if running_tasks:
            task = running_tasks[0]
            execution = self.executions.latest_for_task(session, task.id)
            return RecoveryResult(
                task.id,
                execution.id if execution is not None else None,
                "UNKNOWN",
                ("running task requires execution-specific recovery",),
                "BLOCK",
                remaining_blocker="running_task_present",
            )
        ready = self.readiness.list_ready_tasks(session, project_id=project_id, limit=1)
        return RecoveryResult(
            ready[0].id if ready else None,
            None,
            "BETWEEN_TASKS",
            ("no interrupted execution",),
            "READY_TO_CONTINUE",
            safe_to_continue=True,
        )

    def _complete_db_reconciliation(
        self,
        session: Session,
        *,
        task: Task,
        execution: Execution,
        lease: TaskLease | None,
        evidence: tuple[str, ...],
    ) -> RecoveryResult:
        self.executions.update_lifecycle(
            session,
            execution.id,
            status="succeeded",
            terminal_state="success",
            commit_hash=execution.commit_hash,
            candidate_tree_hash=execution.candidate_tree_hash,
            finished_at=database_now(session),
        )
        self.tasks.update_status(session, task.id, "passed")
        if lease is not None and lease.status == "active":
            self.claiming.complete(session, lease_id=lease.id, owner_id=lease.owner_id)
        self.checkpoints.create(
            session,
            checkpoint_id=f"checkpoint-{uuid.uuid4().hex}",
            task_id=task.id,
            execution_id=execution.id,
            checkpoint_type="execution",
            state=json.dumps(
                {
                    "status": "db_reconciled",
                    "commit_id": execution.commit_hash,
                    "candidate_tree_hash": execution.candidate_tree_hash,
                },
                sort_keys=True,
            ),
            commit_hash=execution.commit_hash,
            tree_hash=execution.candidate_tree_hash,
        )
        return RecoveryResult(
            task.id,
            execution.id,
            "AFTER_COMMIT_BEFORE_DB_FINALIZATION",
            evidence,
            "COMPLETE_DB_RECONCILIATION",
            action_performed=True,
            safe_to_continue=True,
        )


def _execution_evidence(
    execution: Execution,
    task: Task,
    lease: TaskLease | None,
    checkpoint: Checkpoint | None,
    worktree: Path,
) -> tuple[str, ...]:
    evidence = [
        f"task_status={task.status}",
        f"execution_status={execution.status}",
        f"execution_attempt={execution.attempt}",
        f"execution_commit={execution.commit_hash or ''}",
        f"execution_candidate={execution.candidate_tree_hash or ''}",
        f"worktree_exists={worktree.exists()}",
    ]
    if lease is not None:
        evidence.append(f"lease_status={lease.status}")
    if checkpoint is not None:
        evidence.append(f"checkpoint_status={_checkpoint_status(checkpoint) or ''}")
    return tuple(evidence)


def _checkpoint_status(checkpoint: Checkpoint | None) -> str | None:
    if checkpoint is None:
        return None
    try:
        state = json.loads(checkpoint.state)
    except json.JSONDecodeError:
        return None
    value = state.get("status")
    return value if isinstance(value, str) else None


def _worktree_tree(worktree: Path) -> str | None:
    if not worktree.exists():
        return None
    try:
        require_git(["rev-parse", "--show-toplevel"], cwd=worktree)
        return candidate_tree_hash(worktree)
    except RuntimeError:
        return None
