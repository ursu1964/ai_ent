from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ai_ent.bootstrap.git import (
    capture_candidate,
    commit_verified_candidate,
    require_git,
    validate_scope,
)
from ai_ent.bootstrap.manifest import load_tasks
from ai_ent.bootstrap.models import BootstrapTask, Candidate, ExecutionPackage, VerificationResult
from ai_ent.bootstrap.verifier import verify_task
from ai_ent.persistence.models import Checkpoint, Execution, Task, TaskLease
from ai_ent.persistence.repositories.checkpoints import CheckpointRepository
from ai_ent.persistence.repositories.executions import ExecutionRepository
from ai_ent.persistence.repositories.leases import LeaseRepository
from ai_ent.persistence.repositories.tasks import TaskRepository
from ai_ent.scheduler.claiming import TaskClaimingService, database_now

FinalizationStatus = Literal[
    "COMPLETED",
    "IDEMPOTENT_COMPLETED",
    "PRECONDITION_FAILED",
    "SCOPE_FAILED",
    "VERIFICATION_FAILED",
    "COMMIT_DENIED",
    "COMMIT_CREATED_DB_PENDING",
]
SuccessfulFinalizationKind = Literal[
    "NEW_VERIFIED_CHANGE",
    "ALREADY_SATISFIED_NOOP",
    "REPAIR_CONVERGED_EXISTING_TREE",
]

Verifier = Callable[[BootstrapTask, Path], VerificationResult]
Committer = Callable[[BootstrapTask, Candidate, VerificationResult, str, Path], str | None]


@dataclass(frozen=True)
class ExecutionFinalizationResult:
    status: FinalizationStatus
    execution: Execution | None = None
    task: Task | None = None
    lease: TaskLease | None = None
    candidate: Candidate | None = None
    verification: VerificationResult | None = None
    commit_id: str | None = None
    committed_tree_hash: str | None = None
    success_kind: SuccessfulFinalizationKind | None = None
    checkpoint: Checkpoint | None = None
    worktree_path: Path | None = None
    reason: str | None = None

    @property
    def completed(self) -> bool:
        return self.status in {"COMPLETED", "IDEMPOTENT_COMPLETED"}


class ExecutionFinalizer:
    def __init__(
        self,
        *,
        tasks: TaskRepository | None = None,
        executions: ExecutionRepository | None = None,
        leases: LeaseRepository | None = None,
        checkpoints: CheckpointRepository | None = None,
        claiming: TaskClaimingService | None = None,
        manifest_tasks: dict[str, BootstrapTask] | None = None,
        verifier: Verifier = verify_task,
        committer: Committer = commit_verified_candidate,
        after_verification: Callable[[], None] | None = None,
    ) -> None:
        self.tasks = tasks or TaskRepository()
        self.executions = executions or ExecutionRepository()
        self.leases = leases or LeaseRepository()
        self.checkpoints = checkpoints or CheckpointRepository()
        self.claiming = claiming or TaskClaimingService()
        self.manifest_tasks = manifest_tasks
        self.verifier = verifier
        self.committer = committer
        self.after_verification = after_verification

    def finalize(
        self,
        session: Session,
        *,
        package: ExecutionPackage,
        owner_id: str,
    ) -> ExecutionFinalizationResult:
        execution = self.executions.get(session, package.execution_id)
        task = self.tasks.get(session, package.task_id)
        if execution is None or task is None or execution.task_id != package.task_id:
            return ExecutionFinalizationResult(
                status="PRECONDITION_FAILED",
                execution=execution,
                task=task,
                worktree_path=package.worktree_path,
                reason="execution_task_mismatch",
            )
        if execution.status == "succeeded" and execution.candidate_tree_hash and task.status == "passed":
            return ExecutionFinalizationResult(
                status="IDEMPOTENT_COMPLETED",
                execution=execution,
                task=task,
                worktree_path=package.worktree_path,
                commit_id=execution.commit_hash,
                committed_tree_hash=execution.candidate_tree_hash,
                success_kind=self._recorded_success_kind(session, execution),
            )
        if execution.status != "succeeded" or execution.commit_hash is not None:
            return ExecutionFinalizationResult(
                status="PRECONDITION_FAILED",
                execution=execution,
                task=task,
                worktree_path=package.worktree_path,
                reason="execution_not_ready_for_finalization",
            )

        lease = self.leases.active_for_task(session, package.task_id)
        if lease is None or lease.execution_id != execution.id:
            return ExecutionFinalizationResult(
                status="PRECONDITION_FAILED",
                execution=execution,
                task=task,
                worktree_path=package.worktree_path,
                reason="missing_execution_lease",
            )
        if not self.claiming.has_valid_lease(session, task_id=package.task_id, owner_id=owner_id):
            return ExecutionFinalizationResult(
                status="PRECONDITION_FAILED",
                execution=execution,
                task=task,
                lease=lease,
                worktree_path=package.worktree_path,
                reason="lease_not_owned_or_expired",
            )
        if not package.worktree_path.exists() or not package.worktree_path.is_dir():
            return ExecutionFinalizationResult(
                status="PRECONDITION_FAILED",
                execution=execution,
                task=task,
                lease=lease,
                worktree_path=package.worktree_path,
                reason="missing_worktree",
            )

        manifest_task = self._manifest_task(task)
        if manifest_task is None:
            return ExecutionFinalizationResult(
                status="PRECONDITION_FAILED",
                execution=execution,
                task=task,
                lease=lease,
                worktree_path=package.worktree_path,
                reason="manifest_db_mismatch",
            )

        try:
            require_git(["rev-parse", "--show-toplevel"], cwd=package.worktree_path)
            baseline_commit = require_git(["rev-parse", "HEAD"], cwd=package.worktree_path)
            candidate = capture_candidate(manifest_task, cwd=package.worktree_path)
        except RuntimeError as exc:
            return ExecutionFinalizationResult(
                status="PRECONDITION_FAILED",
                execution=execution,
                task=task,
                lease=lease,
                worktree_path=package.worktree_path,
                reason=str(exc),
            )

        scope = validate_scope(manifest_task, candidate)
        if not scope.ok:
            self._record_failure(
                session,
                execution=execution,
                task=task,
                lease=lease,
                owner_id=owner_id,
                candidate=candidate,
                reason="scope_failed",
                findings=scope.findings,
            )
            return ExecutionFinalizationResult(
                status="SCOPE_FAILED",
                execution=execution,
                task=task,
                lease=lease,
                candidate=candidate,
                worktree_path=package.worktree_path,
                reason="; ".join(scope.findings),
            )

        verification = self.verifier(manifest_task, package.worktree_path)
        if self.after_verification is not None:
            self.after_verification()
        if not verification.ok:
            self._record_failure(
                session,
                execution=execution,
                task=task,
                lease=lease,
                owner_id=owner_id,
                candidate=candidate,
                reason="verification_failed",
                findings=tuple(result.output for result in verification.commands if not result.ok),
            )
            return ExecutionFinalizationResult(
                status="VERIFICATION_FAILED",
                execution=execution,
                task=task,
                lease=lease,
                candidate=candidate,
                verification=verification,
                worktree_path=package.worktree_path,
                reason="verification_failed",
            )

        try:
            if candidate.changed_files:
                success_kind = "NEW_VERIFIED_CHANGE"
                commit_id = self.committer(
                    manifest_task,
                    candidate,
                    verification,
                    f"Complete {manifest_task.id}\n\nExecution: {execution.id}",
                    package.worktree_path,
                )
            else:
                prior_commit = _matching_existing_task_commit(
                    package.worktree_path,
                    task_id=manifest_task.id,
                    execution_id=execution.id,
                )
                if prior_commit is not None:
                    success_kind = "NEW_VERIFIED_CHANGE"
                    commit_id = prior_commit
                else:
                    success_kind = _clean_candidate_success_kind(execution)
                    commit_id = None
            committed_tree_hash = require_git(["rev-parse", "HEAD^{tree}"], cwd=package.worktree_path)
        except RuntimeError as exc:
            self.executions.update_lifecycle(
                session,
                execution.id,
                status="failed",
                candidate_tree_hash=candidate.tree_hash,
                terminal_state="failure",
                error_classification="commit_denied",
            )
            self.tasks.update_status(session, task.id, "blocked")
            self.claiming.release(session, lease_id=lease.id, owner_id=owner_id)
            self._append_checkpoint(
                session,
                task_id=task.id,
                execution_id=execution.id,
                state={
                    "status": "commit_denied",
                    "reason": str(exc),
                    "baseline_commit": baseline_commit,
                    "candidate_tree_hash": candidate.tree_hash,
                },
                tree_hash=candidate.tree_hash,
            )
            return ExecutionFinalizationResult(
                status="COMMIT_DENIED",
                execution=execution,
                task=task,
                lease=lease,
                candidate=candidate,
                verification=verification,
                worktree_path=package.worktree_path,
                reason=str(exc),
            )

        try:
            checkpoint = self._record_success(
                session,
                execution=execution,
                task=task,
                lease=lease,
                owner_id=owner_id,
                candidate=candidate,
                commit_id=commit_id,
                committed_tree_hash=committed_tree_hash,
                success_kind=success_kind,
                baseline_commit=baseline_commit,
                verification=verification,
            )
        except (RuntimeError, SQLAlchemyError) as exc:
            return ExecutionFinalizationResult(
                status="COMMIT_CREATED_DB_PENDING",
                execution=execution,
                task=task,
                lease=lease,
                candidate=candidate,
                verification=verification,
                commit_id=commit_id,
                committed_tree_hash=committed_tree_hash,
                success_kind=success_kind,
                worktree_path=package.worktree_path,
                reason=str(exc),
            )

        return ExecutionFinalizationResult(
            status="COMPLETED",
            execution=execution,
            task=task,
            lease=lease,
            candidate=candidate,
            verification=verification,
            commit_id=commit_id,
            committed_tree_hash=committed_tree_hash,
            success_kind=success_kind,
            checkpoint=checkpoint,
            worktree_path=package.worktree_path,
        )

    def _manifest_task(self, task: Task) -> BootstrapTask | None:
        manifest_tasks = self.manifest_tasks or load_tasks()
        manifest_task = manifest_tasks.get(task.id)
        if manifest_task is None:
            return None
        if task.title != manifest_task.title:
            return None
        if task.execution_class != manifest_task.execution_class:
            return None
        if task.schedulable != manifest_task.schedulable:
            return None
        return manifest_task

    def _record_success(
        self,
        session: Session,
        *,
        execution: Execution,
        task: Task,
        lease: TaskLease,
        owner_id: str,
        candidate: Candidate,
        commit_id: str | None,
        committed_tree_hash: str,
        success_kind: SuccessfulFinalizationKind,
        baseline_commit: str,
        verification: VerificationResult,
    ) -> Checkpoint:
        self.executions.update_lifecycle(
            session,
            execution.id,
            status="succeeded",
            candidate_tree_hash=candidate.tree_hash,
            commit_hash=commit_id,
            terminal_state="success",
            finished_at=database_now(session),
        )
        self.tasks.update_status(session, task.id, "passed")
        self.claiming.complete(session, lease_id=lease.id, owner_id=owner_id)
        return self._append_checkpoint(
            session,
            task_id=task.id,
            execution_id=execution.id,
            state={
                "status": "completed",
                "baseline_commit": baseline_commit,
                "candidate_tree_hash": candidate.tree_hash,
                "committed_tree_hash": committed_tree_hash,
                "commit_id": commit_id,
                "commit_created": success_kind == "NEW_VERIFIED_CHANGE",
                "success_kind": success_kind,
                "verification": _verification_state(verification),
            },
            commit_hash=commit_id,
            tree_hash=candidate.tree_hash,
        )

    def _recorded_success_kind(self, session: Session, execution: Execution) -> SuccessfulFinalizationKind | None:
        checkpoint = self.checkpoints.latest_for_execution(session, execution.id)
        if checkpoint is None:
            return None
        try:
            state = json.loads(checkpoint.state)
        except json.JSONDecodeError:
            return None
        value = state.get("success_kind")
        if value in {"NEW_VERIFIED_CHANGE", "ALREADY_SATISFIED_NOOP", "REPAIR_CONVERGED_EXISTING_TREE"}:
            return value
        return None

    def _record_failure(
        self,
        session: Session,
        *,
        execution: Execution,
        task: Task,
        lease: TaskLease,
        owner_id: str,
        candidate: Candidate,
        reason: str,
        findings: tuple[str, ...],
    ) -> Checkpoint:
        self.executions.update_lifecycle(
            session,
            execution.id,
            status="failed",
            candidate_tree_hash=candidate.tree_hash,
            terminal_state="failure",
            error_classification=reason,
            finished_at=database_now(session),
        )
        self.tasks.update_status(session, task.id, "blocked")
        self.claiming.release(session, lease_id=lease.id, owner_id=owner_id)
        return self._append_checkpoint(
            session,
            task_id=task.id,
            execution_id=execution.id,
            state={
                "status": reason,
                "candidate_tree_hash": candidate.tree_hash,
                "changed_files": list(candidate.changed_files),
                "findings": list(findings),
            },
            tree_hash=candidate.tree_hash,
        )

    def _append_checkpoint(
        self,
        session: Session,
        *,
        task_id: str,
        execution_id: str,
        state: dict[str, object],
        commit_hash: str | None = None,
        tree_hash: str | None = None,
    ) -> Checkpoint:
        return self.checkpoints.create(
            session,
            checkpoint_id=f"checkpoint-{uuid.uuid4().hex}",
            task_id=task_id,
            execution_id=execution_id,
            checkpoint_type="execution",
            state=json.dumps(state, sort_keys=True),
            commit_hash=commit_hash,
            tree_hash=tree_hash,
        )


def _verification_state(verification: VerificationResult) -> list[dict[str, object]]:
    return [
        {
            "command": result.command,
            "returncode": result.returncode,
            "ok": result.ok,
            "output": result.output,
        }
        for result in verification.commands
    ]


def _clean_candidate_success_kind(execution: Execution) -> SuccessfulFinalizationKind:
    if execution.attempt > 1:
        return "REPAIR_CONVERGED_EXISTING_TREE"
    return "ALREADY_SATISFIED_NOOP"


def _matching_existing_task_commit(worktree_path: Path, *, task_id: str, execution_id: str) -> str | None:
    try:
        subject = require_git(["log", "-1", "--format=%s"], cwd=worktree_path)
        body = require_git(["log", "-1", "--format=%b"], cwd=worktree_path)
    except RuntimeError:
        return None
    if subject == f"Complete {task_id}" and f"Execution: {execution_id}" in body:
        return require_git(["rev-parse", "--short", "HEAD"], cwd=worktree_path)
    return None
