from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.bootstrap.models import BootstrapTask
from ai_ent.bootstrap.state_reconciliation import BootstrapStateReconciler
from ai_ent.persistence.database import Database
from ai_ent.persistence.models import Base


def session_factory() -> tuple[Database, sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    database = Database.__new__(Database)
    database.engine = engine
    database.session_factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    return database, database.session_factory


def write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_local_only_valid_state_migrates_and_cutover_is_idempotent(tmp_path: Path) -> None:
    database, _ = session_factory()
    state_file = tmp_path / ".bootstrap" / "state.json"
    write_state(
        state_file,
        {
            "completed_tasks": ["TASK-0001", "TASK-0002"],
            "blocked_tasks": {},
            "last_completed_task": "TASK-0002",
        },
    )
    reconciler = BootstrapStateReconciler(state_file=state_file, database=database)

    assert reconciler.inspect().outcome == "LOCAL_ONLY"
    migrated = reconciler.migrate()
    repeated = reconciler.migrate()

    assert migrated.outcome == "IN_SYNC"
    assert migrated.backup_path is not None
    assert migrated.backup_path.exists()
    assert repeated.outcome == "IN_SYNC"
    authoritative = reconciler.load_authoritative_state()
    assert authoritative is not None
    assert authoritative["state_backend"] == "postgresql"
    assert authoritative["completed_tasks"] == ["TASK-0001", "TASK-0002"]


def test_database_only_state_is_preserved(tmp_path: Path) -> None:
    database, _ = session_factory()
    state_file = tmp_path / ".bootstrap" / "missing.json"
    reconciler = BootstrapStateReconciler(state_file=state_file, database=database)
    write_state(
        tmp_path / "source.json",
        {"completed_tasks": ["TASK-0001"], "blocked_tasks": {}, "last_completed_task": "TASK-0001"},
    )
    BootstrapStateReconciler(state_file=tmp_path / "source.json", database=database).migrate()

    result = reconciler.inspect()

    assert result.outcome == "DATABASE_ONLY"
    assert result.database is not None
    assert result.database.completed_tasks == ("TASK-0001",)


def test_post_cutover_legacy_json_drift_is_database_ahead(tmp_path: Path) -> None:
    database, _ = session_factory()
    initial = tmp_path / "initial.json"
    write_state(initial, {"completed_tasks": ["TASK-0001"], "blocked_tasks": {}, "last_completed_task": "TASK-0001"})
    BootstrapStateReconciler(state_file=initial, database=database).migrate()

    drifted = tmp_path / "drifted.json"
    write_state(
        drifted,
        {
            "completed_tasks": ["TASK-0001"],
            "blocked_tasks": {},
            "last_completed_task": "TASK-0001",
            "state_backend": "postgresql",
            "last_verified_commit": "different",
        },
    )

    assert BootstrapStateReconciler(state_file=drifted, database=database).inspect().outcome == "DATABASE_AHEAD"


def test_local_ahead_database_ahead_and_conflict_are_reported(tmp_path: Path) -> None:
    database, _ = session_factory()
    db_state = tmp_path / "db.json"
    write_state(db_state, {"completed_tasks": ["TASK-0001"], "blocked_tasks": {}, "last_completed_task": "TASK-0001"})
    BootstrapStateReconciler(state_file=db_state, database=database).migrate()

    local_ahead = tmp_path / "local-ahead.json"
    write_state(
        local_ahead,
        {
            "completed_tasks": ["TASK-0001", "TASK-0002"],
            "blocked_tasks": {},
            "last_completed_task": "TASK-0002",
        },
    )
    assert BootstrapStateReconciler(state_file=local_ahead, database=database).inspect().outcome == "LOCAL_AHEAD"

    database_ahead = tmp_path / "database-ahead.json"
    write_state(database_ahead, {"completed_tasks": [], "blocked_tasks": {}})
    assert BootstrapStateReconciler(state_file=database_ahead, database=database).inspect().outcome == "DATABASE_AHEAD"

    conflict = tmp_path / "conflict.json"
    write_state(
        conflict,
        {
            "completed_tasks": ["TASK-0001"],
            "blocked_tasks": {"TASK-0002": "blocked"},
            "last_completed_task": "TASK-0001",
        },
    )
    assert BootstrapStateReconciler(state_file=conflict, database=database).inspect().outcome == "CONFLICT"


def test_invalid_local_state_and_unknown_references_are_rejected(tmp_path: Path) -> None:
    database, _ = session_factory()
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{bad", encoding="utf-8")
    assert BootstrapStateReconciler(state_file=malformed, database=database).inspect().outcome == "INVALID_LOCAL_STATE"

    unknown = tmp_path / "unknown.json"
    write_state(unknown, {"completed_tasks": ["TASK-MISSING"], "blocked_tasks": {}})
    result = BootstrapStateReconciler(state_file=unknown, database=database).migrate()
    assert result.outcome == "INVALID_LOCAL_STATE"
    assert "unknown_task:TASK-MISSING" in result.differences
    assert "unknown_last_completed_task:TASK-MISSING" in result.differences

    current_equals_completed = tmp_path / "bad-current.json"
    write_state(
        current_equals_completed,
        {"completed_tasks": ["TASK-0001"], "blocked_tasks": {}, "current_task": "TASK-0001"},
    )
    result = BootstrapStateReconciler(state_file=current_equals_completed, database=database).migrate()
    assert result.outcome == "INVALID_LOCAL_STATE"
    assert "current_task_equals_last_completed" in result.differences


def test_invalid_migration_does_not_activate_authority(tmp_path: Path) -> None:
    database, _ = session_factory()
    state_file = tmp_path / "state.json"
    write_state(
        state_file,
        {
            "completed_tasks": ["TASK-0001"],
            "blocked_tasks": {},
            "last_completed_task": "TASK-0001",
            "last_verified_commit": "definitely-missing-ref",
        },
    )
    reconciler = BootstrapStateReconciler(state_file=state_file, database=database)

    result = reconciler.migrate()

    assert result.outcome == "INVALID_LOCAL_STATE"
    assert result.differences == ("invalid_git_commit:last_verified_commit",)
    assert reconciler.load_authoritative_state() is None


def test_bootstrap_state_loader_reads_database_after_cutover(monkeypatch, tmp_path: Path) -> None:
    from ai_ent.bootstrap import state as bootstrap_state
    from ai_ent.bootstrap import state_reconciliation

    class FakeReconciler:
        def load_authoritative_state(self) -> dict[str, object]:
            return {
                "completed_tasks": ["TASK-0001"],
                "blocked_tasks": {},
                "last_completed_task": "TASK-0001",
                "state_backend": "postgresql",
            }

    monkeypatch.setattr(state_reconciliation, "BootstrapStateReconciler", FakeReconciler)
    monkeypatch.setattr(bootstrap_state, "STATE_FILE", tmp_path / "missing.json")

    loaded = bootstrap_state.load_state()

    assert loaded["state_backend"] == "postgresql"
    assert loaded["completed_tasks"] == ["TASK-0001"]


def test_bootstrap_state_loader_preserves_pre_db_local_fallback(monkeypatch, tmp_path: Path) -> None:
    from ai_ent.bootstrap import state as bootstrap_state
    from ai_ent.bootstrap import state_reconciliation

    class FakeReconciler:
        def load_authoritative_state(self) -> None:
            return None

        def mark_completed(self, task_id: str, message: str) -> bool:
            return False

    state_file = tmp_path / ".bootstrap" / "state.json"
    write_state(state_file, {"completed_tasks": ["TASK-0001"], "blocked_tasks": {}})
    monkeypatch.setattr(state_reconciliation, "BootstrapStateReconciler", FakeReconciler)
    monkeypatch.setattr(bootstrap_state, "STATE_FILE", state_file)

    assert bootstrap_state.load_state()["completed_tasks"] == ["TASK-0001"]
    bootstrap_state.mark_completed("TASK-0002", "done")

    loaded = json.loads(state_file.read_text(encoding="utf-8"))
    assert loaded["completed_tasks"] == ["TASK-0001", "TASK-0002"]


def test_mark_completed_syncs_new_manifest_task_after_cutover(monkeypatch, tmp_path: Path) -> None:
    from ai_ent.bootstrap import state_reconciliation

    database, _ = session_factory()
    state_file = tmp_path / ".bootstrap" / "state.json"
    write_state(state_file, {"completed_tasks": ["TASK-0001"], "blocked_tasks": {}, "last_completed_task": "TASK-0001"})
    reconciler = BootstrapStateReconciler(state_file=state_file, database=database)
    assert reconciler.migrate().outcome == "IN_SYNC"

    original_tasks = state_reconciliation.load_tasks()
    monkeypatch.setattr(
        state_reconciliation,
        "load_tasks",
        lambda: {
            **original_tasks,
            "TASK-NEW": BootstrapTask(id="TASK-NEW", stage="T", title="New Task", executor="fake"),
        },
    )

    assert reconciler.mark_completed("TASK-NEW", "done")
    authoritative = reconciler.load_authoritative_state()
    assert authoritative is not None
    assert authoritative["last_completed_task"] == "TASK-NEW"
    assert "TASK-NEW" in authoritative["completed_tasks"]
