from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from ai_ent.bootstrap.codex import CodexConfig, CodexExecutor
from ai_ent.bootstrap.git import changed_files, require_git
from ai_ent.bootstrap.models import ExecutionPackage, ExecutionResult
from ai_ent.bootstrap.paths import ROOT
from ai_ent.bootstrap.worktree import create_execution_worktree
from ai_ent.persistence.models import Execution, TaskLease
from ai_ent.persistence.repositories.executions import ExecutionRepository
from ai_ent.persistence.repositories.leases import LeaseRepository
from ai_ent.scheduler.claiming import TaskClaimingService, database_now

ClaimedExecutionStatus = Literal[
    "EXECUTED",
    "NOT_CONFIGURED",
    "OWNERSHIP_LOST",
    "INVALID_EXECUTION",
    "WORKTREE_ERROR",
    "EXECUTOR_FAILED",
]


@dataclass(frozen=True)
class ClaimedExecutionResult:
    status: ClaimedExecutionStatus
    execution: Execution | None = None
    lease: TaskLease | None = None
    package: ExecutionPackage | None = None
    execution_result: ExecutionResult | None = None
    worktree_path: Path | None = None
    changed_files: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def executed(self) -> bool:
        return self.status == "EXECUTED"


class ClaimedExecutionRunner:
    def __init__(
        self,
        *,
        executor: CodexExecutor | None = None,
        config: CodexConfig | None = None,
        executions: ExecutionRepository | None = None,
        leases: LeaseRepository | None = None,
        claiming: TaskClaimingService | None = None,
        repository_path: Path = ROOT,
        worktree_root: Path | None = None,
        lease_grace: timedelta = timedelta(minutes=1),
    ) -> None:
        self.config = config or CodexConfig.from_env()
        self.executor = executor or CodexExecutor(config=self.config)
        self.executions = executions or ExecutionRepository()
        self.leases = leases or LeaseRepository()
        self.claiming = claiming or TaskClaimingService()
        self.repository_path = repository_path
        self.worktree_root = worktree_root
        self.lease_grace = lease_grace

    def run_claimed(
        self,
        session: Session,
        *,
        package: ExecutionPackage,
        owner_id: str,
        baseline_ref: str = "HEAD",
    ) -> ClaimedExecutionResult:
        execution = self.executions.get(session, package.execution_id)
        if execution is None or execution.status not in {"pending", "running"}:
            return ClaimedExecutionResult(status="INVALID_EXECUTION", package=package, reason="invalid_execution")
        lease = self.leases.active_for_task(session, package.task_id)
        if lease is None or lease.execution_id != execution.id:
            return ClaimedExecutionResult(
                status="OWNERSHIP_LOST",
                execution=execution,
                package=package,
                reason="missing_active_lease",
            )
        if not self.claiming.has_valid_lease(session, task_id=package.task_id, owner_id=owner_id):
            return ClaimedExecutionResult(
                status="OWNERSHIP_LOST",
                execution=execution,
                lease=lease,
                package=package,
                reason="lease_not_owned_or_expired",
            )
        if not self.config.command:
            self.executions.update_lifecycle(
                session,
                execution.id,
                status="failed",
                terminal_state="not_configured",
                error_classification="codex_not_configured",
                finished_at=database_now(session),
            )
            return ClaimedExecutionResult(
                status="NOT_CONFIGURED",
                execution=execution,
                lease=lease,
                package=package,
                reason="codex_not_configured",
            )

        renewal = self.claiming.renew(
            session,
            lease_id=lease.id,
            owner_id=owner_id,
            lease_duration=timedelta(seconds=package.timeout_seconds) + self.lease_grace,
        )
        if not renewal.updated:
            return ClaimedExecutionResult(
                status="OWNERSHIP_LOST",
                execution=execution,
                lease=lease,
                package=package,
                reason=renewal.status,
            )

        try:
            require_git(["rev-parse", "--show-toplevel"], cwd=self.repository_path)
            base_commit = require_git(["rev-parse", baseline_ref], cwd=self.repository_path)
            worktree = create_execution_worktree(
                package.task_id,
                execution.id,
                base_ref=base_commit,
                root=self.repository_path,
                worktree_root=self.worktree_root or self.repository_path.parent / ".ai_ent-worktrees",
            )
        except (FileExistsError, RuntimeError) as exc:
            self.executions.update_lifecycle(
                session,
                execution.id,
                status="failed",
                terminal_state="failure",
                error_classification="worktree_error",
                finished_at=database_now(session),
            )
            return ClaimedExecutionResult(
                status="WORKTREE_ERROR",
                execution=execution,
                lease=lease,
                package=package,
                reason=str(exc),
            )

        execution_package = replace(package, repository_path=self.repository_path, worktree_path=worktree)
        started_at = database_now(session)
        self.executions.update_lifecycle(session, execution.id, status="running", started_at=started_at)
        try:
            result = self.executor.execute_package(execution_package)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            finished_at = database_now(session)
            self.executions.update_lifecycle(
                session,
                execution.id,
                status="failed",
                finished_at=finished_at,
                terminal_state="failure",
                error_classification="executor_exception",
            )
            return ClaimedExecutionResult(
                status="EXECUTOR_FAILED",
                execution=execution,
                lease=lease,
                package=execution_package,
                worktree_path=worktree,
                changed_files=changed_files(worktree),
                reason=str(exc),
            )
        actual_changed_files = changed_files(worktree)
        finished_at = database_now(session)
        status = _execution_status(result)
        self.executions.update_lifecycle(
            session,
            execution.id,
            status=status,
            finished_at=finished_at,
            terminal_state=result.terminal_state,
            error_classification=_error_classification(result),
        )
        return ClaimedExecutionResult(
            status="EXECUTED" if result.ok else "EXECUTOR_FAILED",
            execution=execution,
            lease=lease,
            package=execution_package,
            execution_result=result,
            worktree_path=worktree,
            changed_files=actual_changed_files,
        )


def _execution_status(result: ExecutionResult) -> Literal["succeeded", "failed", "timeout", "cancelled"]:
    if result.terminal_state == "timeout":
        return "timeout"
    if result.terminal_state == "cancelled":
        return "cancelled"
    if result.ok:
        return "succeeded"
    return "failed"


def _error_classification(result: ExecutionResult) -> str | None:
    if result.ok:
        return None
    if result.terminal_state == "not_configured":
        return "codex_not_configured"
    if result.terminal_state == "timeout":
        return "executor_timeout"
    return "executor_failed"
