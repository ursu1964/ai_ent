from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ai_ent.bootstrap.codex import CodexConfig
from ai_ent.bootstrap.paths import ROOT, VENV_PYTHON
from ai_ent.persistence.models import Project, RuntimePlanImport, RuntimeTaskPlanBinding, Task
from ai_ent.persistence.repositories.bootstrap import BootstrapRunRepository
from ai_ent.scheduler.bounded import BoundedRunResult, BoundedSchedulerRunner
from ai_ent.scheduler.readiness import TaskReadinessService
from ai_ent.scheduler.recovery import RecoveryResult, SchedulerRecoveryService

AutonomyLevel = Literal["bootstrap"]
ExecutionMode = Literal["unrestricted", "bootstrap", "original", "residual", "product"]
GuardedStopReason = Literal[
    "COMPLETED_BOUND",
    "NO_READY_TASK",
    "HUMAN_REQUIRED",
    "BLOCKED",
    "RECONCILIATION_REQUIRED",
    "FAILURE_LIMIT",
    "TIME_LIMIT",
    "TASK_LIMIT",
    "RUNTIME_ERROR",
]


@dataclass(frozen=True)
class GuardedRunConfig:
    project_id: str = "ai-ent"
    execution_mode: ExecutionMode = "unrestricted"
    expected_project_id: str | None = None
    expected_plan_id: str | None = None
    expected_plan_version: str | None = None
    task_id_prefix: str | None = None
    max_tasks_per_run: int = 1
    max_wall_clock_seconds: float = 900.0
    max_failures_per_run: int = 1
    max_repairs_per_task: int = 2
    autonomy_level: AutonomyLevel = "bootstrap"
    stop_on_human_required: bool = True
    stop_on_reconciliation_required: bool = True
    dry_run: bool = False


@dataclass(frozen=True)
class GuardedPreflightResult:
    ok: bool
    detail: str


@dataclass(frozen=True)
class PostgresAuthorityResult:
    ok: bool
    detail: str


@dataclass(frozen=True)
class RoutingValidationResult:
    ok: bool
    detail: str


@dataclass(frozen=True)
class GuardedRunResult:
    run_id: str
    project_id: str
    started_at: datetime
    finished_at: datetime
    stop_reason: GuardedStopReason
    recovered_state: RecoveryResult | None = None
    bounded_result: BoundedRunResult | None = None
    dry_run: bool = False
    tasks_attempted: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    repairs_attempted: int = 0
    commits_created: tuple[str, ...] = ()
    human_action_required: bool = False
    reconciliation_required: bool = False
    remaining_ready_tasks: tuple[str, ...] = ()
    likely_next_task_id: str | None = None
    blockers: tuple[str, ...] = ()


Preflight = Callable[[], GuardedPreflightResult]
AuthorityCheck = Callable[[Session, str], PostgresAuthorityResult]
RunIdFactory = Callable[[], str]


class GuardedAutonomousRunner:
    """Operator-facing guarded boundary around bounded scheduler autonomy."""

    def __init__(
        self,
        *,
        bounded_runner: BoundedSchedulerRunner | None = None,
        recovery: SchedulerRecoveryService | None = None,
        readiness: TaskReadinessService | None = None,
        codex_config: CodexConfig | None = None,
        preflight: Preflight | None = None,
        authority_check: AuthorityCheck | None = None,
        run_id_factory: RunIdFactory | None = None,
        persist_summary: bool = True,
    ) -> None:
        self.bounded_runner = bounded_runner or BoundedSchedulerRunner()
        self.recovery = recovery or SchedulerRecoveryService()
        self.readiness = readiness or TaskReadinessService()
        self.codex_config = (codex_config or CodexConfig.for_scheduler_from_env()).for_scheduler_execution()
        self.preflight = preflight or repository_preflight
        self.authority_check = authority_check or require_postgresql_authority
        self.run_id_factory = run_id_factory or (lambda: f"guarded-{uuid.uuid4().hex}")
        self.persist_summary = persist_summary

    def dry_run(self, session: Session, config: GuardedRunConfig) -> GuardedRunResult:
        return self.run(session, GuardedRunConfig(**{**config.__dict__, "dry_run": True}))

    def run(self, session: Session, config: GuardedRunConfig) -> GuardedRunResult:
        run_id = self.run_id_factory()
        started_at = datetime.now(UTC)

        preflight = self.preflight()
        if not preflight.ok:
            return self._result(
                run_id,
                config,
                started_at,
                "RUNTIME_ERROR",
                blockers=(f"preflight_failed:{preflight.detail}",),
            )

        authority = self.authority_check(session, config.project_id)
        if not authority.ok:
            return self._result(
                run_id,
                config,
                started_at,
                "RUNTIME_ERROR",
                blockers=(f"postgresql_authority_required:{authority.detail}",),
            )

        routing = validate_guarded_routing(session, config)
        if not routing.ok:
            return self._record_summary(
                session,
                self._result(
                    run_id,
                    config,
                    started_at,
                    "BLOCKED",
                    human_action_required=True,
                    blockers=(routing.detail,),
                ),
            )

        recovery = self.recovery.recover_project(session, project_id=config.project_id)
        if not recovery.safe_to_continue:
            stop_reason = _stop_reason_for_recovery(recovery)
            return self._record_summary(
                session,
                self._result(
                    run_id,
                    config,
                    started_at,
                    stop_reason,
                    recovered_state=recovery,
                    human_action_required=stop_reason == "HUMAN_REQUIRED",
                    reconciliation_required=stop_reason == "RECONCILIATION_REQUIRED",
                    blockers=tuple(blocker for blocker in (recovery.remaining_blocker,) if blocker),
                ),
            )

        ready_tasks = tuple(task.id for task in self.readiness.list_ready_tasks(session, project_id=config.project_id))
        if config.dry_run:
            return self._record_summary(
                session,
                self._result(
                    run_id,
                    config,
                    started_at,
                    "NO_READY_TASK" if not ready_tasks else "COMPLETED_BOUND",
                    recovered_state=recovery,
                    dry_run=True,
                    remaining_ready_tasks=ready_tasks,
                    likely_next_task_id=ready_tasks[0] if ready_tasks else None,
                ),
            )

        configuration_error = self.codex_config.scheduler_configuration_error()
        if configuration_error is not None:
            return self._record_summary(
                session,
                self._result(
                    run_id,
                    config,
                    started_at,
                    "BLOCKED",
                    recovered_state=recovery,
                    remaining_ready_tasks=ready_tasks,
                    likely_next_task_id=ready_tasks[0] if ready_tasks else None,
                    human_action_required=True,
                    blockers=(configuration_error,),
                ),
            )

        bounded = self.bounded_runner.run(session, project_id=config.project_id)
        remaining = tuple(task.id for task in self.readiness.list_ready_tasks(session, project_id=config.project_id))
        return self._record_summary(
            session,
            self._result(
                run_id,
                config,
                started_at,
                _map_bounded_stop_reason(bounded.stop_reason),
                recovered_state=recovery,
                bounded_result=bounded,
                tasks_attempted=bounded.tasks_attempted,
                tasks_completed=bounded.tasks_completed,
                tasks_failed=bounded.tasks_failed,
                repairs_attempted=bounded.repairs_attempted,
                commits_created=bounded.commits_created,
                human_action_required=bounded.stop_reason == "HUMAN_REQUIRED",
                reconciliation_required=bounded.stop_reason == "RECONCILIATION_REQUIRED",
                remaining_ready_tasks=remaining,
                likely_next_task_id=remaining[0] if remaining else None,
            ),
        )

    def _record_summary(self, session: Session, result: GuardedRunResult) -> GuardedRunResult:
        if not self.persist_summary:
            return result
        authority = BootstrapRunRepository().get_active_authority(session, result.project_id)
        if authority is None:
            return result
        state = json.dumps(
            {
                "kind": "guarded_run_summary",
                "run_id": result.run_id,
                "project_id": result.project_id,
                "dry_run": result.dry_run,
                "stop_reason": result.stop_reason,
                "tasks_attempted": result.tasks_attempted,
                "tasks_completed": result.tasks_completed,
                "tasks_failed": result.tasks_failed,
                "repairs_attempted": result.repairs_attempted,
                "commits_created": list(result.commits_created),
                "human_action_required": result.human_action_required,
                "reconciliation_required": result.reconciliation_required,
                "remaining_ready_tasks": list(result.remaining_ready_tasks),
                "likely_next_task_id": result.likely_next_task_id,
                "blockers": list(result.blockers),
                "recovery_action": result.recovered_state.action if result.recovered_state else None,
                "recovery_stage": result.recovered_state.detected_stage if result.recovered_state else None,
            },
            sort_keys=True,
        )
        BootstrapRunRepository().append_checkpoint(
            session,
            checkpoint_id=f"{result.run_id}-summary",
            run_id=authority.run_id,
            checkpoint_kind="state_snapshot",
            state=state,
            task_id=result.bounded_result.last_task_id if result.bounded_result else None,
            execution_id=result.bounded_result.last_execution_id if result.bounded_result else None,
            verified_commit=result.commits_created[-1] if result.commits_created else None,
            sequence=BootstrapRunRepository().next_checkpoint_sequence(session, authority.run_id),
        )
        return result

    def _result(
        self,
        run_id: str,
        config: GuardedRunConfig,
        started_at: datetime,
        stop_reason: GuardedStopReason,
        *,
        recovered_state: RecoveryResult | None = None,
        bounded_result: BoundedRunResult | None = None,
        dry_run: bool | None = None,
        tasks_attempted: int = 0,
        tasks_completed: int = 0,
        tasks_failed: int = 0,
        repairs_attempted: int = 0,
        commits_created: tuple[str, ...] = (),
        human_action_required: bool = False,
        reconciliation_required: bool = False,
        remaining_ready_tasks: tuple[str, ...] = (),
        likely_next_task_id: str | None = None,
        blockers: tuple[str, ...] = (),
    ) -> GuardedRunResult:
        return GuardedRunResult(
            run_id=run_id,
            project_id=config.project_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            stop_reason=stop_reason,
            recovered_state=recovered_state,
            bounded_result=bounded_result,
            dry_run=config.dry_run if dry_run is None else dry_run,
            tasks_attempted=tasks_attempted,
            tasks_completed=tasks_completed,
            tasks_failed=tasks_failed,
            repairs_attempted=repairs_attempted,
            commits_created=commits_created,
            human_action_required=human_action_required,
            reconciliation_required=reconciliation_required,
            remaining_ready_tasks=remaining_ready_tasks,
            likely_next_task_id=likely_next_task_id,
            blockers=blockers,
        )


def repository_preflight() -> GuardedPreflightResult:
    preflight_script = ROOT / "scripts" / "preflight.py"
    if not VENV_PYTHON.exists():
        return GuardedPreflightResult(False, "preflight python missing")
    if not preflight_script.exists():
        return GuardedPreflightResult(False, "preflight script missing")
    try:
        completed = subprocess.run(
            [str(VENV_PYTHON), str(preflight_script)],
            cwd=ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        return GuardedPreflightResult(
            False,
            f"preflight command unavailable:{exc.__class__.__name__}",
        )
    if completed.returncode != 0:
        detail = (
            completed.stdout.splitlines()[-1]
            if completed.stdout
            else "preflight command failed"
        )
        return GuardedPreflightResult(False, detail)
    return GuardedPreflightResult(True, "preflight passed")


def require_postgresql_authority(session: Session, project_id: str) -> PostgresAuthorityResult:
    try:
        authority = BootstrapRunRepository().get_active_authority(session, project_id)
    except SQLAlchemyError:
        return PostgresAuthorityResult(False, "database unavailable")
    if authority is None:
        return PostgresAuthorityResult(False, "active postgresql authority marker missing")
    return PostgresAuthorityResult(True, f"postgresql authority active for run {authority.run_id}")


def validate_guarded_routing(session: Session, config: GuardedRunConfig) -> RoutingValidationResult:
    """Validate project/plan/namespace routing before scheduler readiness or claim."""

    if config.execution_mode == "unrestricted":
        return RoutingValidationResult(True, "routing validation not requested")

    project = session.get(Project, config.project_id)
    if project is None:
        return _routing_mismatch(config, reason="project_missing")

    if config.expected_project_id is not None and config.project_id != config.expected_project_id:
        return _routing_mismatch(
            config,
            reason="requested_project_mismatch",
            actual_project_id=config.project_id,
        )

    prefix = _expected_task_prefix(config)
    if config.execution_mode == "bootstrap":
        return _validate_bootstrap_routing(session, config, prefix)
    return _validate_runtime_plan_routing(session, config, prefix)


def _validate_bootstrap_routing(
    session: Session,
    config: GuardedRunConfig,
    prefix: str | None,
) -> RoutingValidationResult:
    imports = _runtime_import_summaries(session, project_id=config.project_id)
    if imports:
        return _routing_mismatch(
            config,
            reason="bootstrap_project_has_runtime_plan_binding",
            actual_plan=", ".join(imports),
        )

    if prefix is not None:
        mismatches = _task_namespace_mismatches(session, project_id=config.project_id, prefix=prefix)
        if mismatches:
            return _routing_mismatch(
                config,
                reason="task_namespace_mismatch",
                actual_namespace=", ".join(mismatches[:5]),
            )
    return RoutingValidationResult(True, "bootstrap routing validated")


def _validate_runtime_plan_routing(
    session: Session,
    config: GuardedRunConfig,
    prefix: str | None,
) -> RoutingValidationResult:
    if config.expected_plan_id is None:
        return _routing_mismatch(config, reason="expected_plan_required")

    plan_query = (
        select(RuntimePlanImport)
        .where(RuntimePlanImport.project_id == config.project_id)
        .where(RuntimePlanImport.plan_id == config.expected_plan_id)
        .where(RuntimePlanImport.status == "imported")
    )
    if config.expected_plan_version is not None:
        plan_query = plan_query.where(RuntimePlanImport.plan_version == config.expected_plan_version)
    plan_import = session.scalars(plan_query.order_by(RuntimePlanImport.imported_at.desc())).first()
    if plan_import is None:
        return _routing_mismatch(
            config,
            reason="runtime_plan_binding_missing",
            actual_plan=", ".join(_runtime_import_summaries(session, project_id=config.project_id)) or "none",
        )

    if prefix is not None:
        binding_task_ids = tuple(
            session.scalars(
                select(RuntimeTaskPlanBinding.task_id)
                .where(RuntimeTaskPlanBinding.import_id == plan_import.id)
                .order_by(RuntimeTaskPlanBinding.task_id)
            ).all()
        )
        if not binding_task_ids:
            return _routing_mismatch(config, reason="runtime_plan_has_no_task_bindings")
        mismatches = tuple(task_id for task_id in binding_task_ids if not task_id.startswith(prefix))
        if mismatches:
            return _routing_mismatch(
                config,
                reason="task_namespace_mismatch",
                actual_namespace=", ".join(mismatches[:5]),
            )

    return RoutingValidationResult(True, "runtime plan routing validated")


def _expected_task_prefix(config: GuardedRunConfig) -> str | None:
    if config.task_id_prefix is not None:
        return config.task_id_prefix
    return {
        "bootstrap": "TASK-",
        "original": "IMPL-",
        "residual": "RES-",
        "product": "PRD-TASK-",
    }.get(config.execution_mode)


def _task_namespace_mismatches(session: Session, *, project_id: str, prefix: str) -> tuple[str, ...]:
    return tuple(
        session.scalars(
            select(Task.id)
            .where(Task.project_id == project_id)
            .where(Task.id.not_like(f"{prefix}%"))
            .order_by(Task.id)
        ).all()
    )


def _runtime_import_summaries(session: Session, *, project_id: str) -> tuple[str, ...]:
    imports = session.scalars(
        select(RuntimePlanImport)
        .where(RuntimePlanImport.project_id == project_id)
        .order_by(RuntimePlanImport.plan_id, RuntimePlanImport.plan_version)
    ).all()
    return tuple(f"{row.plan_id}/v{row.plan_version}:{row.status}" for row in imports)


def _routing_mismatch(
    config: GuardedRunConfig,
    *,
    reason: str,
    actual_project_id: str | None = None,
    actual_plan: str | None = None,
    actual_namespace: str | None = None,
) -> RoutingValidationResult:
    detail = {
        "reason": reason,
        "requested_project": config.project_id,
        "expected_project": config.expected_project_id,
        "execution_mode": config.execution_mode,
        "expected_plan": config.expected_plan_id,
        "expected_plan_version": config.expected_plan_version,
        "expected_namespace": _expected_task_prefix(config),
        "actual_project": actual_project_id,
        "actual_plan": actual_plan,
        "actual_namespace": actual_namespace,
    }
    rendered = ";".join(f"{key}={value}" for key, value in detail.items() if value is not None)
    return RoutingValidationResult(False, f"PROJECT_PLAN_ROUTING_MISMATCH;{rendered}")


def _stop_reason_for_recovery(recovery: RecoveryResult) -> GuardedStopReason:
    if recovery.action == "HUMAN_REQUIRED":
        return "HUMAN_REQUIRED"
    if recovery.action in {"COMPLETE_DB_RECONCILIATION", "REPAIR_RECONCILIATION"}:
        return "RECONCILIATION_REQUIRED"
    if recovery.action == "BLOCK":
        return "BLOCKED"
    return "RECONCILIATION_REQUIRED"


def _map_bounded_stop_reason(reason: str) -> GuardedStopReason:
    mapping: dict[str, GuardedStopReason] = {
        "NO_READY_TASK": "NO_READY_TASK",
        "MAX_TASKS_PER_RUN": "TASK_LIMIT",
        "MAX_WALL_CLOCK_SECONDS": "TIME_LIMIT",
        "MAX_FAILURES_PER_RUN": "FAILURE_LIMIT",
        "HUMAN_REQUIRED": "HUMAN_REQUIRED",
        "BLOCK": "BLOCKED",
        "REPLAN": "BLOCKED",
        "RECONCILIATION_REQUIRED": "RECONCILIATION_REQUIRED",
        "RUNTIME_ERROR": "RUNTIME_ERROR",
    }
    return mapping.get(reason, "RUNTIME_ERROR")
