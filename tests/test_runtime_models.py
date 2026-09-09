from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.persistence.models import (
    Base,
    BootstrapCheckpoint,
    BootstrapRun,
    BootstrapStateAuthority,
    Checkpoint,
    Execution,
    Project,
    RuntimeHumanGate,
    RuntimePlanImport,
    RuntimeTaskPlanBinding,
    Task,
    TaskDependency,
    TaskLease,
)

EXPECTED_TABLES = {
    "projects",
    "tasks",
    "task_dependencies",
    "executions",
    "checkpoints",
    "task_leases",
    "bootstrap_runs",
    "bootstrap_checkpoints",
    "bootstrap_state_authority",
    "runtime_plan_imports",
    "runtime_task_plan_bindings",
    "runtime_human_gates",
}


def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def seed_project(session: Session) -> Project:
    project = Project(id="project-1", name="AI Enterprise")
    session.add(project)
    session.flush()
    return project


def seed_task(session: Session, task_id: str = "TASK-1") -> Task:
    project = seed_project(session)
    task = Task(
        id=task_id,
        project_id=project.id,
        title="Bootstrap task",
        objective="Persist task state",
        fingerprint=f"fingerprint-{task_id}",
    )
    session.add(task)
    session.flush()
    return task


def test_metadata_matches_expected_runtime_tables() -> None:
    assert EXPECTED_TABLES.issubset(Base.metadata.tables)


def test_project_persistence_and_defaults() -> None:
    factory = session_factory()
    with factory() as session:
        project = seed_project(session)
        session.commit()

        persisted = session.get(Project, project.id)

    assert persisted is not None
    assert persisted.status == "active"
    assert persisted.created_at is not None
    assert persisted.updated_at is not None


def test_task_persistence_and_project_relationship() -> None:
    factory = session_factory()
    with factory() as session:
        task = seed_task(session)
        session.commit()
        persisted = session.scalars(select(Task).where(Task.id == task.id)).one()
        assert persisted.project.name == "AI Enterprise"
        assert persisted.status == "pending"
        assert persisted.execution_class == "implementation"
        assert persisted.schedulable is True


def test_runtime_plan_import_models_preserve_plan_metadata() -> None:
    factory = session_factory()
    with factory() as session:
        project = seed_project(session)
        task = Task(id="IMPL-A", project_id=project.id, title="A", fingerprint="fp-a")
        session.add(task)
        session.flush()
        receipt = RuntimePlanImport(
            id="rhi-plan-a-v1",
            project_id=project.id,
            plan_project_id="PRJ-A",
            plan_id="PLAN-A",
            plan_version="1",
            status="imported",
            imported_at=datetime.now(UTC),
            compiled_project_hash="a" * 64,
            capability_resolution_hash="b" * 64,
            trace_validation_hash="c" * 64,
            implementation_plan_hash="d" * 64,
            feasibility_hash="e" * 64,
            dry_run_hash="f" * 64,
            task_fingerprint_hash="1" * 64,
            dependency_graph_hash="2" * 64,
            importer_version="rhi-001.test",
            task_count=1,
            dependency_count=0,
            human_gate_count=1,
            effective_concurrency=1,
        )
        session.add(receipt)
        session.add(
            RuntimeTaskPlanBinding(
                task_id=task.id,
                import_id=receipt.id,
                plan_id=receipt.plan_id,
                plan_version=receipt.plan_version,
                fingerprint="fp-a",
                risk_level="HIGH",
                agent_role="AGT-001",
                model_profile="CODING_STANDARD",
                executor="codex",
                verification_profile="STANDARD_REGRESSION",
                feasibility_status="HUMAN_APPROVAL_REQUIRED",
                policy_decision="HUMAN_APPROVAL_REQUIRED",
                implements_json="{}",
                write_scope_json="{}",
                acceptance_json="[]",
            )
        )
        session.add(
            RuntimeHumanGate(
                id="GATE-A",
                import_id=receipt.id,
                task_id=task.id,
                plan_id=receipt.plan_id,
                plan_version=receipt.plan_version,
                status="pending",
                reason="approval required",
                risk_level="HIGH",
                approval_boundary="execution",
                expected_evidence_json="[]",
                downstream_task_ids_json="[]",
                resume_semantics="resume",
            )
        )
        session.commit()

        persisted = session.get(RuntimePlanImport, receipt.id)

        assert persisted is not None
        assert persisted.project_id == "project-1"
        assert persisted.task_bindings[0].task_id == "IMPL-A"
        assert persisted.human_gates[0].status == "pending"


def test_task_dependency_persistence() -> None:
    factory = session_factory()
    with factory() as session:
        project = seed_project(session)
        first = Task(id="TASK-1", project_id=project.id, title="first")
        second = Task(id="TASK-2", project_id=project.id, title="second")
        session.add_all([first, second])
        session.flush()
        session.add(TaskDependency(task_id=second.id, depends_on_task_id=first.id))
        session.commit()

        persisted = session.get(Task, second.id)
        assert persisted is not None
        assert persisted.dependencies[0].depends_on_task_id == "TASK-1"


def test_duplicate_dependency_rejected() -> None:
    factory = session_factory()
    with factory() as session:
        project = seed_project(session)
        first = Task(id="TASK-1", project_id=project.id, title="first")
        second = Task(id="TASK-2", project_id=project.id, title="second")
        session.add_all([first, second])
        session.flush()
        session.add_all(
            [
                TaskDependency(task_id=second.id, depends_on_task_id=first.id),
                TaskDependency(task_id=second.id, depends_on_task_id=first.id),
            ]
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
        else:
            raise AssertionError("duplicate dependency was not rejected")


def test_self_dependency_rejected() -> None:
    factory = session_factory()
    with factory() as session:
        task = seed_task(session)
        session.add(TaskDependency(task_id=task.id, depends_on_task_id=task.id))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
        else:
            raise AssertionError("self dependency was not rejected")


def test_execution_relationship() -> None:
    factory = session_factory()
    with factory() as session:
        task = seed_task(session)
        execution = Execution(
            id="execution-1",
            task_id=task.id,
            executor_type="fake",
            status="succeeded",
            attempt=1,
            candidate_tree_hash="a" * 40,
        )
        session.add(execution)
        session.commit()

        persisted = session.get(Execution, execution.id)
        assert persisted is not None
        assert persisted.task.id == "TASK-1"
        assert persisted.attempt == 1


def test_task_lease_relationship() -> None:
    factory = session_factory()
    with factory() as session:
        task = seed_task(session)
        now = datetime.now(UTC)
        execution = Execution(id="execution-1", task_id=task.id, executor_type="fake")
        lease = TaskLease(
            id="lease-1",
            task_id=task.id,
            execution_id=execution.id,
            owner_id="worker-1",
            acquired_at=now,
            renewed_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        session.add_all([execution, lease])
        session.commit()

        persisted = session.get(TaskLease, lease.id)
        assert persisted is not None
        assert persisted.task.id == "TASK-1"
        assert persisted.execution.id == "execution-1"
        assert persisted.status == "active"


def test_bootstrap_run_and_checkpoint_relationship() -> None:
    factory = session_factory()
    with factory() as session:
        task = seed_task(session)
        run = BootstrapRun(
            id="run-1",
            project_id=task.project_id,
            status="running",
            manifest_ref="manifest/bootstrap",
            baseline_commit="abc123",
            current_stage="B04",
            current_task_id=task.id,
        )
        checkpoint = BootstrapCheckpoint(
            id="bootstrap-checkpoint-1",
            run_id=run.id,
            sequence=1,
            checkpoint_kind="task_completed",
            task_id=task.id,
            verified_commit="def456",
            state='{"completed":["TASK-1"]}',
        )
        session.add_all([run, checkpoint])
        authority = BootstrapStateAuthority(
            id="project-1",
            project_id=task.project_id,
            run_id=run.id,
            backend="postgresql",
            status="active",
        )
        session.add(authority)
        session.commit()

        persisted = session.get(BootstrapRun, run.id)
        assert persisted is not None
        assert persisted.project.id == task.project_id
        assert persisted.current_task is not None
        assert persisted.current_task.id == task.id
        assert persisted.bootstrap_checkpoints[0].verified_commit == "def456"
        assert session.get(BootstrapStateAuthority, "project-1").run_id == run.id  # type: ignore[union-attr]


def test_checkpoint_relationship() -> None:
    factory = session_factory()
    with factory() as session:
        task = seed_task(session)
        execution = Execution(id="execution-1", task_id=task.id, executor_type="fake")
        checkpoint = Checkpoint(
            id="checkpoint-1",
            task_id=task.id,
            execution_id=execution.id,
            checkpoint_type="execution",
            state='{"status":"complete"}',
            commit_hash="b" * 40,
            tree_hash="c" * 40,
        )
        session.add_all([execution, checkpoint])
        session.commit()

        persisted = session.get(Checkpoint, checkpoint.id)
        assert persisted is not None
        assert persisted.task.id == "TASK-1"
        assert persisted.execution is not None
        assert persisted.execution.id == "execution-1"


def test_foreign_key_integrity() -> None:
    factory = session_factory()
    with factory() as session:
        session.add(Task(id="TASK-MISSING", project_id="missing", title="missing project"))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
        else:
            raise AssertionError("foreign-key violation was not rejected")


def test_required_field_constraints() -> None:
    factory = session_factory()
    with factory() as session:
        session.add(Project(id="bad-project", name=None))  # type: ignore[arg-type]
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
        else:
            raise AssertionError("required-field violation was not rejected")


def test_invalid_status_rejected() -> None:
    factory = session_factory()
    with factory() as session:
        session.add(Project(id="bad-project", name="Bad", status="invalid"))  # type: ignore[arg-type]
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
        else:
            raise AssertionError("invalid status was not rejected")
