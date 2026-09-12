from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.persistence.models import (
    Base,
    BootstrapCheckpoint,
    RuntimeHumanGate,
    RuntimePlanImport,
    RuntimeTaskPlanBinding,
    TaskLease,
)
from ai_ent.persistence.repositories import (
    BootstrapRunRepository,
    ExecutionRepository,
    LeaseRepository,
    ProjectRepository,
    TaskRepository,
)
from ai_ent.runtime_operations import (
    MigrationSafetyChecker,
    RetentionPolicy,
    RuntimeCleanupService,
    build_backup_guidance,
    mandatory_verification_commands,
)


def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def seed_task(session: Session) -> None:
    ProjectRepository().create(session, project_id="project-1", name="Project One")
    TaskRepository().create(session, task_id="PRD-TASK-001", project_id="project-1", title="Task One")


def seed_execution_and_lease(
    session: Session,
    *,
    lease_id: str,
    status: str,
    now: datetime,
) -> None:
    execution = ExecutionRepository().create(
        session,
        execution_id=f"execution-{lease_id}",
        task_id="PRD-TASK-001",
        executor_type="codex",
        status="succeeded",
        finished_at=now - timedelta(days=30),
    )
    lease = LeaseRepository().create(
        session,
        lease_id=lease_id,
        task_id="PRD-TASK-001",
        execution_id=execution.id,
        owner_id="worker-1",
        status=status,  # type: ignore[arg-type]
        acquired_at=now - timedelta(days=30),
        expires_at=now - timedelta(days=29),
    )
    lease.updated_at = now - timedelta(days=30)


def test_cleanup_plans_stale_leases_and_requires_explicit_gate() -> None:
    factory = session_factory()
    now = datetime(2026, 9, 12, 12, tzinfo=UTC)
    with factory() as session:
        seed_task(session)
        seed_execution_and_lease(session, lease_id="lease-stale", status="active", now=now)

        service = RuntimeCleanupService()
        planned = service.plan(session, now=now)
        blocked = service.apply(session, now=now)

        assert planned.status == "planned"
        assert planned.dry_run
        assert planned.requires_human_gate
        assert [action.kind for action in planned.actions] == ["expire_lease"]
        assert blocked.status == "blocked"
        assert blocked.blocked_reason == "explicit_cleanup_human_gate_required"
        assert session.get(TaskLease, "lease-stale").status == "active"  # type: ignore[union-attr]


def test_cleanup_apply_with_gate_expires_stale_lease_without_deleting_authority_or_gates() -> None:
    factory = session_factory()
    now = datetime(2026, 9, 12, 12, tzinfo=UTC)
    with factory() as session:
        seed_task(session)
        seed_execution_and_lease(session, lease_id="lease-stale", status="active", now=now)
        bootstrap = BootstrapRunRepository()
        bootstrap.create_run(
            session,
            run_id="run-1",
            project_id="project-1",
            manifest_ref="manifest/project/ai-ent",
        )
        bootstrap.activate_postgresql_authority(session, project_id="project-1", run_id="run-1")
        session.add(
            RuntimePlanImport(
                id="import-1",
                project_id="project-1",
                plan_project_id="project-1",
                plan_id="plan-1",
                plan_version="1",
                status="imported",
                imported_at=now,
                compiled_project_hash="a" * 64,
                capability_resolution_hash="b" * 64,
                trace_validation_hash="c" * 64,
                implementation_plan_hash="d" * 64,
                feasibility_hash="e" * 64,
                dry_run_hash="f" * 64,
                task_fingerprint_hash="1" * 64,
                dependency_graph_hash="2" * 64,
                importer_version="test",
                task_count=1,
                dependency_count=0,
                human_gate_count=1,
                effective_concurrency=1,
            )
        )
        session.add(
            RuntimeHumanGate(
                id="gate-1",
                import_id="import-1",
                task_id="PRD-TASK-001",
                plan_id="plan-1",
                plan_version="1",
                status="pending",
                reason="high risk",
                risk_level="high",
                approval_boundary="operator",
                expected_evidence_json="[]",
                downstream_task_ids_json="[]",
                resume_semantics="resume after approval",
            )
        )
        session.flush()

        applied = RuntimeCleanupService().apply(
            session,
            now=now,
            human_gate_confirmed=True,
        )

        assert applied.status == "applied"
        assert applied.applied_count == 1
        assert session.get(TaskLease, "lease-stale").status == "expired"  # type: ignore[union-attr]
        assert bootstrap.get_active_authority(session, "project-1") is not None
        assert session.get(RuntimeHumanGate, "gate-1") is not None


def test_cleanup_retains_latest_state_snapshot_per_run() -> None:
    factory = session_factory()
    now = datetime(2026, 9, 12, 12, tzinfo=UTC)
    with factory() as session:
        seed_task(session)
        bootstrap = BootstrapRunRepository()
        bootstrap.create_run(
            session,
            run_id="run-1",
            project_id="project-1",
            manifest_ref="manifest/project/ai-ent",
        )
        for sequence, checkpoint_id in enumerate(("snapshot-old", "snapshot-new"), start=1):
            checkpoint = bootstrap.append_checkpoint(
                session,
                checkpoint_id=checkpoint_id,
                run_id="run-1",
                checkpoint_kind="state_snapshot",
                state="{}",
                sequence=sequence,
            )
            checkpoint.created_at = now - timedelta(days=30 - sequence)
        durable = bootstrap.append_checkpoint(
            session,
            checkpoint_id="task-completed",
            run_id="run-1",
            checkpoint_kind="task_completed",
            state="{}",
            task_id="PRD-TASK-001",
            sequence=3,
        )
        durable.created_at = now - timedelta(days=30)
        session.flush()

        policy = RetentionPolicy(state_snapshot_retention=timedelta(days=7))
        applied = RuntimeCleanupService(policy).apply(
            session,
            now=now,
            human_gate_confirmed=True,
        )

        assert [action.target_id for action in applied.actions] == ["snapshot-old"]
        assert session.get(BootstrapCheckpoint, "snapshot-old") is None
        assert session.get(BootstrapCheckpoint, "snapshot-new") is not None
        assert session.get(BootstrapCheckpoint, "task-completed") is not None


def test_cleanup_skips_active_authority_state_snapshots() -> None:
    factory = session_factory()
    now = datetime(2026, 9, 12, 12, tzinfo=UTC)
    with factory() as session:
        seed_task(session)
        bootstrap = BootstrapRunRepository()
        bootstrap.create_run(
            session,
            run_id="run-active",
            project_id="project-1",
            manifest_ref="manifest/project/ai-ent",
        )
        bootstrap.activate_postgresql_authority(
            session,
            project_id="project-1",
            run_id="run-active",
        )
        checkpoint = bootstrap.append_checkpoint(
            session,
            checkpoint_id="snapshot-active-old",
            run_id="run-active",
            checkpoint_kind="state_snapshot",
            state="{}",
            sequence=1,
        )
        checkpoint.created_at = now - timedelta(days=30)
        session.flush()

        policy = RetentionPolicy(state_snapshot_retention=timedelta(days=7))
        applied = RuntimeCleanupService(policy).apply(
            session,
            now=now,
            human_gate_confirmed=True,
        )

        assert applied.actions == ()
        assert session.get(BootstrapCheckpoint, "snapshot-active-old") is not None


def test_migration_checks_preserve_authority_and_verification_profiles() -> None:
    factory = session_factory()
    now = datetime(2026, 9, 12, 12, tzinfo=UTC)
    with factory() as session:
        seed_task(session)
        bootstrap = BootstrapRunRepository()
        bootstrap.create_run(
            session,
            run_id="run-1",
            project_id="project-1",
            manifest_ref="manifest/project/ai-ent",
        )
        bootstrap.activate_postgresql_authority(session, project_id="project-1", run_id="run-1")
        session.add(
            RuntimePlanImport(
                id="import-1",
                project_id="project-1",
                plan_project_id="project-1",
                plan_id="plan-1",
                plan_version="1",
                status="imported",
                imported_at=now,
                compiled_project_hash="a" * 64,
                capability_resolution_hash="b" * 64,
                trace_validation_hash="c" * 64,
                implementation_plan_hash="d" * 64,
                feasibility_hash="e" * 64,
                dry_run_hash="f" * 64,
                task_fingerprint_hash="1" * 64,
                dependency_graph_hash="2" * 64,
                importer_version="test",
                task_count=1,
                dependency_count=0,
                human_gate_count=0,
                effective_concurrency=1,
            )
        )
        session.add(
            RuntimeTaskPlanBinding(
                task_id="PRD-TASK-001",
                import_id="import-1",
                plan_id="plan-1",
                plan_version="1",
                fingerprint="fingerprint",
                risk_level="medium",
                agent_role="implementer",
                model_profile="codex",
                executor="codex",
                verification_profile="",
                feasibility_status="feasible",
                policy_decision="allowed",
                implements_json="[]",
                write_scope_json="[]",
                acceptance_json="[]",
            )
        )
        session.flush()

        result = MigrationSafetyChecker().check(session, project_id="project-1")

        assert not result.ok
        assert "control_plane_authority" not in [failure.name for failure in result.failures]
        assert [failure.name for failure in result.failures] == ["independent_verification"]
        assert "PRD-TASK-001" in result.failures[0].detail


def test_migration_check_blocks_missing_postgresql_authority() -> None:
    factory = session_factory()
    with factory() as session:
        seed_task(session)

        result = MigrationSafetyChecker().check(session, project_id="project-1")

        assert not result.ok
        assert "control_plane_authority" in [failure.name for failure in result.failures]


def test_backup_guidance_and_verification_commands_do_not_render_secret_values() -> None:
    guidance = build_backup_guidance()
    rendered = "\n".join(guidance.as_lines())

    assert "AIENT_DB_PASSWORD" in rendered
    assert "secret-password" not in rendered
    assert "CHANGE_ME" not in rendered
    assert mandatory_verification_commands() == (
        "/home/user/projects/ai_ent/aient/bin/python -m pytest -q",
        "/home/user/projects/ai_ent/aient/bin/python -m ruff check .",
        "/home/user/projects/ai_ent/aient/bin/python -m pyright",
    )
