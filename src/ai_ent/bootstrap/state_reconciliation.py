from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ai_ent.bootstrap.manifest import load_bootstrap, load_tasks
from ai_ent.bootstrap.paths import ROOT, STATE_FILE
from ai_ent.persistence.config import DatabaseConfigError, load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.persistence.models import BootstrapRun
from ai_ent.persistence.repositories import (
    BootstrapRunRepository,
    ProjectRepository,
    TaskRepository,
)

ReconciliationOutcome = Literal[
    "IN_SYNC",
    "LOCAL_ONLY",
    "DATABASE_ONLY",
    "DATABASE_AHEAD",
    "LOCAL_AHEAD",
    "CONFLICT",
    "INVALID_LOCAL_STATE",
    "INVALID_DATABASE_STATE",
]


@dataclass(frozen=True)
class NormalizedBootstrapState:
    exists: bool
    project_id: str
    run_id: str
    current_stage: str | None = None
    current_task_id: str | None = None
    last_completed_task_id: str | None = None
    last_verified_commit: str | None = None
    last_checkpoint_task_id: str | None = None
    blocked_tasks: dict[str, str] = field(default_factory=dict)
    completed_tasks: tuple[str, ...] = ()
    authority_backend: str | None = None


@dataclass(frozen=True)
class ReconciliationResult:
    outcome: ReconciliationOutcome
    local: NormalizedBootstrapState | None = None
    database: NormalizedBootstrapState | None = None
    differences: tuple[str, ...] = ()
    backup_path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in {"IN_SYNC", "LOCAL_ONLY", "DATABASE_ONLY"}


class BootstrapStateReconciler:
    def __init__(
        self,
        *,
        state_file: Path = STATE_FILE,
        root: Path = ROOT,
        database: Database | None = None,
        run_id: str = "bootstrap-main",
    ) -> None:
        self.state_file = state_file
        self.root = root
        self.database = database
        self.run_id = run_id
        bootstrap = load_bootstrap()
        self.project_id = str(bootstrap.get("project", "ai-ent"))
        self.manifest_ref = "manifest/bootstrap"
        self.initial_stage = str(bootstrap.get("initial_stage", "B00"))
        self.repository = BootstrapRunRepository()

    def inspect(self) -> ReconciliationResult:
        local = self._load_local()
        if local is None:
            return ReconciliationResult("INVALID_LOCAL_STATE", differences=("malformed_local_json",))
        database = self._with_database(lambda session: self._load_database(session))
        if database is None:
            return ReconciliationResult("LOCAL_ONLY" if local.exists else "IN_SYNC", local=local)
        return self._compare(local, database)

    def migrate(self, *, confirm_cutover: bool = True, backup: bool = True) -> ReconciliationResult:
        local = self._load_local()
        if local is None:
            return ReconciliationResult("INVALID_LOCAL_STATE", differences=("malformed_local_json",))
        if not local.exists:
            return ReconciliationResult("INVALID_LOCAL_STATE", local=local, differences=("missing_local_state",))
        validation = self._validate_local(local)
        if validation:
            return ReconciliationResult("INVALID_LOCAL_STATE", local=local, differences=tuple(validation))

        backup_path = self._backup_local_state() if backup else None

        def write(session: Session) -> ReconciliationResult:
            database = self._load_database(session)
            if database is not None:
                comparison = self._compare(local, database)
                if comparison.outcome not in {"IN_SYNC", "LOCAL_ONLY"}:
                    return comparison
            self._ensure_project_and_tasks(session)
            self._write_local_to_database(session, local)
            database_after = self._load_database(session)
            if database_after is None:
                raise RuntimeError("database bootstrap state was not created")
            comparison = self._compare(local, database_after)
            if comparison.outcome != "IN_SYNC":
                return comparison
            if confirm_cutover:
                self.repository.activate_postgresql_authority(
                    session,
                    project_id=self.project_id,
                    run_id=self.run_id,
                    source_snapshot_path=str(backup_path) if backup_path else None,
                )
            return ReconciliationResult("IN_SYNC", local=local, database=database_after, backup_path=backup_path)

        return self._with_database(write) or ReconciliationResult(
            "INVALID_DATABASE_STATE",
            local=local,
            differences=("database_unavailable",),
            backup_path=backup_path,
        )

    def reconcile(self) -> ReconciliationResult:
        return self.inspect()

    def confirm_cutover(self, *, backup: bool = True) -> ReconciliationResult:
        return self.migrate(confirm_cutover=True, backup=backup)

    def load_authoritative_state(self) -> dict[str, Any] | None:
        def read(session: Session) -> dict[str, Any] | None:
            authority = self.repository.get_active_authority(session, self.project_id)
            if authority is None:
                return None
            state = self._load_database(session)
            if state is None:
                return None
            return {
                "completed_tasks": list(state.completed_tasks),
                "blocked_tasks": dict(state.blocked_tasks),
                "last_completed_task": state.last_completed_task_id,
                "last_result": f"postgresql authority: {state.last_completed_task_id or state.current_task_id or 'none'}",
                "state_backend": "postgresql",
            }

        return self._with_database(read)

    def mark_completed(self, task_id: str, message: str) -> bool:
        def write(session: Session) -> bool:
            authority = self.repository.get_active_authority(session, self.project_id)
            if authority is None:
                return False
            run = self.repository.require_run(session, authority.run_id)
            self.repository.update_run_state(
                session,
                run.id,
                status="running",
                current_task_id=None,
                last_completed_task_id=task_id,
                last_verified_commit=_git_head(self.root),
            )
            sequence = self.repository.next_checkpoint_sequence(session, run.id)
            self.repository.append_checkpoint(
                session,
                checkpoint_id=f"{run.id}-{task_id}-completed-{sequence}",
                run_id=run.id,
                sequence=sequence,
                checkpoint_kind="task_completed",
                task_id=task_id,
                verified_commit=_git_head(self.root),
                state=json.dumps({"completed_task": task_id, "message": message}, sort_keys=True),
            )
            return True

        return bool(self._with_database(write))

    def mark_blocked(self, task_id: str, reason: str) -> bool:
        def write(session: Session) -> bool:
            authority = self.repository.get_active_authority(session, self.project_id)
            if authority is None:
                return False
            run = self.repository.require_run(session, authority.run_id)
            self.repository.mark_blocked(session, run.id, current_task_id=task_id, reason=reason)
            sequence = self.repository.next_checkpoint_sequence(session, run.id)
            self.repository.append_checkpoint(
                session,
                checkpoint_id=f"{run.id}-{task_id}-blocked-{sequence}",
                run_id=run.id,
                sequence=sequence,
                checkpoint_kind="run_blocked",
                task_id=task_id,
                state=json.dumps({"blocked_task": task_id, "reason": reason}, sort_keys=True),
            )
            return True

        return bool(self._with_database(write))

    def _load_local(self) -> NormalizedBootstrapState | None:
        if not self.state_file.exists():
            return NormalizedBootstrapState(False, project_id=self.project_id, run_id=self.run_id)
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        completed = raw.get("completed_tasks", [])
        blocked = raw.get("blocked_tasks", {})
        if not isinstance(completed, list) or not all(isinstance(item, str) for item in completed):
            return None
        if not isinstance(blocked, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in blocked.items()):
            return None
        last_completed = raw.get("last_completed_task")
        if last_completed is not None and not isinstance(last_completed, str):
            return None
        last_verified_commit = raw.get("last_verified_commit")
        if last_verified_commit is not None and not isinstance(last_verified_commit, str):
            return None
        current_task = raw.get("current_task")
        if current_task is not None and not isinstance(current_task, str):
            return None
        return NormalizedBootstrapState(
            True,
            project_id=self.project_id,
            run_id=str(raw.get("run_id", self.run_id)),
            current_stage=str(raw.get("current_stage", self.initial_stage)),
            current_task_id=current_task,
            last_completed_task_id=last_completed or (completed[-1] if completed else None),
            last_verified_commit=last_verified_commit,
            last_checkpoint_task_id=last_completed or (completed[-1] if completed else None),
            blocked_tasks=dict(blocked),
            completed_tasks=tuple(dict.fromkeys(completed)),
            authority_backend=str(raw.get("state_backend")) if raw.get("state_backend") else "local_json",
        )

    def _load_database(self, session: Session) -> NormalizedBootstrapState | None:
        run = session.get(BootstrapRun, self.run_id)
        if run is None:
            return None
        checkpoints = self.repository.list_checkpoints(session, self.run_id)
        completed = tuple(
            checkpoint.task_id
            for checkpoint in checkpoints
            if checkpoint.checkpoint_kind == "task_completed" and checkpoint.task_id is not None
        )
        blocked: dict[str, str] = {}
        if run.status == "blocked" and run.current_task_id and run.blocked_reason:
            blocked[run.current_task_id] = run.blocked_reason
        latest = self.repository.latest_checkpoint(session, self.run_id)
        authority = self.repository.get_active_authority(session, self.project_id)
        return NormalizedBootstrapState(
            True,
            project_id=run.project_id,
            run_id=run.id,
            current_stage=run.current_stage,
            current_task_id=run.current_task_id,
            last_completed_task_id=run.last_completed_task_id,
            last_verified_commit=run.last_verified_commit,
            last_checkpoint_task_id=latest.task_id if latest else None,
            blocked_tasks=blocked,
            completed_tasks=completed,
            authority_backend=authority.backend if authority else None,
        )

    def _compare(
        self,
        local: NormalizedBootstrapState,
        database: NormalizedBootstrapState | None,
    ) -> ReconciliationResult:
        if local.exists and database is None:
            return ReconciliationResult("LOCAL_ONLY", local=local)
        if not local.exists and database is not None:
            return ReconciliationResult("DATABASE_ONLY", local=local, database=database)
        if database is None:
            return ReconciliationResult("IN_SYNC", local=local)
        differences: list[str] = []
        for field_name in (
            "project_id",
            "run_id",
            "current_stage",
            "current_task_id",
            "last_completed_task_id",
            "last_verified_commit",
            "blocked_tasks",
            "completed_tasks",
        ):
            if getattr(local, field_name) != getattr(database, field_name):
                differences.append(field_name)
        if not differences:
            return ReconciliationResult("IN_SYNC", local=local, database=database)
        local_completed = len(local.completed_tasks)
        database_completed = len(database.completed_tasks)
        if set(differences).issubset({"completed_tasks", "last_completed_task_id", "last_checkpoint_task_id"}):
            if local_completed > database_completed:
                return ReconciliationResult("LOCAL_AHEAD", local=local, database=database, differences=tuple(differences))
            if database_completed > local_completed:
                return ReconciliationResult("DATABASE_AHEAD", local=local, database=database, differences=tuple(differences))
        return ReconciliationResult("CONFLICT", local=local, database=database, differences=tuple(differences))

    def _validate_local(self, local: NormalizedBootstrapState) -> list[str]:
        tasks = load_tasks()
        errors: list[str] = []
        for task_id in [*local.completed_tasks, *local.blocked_tasks.keys()]:
            if task_id not in tasks:
                errors.append(f"unknown_task:{task_id}")
        if local.current_task_id and local.current_task_id not in tasks:
            errors.append(f"unknown_current_task:{local.current_task_id}")
        if local.last_completed_task_id and local.last_completed_task_id not in tasks:
            errors.append(f"unknown_last_completed_task:{local.last_completed_task_id}")
        if local.current_task_id and local.current_task_id == local.last_completed_task_id:
            errors.append("current_task_equals_last_completed")
        if local.last_verified_commit and not self._git_commit_exists(local.last_verified_commit):
            errors.append("invalid_git_commit:last_verified_commit")
        return errors

    def _ensure_project_and_tasks(self, session: Session) -> None:
        projects = ProjectRepository()
        tasks = TaskRepository()
        project = projects.get(session, self.project_id)
        if project is None:
            projects.create(session, project_id=self.project_id, name=self.project_id)
        for task in load_tasks().values():
            existing = tasks.get(session, task.id)
            if existing is None:
                tasks.create(
                    session,
                    task_id=task.id,
                    project_id=self.project_id,
                    title=task.title,
                    objective=task.objective,
                    status="pending",
                    execution_class=task.execution_class,
                    schedulable=task.schedulable,
                    fingerprint=None,
                )

    def _write_local_to_database(self, session: Session, local: NormalizedBootstrapState) -> None:
        run = self.repository.create_run(
            session,
            run_id=self.run_id,
            project_id=self.project_id,
            manifest_ref=self.manifest_ref,
            baseline_commit=local.last_verified_commit,
            status="running",
            current_stage=local.current_stage,
            current_task_id=local.current_task_id,
            started_at=datetime.now(UTC),
        )
        run.last_completed_task_id = local.last_completed_task_id
        run.last_verified_commit = local.last_verified_commit
        if local.blocked_tasks:
            task_id, reason = next(iter(local.blocked_tasks.items()))
            run.status = "blocked"
            run.current_task_id = task_id
            run.blocked_reason = reason
            run.failure_classification = "blocked"
        elif local.last_completed_task_id:
            run.status = "running"
        for index, task_id in enumerate(local.completed_tasks, start=1):
            self.repository.append_checkpoint(
                session,
                checkpoint_id=f"{self.run_id}-{task_id}-completed",
                run_id=self.run_id,
                sequence=index,
                checkpoint_kind="task_completed",
                task_id=task_id,
                verified_commit=local.last_verified_commit if task_id == local.last_completed_task_id else None,
                state=json.dumps({"completed_task": task_id}, sort_keys=True),
            )

    def _backup_local_state(self) -> Path | None:
        if not self.state_file.exists():
            return None
        backup_dir = self.state_file.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"state.{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.json"
        shutil.copy2(self.state_file, backup_path)
        return backup_path

    def _git_commit_exists(self, commit_ref: str) -> bool:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{commit_ref}^{{commit}}"],
            cwd=self.root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def _with_database(self, callback):
        database = self.database
        owns_database = database is None
        try:
            if database is None:
                database = Database(load_database_settings(self.root / ".env"))
            with database.session() as session:
                return callback(session)
        except (DatabaseConfigError, SQLAlchemyError):
            return None
        finally:
            if owns_database and database is not None:
                database.dispose()


def _git_head(root: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
