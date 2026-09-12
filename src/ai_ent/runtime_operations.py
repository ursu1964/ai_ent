from __future__ import annotations

import signal
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from sqlalchemy import delete, inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ai_ent.persistence.models import (
    Base,
    BootstrapCheckpoint,
    BootstrapStateAuthority,
    RuntimeHumanGate,
    RuntimeTaskPlanBinding,
    TaskLease,
)
from ai_ent.persistence.repositories.bootstrap import BootstrapRunRepository

CleanupActionKind = Literal["expire_lease", "delete_terminal_lease", "delete_state_snapshot"]
CleanupStatus = Literal["planned", "applied", "blocked"]
MigrationCheckStatus = Literal["pass", "fail"]

MANDATORY_VERIFICATION_COMMANDS: tuple[str, ...] = (
    "/home/user/projects/ai_ent/aient/bin/python -m pytest -q",
    "/home/user/projects/ai_ent/aient/bin/python -m ruff check .",
    "/home/user/projects/ai_ent/aient/bin/python -m pyright",
)

CONTROL_PLANE_TABLES: frozenset[str] = frozenset(
    {
        "bootstrap_runs",
        "bootstrap_checkpoints",
        "bootstrap_state_authority",
        "runtime_plan_imports",
        "runtime_task_plan_bindings",
        "runtime_human_gates",
    }
)


@dataclass(frozen=True)
class RetentionPolicy:
    stale_active_lease_grace: timedelta = timedelta(minutes=5)
    terminal_lease_retention: timedelta | None = None
    state_snapshot_retention: timedelta | None = None
    minimum_state_snapshots_per_run: int = 1

    def __post_init__(self) -> None:
        if self.stale_active_lease_grace < timedelta():
            raise ValueError("stale_active_lease_grace must be non-negative")
        if self.terminal_lease_retention is not None and self.terminal_lease_retention <= timedelta():
            raise ValueError("terminal_lease_retention must be positive when set")
        if self.state_snapshot_retention is not None and self.state_snapshot_retention <= timedelta():
            raise ValueError("state_snapshot_retention must be positive when set")
        if self.minimum_state_snapshots_per_run < 1:
            raise ValueError("minimum_state_snapshots_per_run must be at least 1")


@dataclass(frozen=True)
class CleanupAction:
    kind: CleanupActionKind
    target_id: str
    reason: str


@dataclass(frozen=True)
class CleanupResult:
    status: CleanupStatus
    planned_at: datetime
    dry_run: bool
    human_gate_confirmed: bool
    actions: tuple[CleanupAction, ...]
    applied_count: int = 0
    blocked_reason: str | None = None

    @property
    def requires_human_gate(self) -> bool:
        return bool(self.actions) and (self.dry_run or not self.human_gate_confirmed)


class RuntimeCleanupService:
    """Retention-aware cleanup that leaves authoritative control-plane rows intact."""

    def __init__(self, policy: RetentionPolicy | None = None) -> None:
        self.policy = policy or RetentionPolicy()

    def plan(self, session: Session, *, now: datetime | None = None) -> CleanupResult:
        planned_at = _as_utc(now or datetime.now(UTC))
        return CleanupResult(
            status="planned",
            planned_at=planned_at,
            dry_run=True,
            human_gate_confirmed=False,
            actions=tuple(self._actions(session, planned_at)),
        )

    def apply(
        self,
        session: Session,
        *,
        now: datetime | None = None,
        human_gate_confirmed: bool = False,
    ) -> CleanupResult:
        planned_at = _as_utc(now or datetime.now(UTC))
        actions = tuple(self._actions(session, planned_at))
        if actions and not human_gate_confirmed:
            return CleanupResult(
                status="blocked",
                planned_at=planned_at,
                dry_run=False,
                human_gate_confirmed=False,
                actions=actions,
                blocked_reason="explicit_cleanup_human_gate_required",
            )

        applied = 0
        for action in actions:
            if action.kind == "expire_lease":
                lease = session.get(TaskLease, action.target_id)
                if lease is not None and lease.status == "active":
                    lease.status = "expired"
                    applied += 1
            elif action.kind == "delete_terminal_lease":
                applied += _rowcount(session.execute(delete(TaskLease).where(TaskLease.id == action.target_id)))
            elif action.kind == "delete_state_snapshot":
                applied += _rowcount(
                    session.execute(delete(BootstrapCheckpoint).where(BootstrapCheckpoint.id == action.target_id))
                )
        session.flush()
        return CleanupResult(
            status="applied",
            planned_at=planned_at,
            dry_run=False,
            human_gate_confirmed=human_gate_confirmed,
            actions=actions,
            applied_count=applied,
        )

    def _actions(self, session: Session, now: datetime) -> Iterable[CleanupAction]:
        yield from self._stale_active_lease_actions(session, now)
        yield from self._terminal_lease_actions(session, now)
        yield from self._state_snapshot_actions(session, now)

    def _stale_active_lease_actions(self, session: Session, now: datetime) -> Iterable[CleanupAction]:
        cutoff = now - self.policy.stale_active_lease_grace
        leases = session.scalars(
            select(TaskLease)
            .where(TaskLease.status == "active")
            .where(TaskLease.expires_at <= cutoff)
            .order_by(TaskLease.expires_at, TaskLease.id)
        ).all()
        for lease in leases:
            yield CleanupAction("expire_lease", lease.id, "active lease expired beyond retention grace")

    def _terminal_lease_actions(self, session: Session, now: datetime) -> Iterable[CleanupAction]:
        if self.policy.terminal_lease_retention is None:
            return
        cutoff = now - self.policy.terminal_lease_retention
        leases = session.scalars(
            select(TaskLease)
            .where(TaskLease.status.in_(("released", "completed", "expired")))
            .where(TaskLease.updated_at <= cutoff)
            .order_by(TaskLease.updated_at, TaskLease.id)
        ).all()
        for lease in leases:
            yield CleanupAction(
                "delete_terminal_lease",
                lease.id,
                "terminal lease exceeded configured retention period",
            )

    def _state_snapshot_actions(self, session: Session, now: datetime) -> Iterable[CleanupAction]:
        if self.policy.state_snapshot_retention is None:
            return
        cutoff = now - self.policy.state_snapshot_retention
        active_authority_run_ids = set(
            session.scalars(
                select(BootstrapStateAuthority.run_id)
                .where(BootstrapStateAuthority.backend == "postgresql")
                .where(BootstrapStateAuthority.status == "active")
                .order_by(BootstrapStateAuthority.run_id)
            ).all()
        )
        run_ids = tuple(
            session.scalars(
                select(BootstrapCheckpoint.run_id)
                .where(BootstrapCheckpoint.checkpoint_kind == "state_snapshot")
                .distinct()
                .order_by(BootstrapCheckpoint.run_id)
            ).all()
        )
        for run_id in run_ids:
            if run_id in active_authority_run_ids:
                continue
            snapshots = session.scalars(
                select(BootstrapCheckpoint)
                .where(BootstrapCheckpoint.run_id == run_id)
                .where(BootstrapCheckpoint.checkpoint_kind == "state_snapshot")
                .order_by(
                    BootstrapCheckpoint.created_at.desc(),
                    BootstrapCheckpoint.sequence.desc(),
                    BootstrapCheckpoint.id.desc(),
                )
            ).all()
            retained_ids = {snapshot.id for snapshot in snapshots[: self.policy.minimum_state_snapshots_per_run]}
            for snapshot in snapshots[self.policy.minimum_state_snapshots_per_run :]:
                if snapshot.id in retained_ids or _as_utc(snapshot.created_at) > cutoff:
                    continue
                yield CleanupAction(
                    "delete_state_snapshot",
                    snapshot.id,
                    "state snapshot exceeded configured retention period",
                )


@dataclass(frozen=True)
class MigrationCheck:
    name: str
    status: MigrationCheckStatus
    detail: str


@dataclass(frozen=True)
class MigrationCheckResult:
    ok: bool
    checks: tuple[MigrationCheck, ...]

    @property
    def failures(self) -> tuple[MigrationCheck, ...]:
        return tuple(check for check in self.checks if check.status == "fail")


class MigrationSafetyChecker:
    def check(
        self,
        session: Session,
        *,
        project_id: str | None = None,
        require_postgresql_dialect: bool = False,
    ) -> MigrationCheckResult:
        try:
            checks = (
                self._schema_tables_check(session),
                self._authority_check(
                    session,
                    project_id,
                    require_postgresql_dialect=require_postgresql_dialect,
                ),
                self._human_gate_check(session),
                self._verification_profile_check(session),
            )
        except SQLAlchemyError as exc:
            checks = (MigrationCheck("database", "fail", _sanitize_error(exc)),)
        return MigrationCheckResult(
            ok=all(check.status == "pass" for check in checks),
            checks=checks,
        )

    def _schema_tables_check(self, session: Session) -> MigrationCheck:
        table_names = set(inspect(session.connection()).get_table_names())
        expected = set(Base.metadata.tables)
        missing = sorted(expected - table_names)
        if missing:
            return MigrationCheck("schema_tables", "fail", f"missing tables: {', '.join(missing)}")
        return MigrationCheck("schema_tables", "pass", "all mapped tables present")

    def _authority_check(
        self,
        session: Session,
        project_id: str | None,
        *,
        require_postgresql_dialect: bool,
    ) -> MigrationCheck:
        bind = session.get_bind()
        dialect_name = getattr(getattr(bind, "dialect", None), "name", "unknown")
        if project_id is None:
            if require_postgresql_dialect and dialect_name != "postgresql":
                return MigrationCheck(
                    "control_plane_authority",
                    "fail",
                    f"fallback/non-authoritative database dialect={dialect_name}",
                )
            missing = sorted(CONTROL_PLANE_TABLES - set(Base.metadata.tables))
            if missing:
                return MigrationCheck(
                    "control_plane_authority",
                    "fail",
                    f"missing mapped control-plane tables: {', '.join(missing)}",
                )
            return MigrationCheck(
                "control_plane_authority",
                "pass",
                f"control-plane tables mapped; authority marker not project-scoped; dialect={dialect_name}",
            )
        authority = BootstrapRunRepository().get_active_authority(session, project_id)
        if authority is None:
            marker = session.scalars(
                select(BootstrapStateAuthority)
                .where(BootstrapStateAuthority.project_id == project_id)
                .order_by(BootstrapStateAuthority.updated_at.desc(), BootstrapStateAuthority.id)
            ).first()
            detail = _missing_authority_detail(marker, dialect_name)
            return MigrationCheck(
                "control_plane_authority",
                "fail",
                detail,
            )
        if require_postgresql_dialect and dialect_name != "postgresql":
            return MigrationCheck(
                "control_plane_authority",
                "fail",
                f"active postgresql authority marker is not enough on fallback/non-authoritative dialect={dialect_name}",
            )
        return MigrationCheck(
            "control_plane_authority",
            "pass",
            f"active postgresql authority marker preserved for run {authority.run_id}; dialect={dialect_name}",
        )

    def _human_gate_check(self, session: Session) -> MigrationCheck:
        invalid = tuple(
            session.scalars(
                select(RuntimeHumanGate.id)
                .where(RuntimeHumanGate.status.not_in(("pending", "approved", "rejected")))
                .order_by(RuntimeHumanGate.id)
            ).all()
        )
        if invalid:
            return MigrationCheck(
                "human_gates",
                "fail",
                f"runtime human gates have invalid explicit statuses: {', '.join(invalid[:5])}",
            )
        return MigrationCheck("human_gates", "pass", "runtime human gates remain explicit")

    def _verification_profile_check(self, session: Session) -> MigrationCheck:
        missing = tuple(
            session.scalars(
                select(RuntimeTaskPlanBinding.task_id)
                .where(RuntimeTaskPlanBinding.verification_profile == "")
                .order_by(RuntimeTaskPlanBinding.task_id)
            ).all()
        )
        if missing:
            return MigrationCheck(
                "independent_verification",
                "fail",
                f"runtime task bindings missing verification profile: {', '.join(missing[:5])}",
            )
        return MigrationCheck(
            "independent_verification",
            "pass",
            "runtime task bindings require verification profiles",
        )


@dataclass(frozen=True)
class BackupGuidance:
    dump_command: str
    verify_command: str
    restore_drill_command: str
    notes: tuple[str, ...]

    def as_lines(self) -> tuple[str, ...]:
        return (
            "Backup guidance:",
            f"- dump: {self.dump_command}",
            f"- verify: {self.verify_command}",
            f"- restore drill: {self.restore_drill_command}",
            *(f"- note: {note}" for note in self.notes),
        )


def build_backup_guidance(
    *,
    output_path: Path = Path("backups/ai-ent-control-plane.dump"),
) -> BackupGuidance:
    target = _shell_quote(str(output_path))
    database_url = (
        "postgresql://${AIENT_DB_USER:?set AIENT_DB_USER}"
        "@${AIENT_DB_HOST:?set AIENT_DB_HOST}:${AIENT_DB_PORT:?set AIENT_DB_PORT}"
        "/${AIENT_DB_NAME:?set AIENT_DB_NAME}"
    )
    dump = (
        "PGPASSWORD=${AIENT_DB_PASSWORD:?set AIENT_DB_PASSWORD} "
        f'pg_dump --format=custom --no-owner --no-acl --dbname="{database_url}" --file={target}'
    )
    verify = f"pg_restore --list {target} >/dev/null"
    restore = f"createdb ai_ent_restore_drill && pg_restore --dbname=ai_ent_restore_drill --clean --if-exists {target}"
    return BackupGuidance(
        dump_command=dump,
        verify_command=verify,
        restore_drill_command=restore,
        notes=(
            "Run before cleanup or migration apply steps.",
            "Keep retention cleanup behind an explicit operator confirmation.",
            "Run the mandatory independent verification commands after migration and restore drills.",
            "Commands reference environment variables only; no secret values are rendered.",
        ),
    )


class GracefulShutdownController:
    def __init__(self) -> None:
        self._requested = False
        self._signals: list[str] = []

    @property
    def requested(self) -> bool:
        return self._requested

    @property
    def signals(self) -> tuple[str, ...]:
        return tuple(self._signals)

    def request_shutdown(self, signum: int | None = None, _frame: object | None = None) -> None:
        self._requested = True
        if signum is not None:
            try:
                name = signal.Signals(signum).name
            except ValueError:
                name = str(signum)
            self._signals.append(name)

    def install_signal_handlers(self, signals: tuple[int, ...] = (signal.SIGINT, signal.SIGTERM)) -> None:
        for signum in signals:
            signal.signal(signum, self.request_shutdown)


def mandatory_verification_commands() -> tuple[str, ...]:
    return MANDATORY_VERIFICATION_COMMANDS


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _sanitize_error(exc: BaseException) -> str:
    message = str(exc)
    if "password" in message.lower():
        return "database operation failed"
    return message.splitlines()[0] if message else "database operation failed"


def _missing_authority_detail(marker: BootstrapStateAuthority | None, dialect_name: str) -> str:
    if marker is None:
        return f"unknown authority: active postgresql authority marker missing; dialect={dialect_name}"
    if marker.backend != "postgresql":
        return (
            "fallback/non-authoritative authority marker present; "
            f"backend={marker.backend} status={marker.status}; dialect={dialect_name}"
        )
    return (
        "postgresql authority configured but unavailable/inactive; "
        f"backend={marker.backend} status={marker.status}; dialect={dialect_name}"
    )


def _rowcount(result: object) -> int:
    value = getattr(result, "rowcount", 0)
    return value if isinstance(value, int) else 0


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
