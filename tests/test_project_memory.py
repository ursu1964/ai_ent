from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.persistence.models import (
    Base,
    Checkpoint,
    Execution,
    Project,
    RuntimeHumanGate,
    RuntimePlanImport,
    RuntimeTaskPlanBinding,
    Task,
)
from ai_ent.project_memory import ProjectMemoryError, ProjectMemoryService


def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def seed_runtime_project(session: Session) -> None:
    session.add(Project(id="project-1", name="Project One"))
    session.add(
        Task(
            id="IMPL-C19-CMP-011",
            project_id="project-1",
            title="Implement Project Memory",
            objective="Persist project memory.",
            fingerprint="f" * 64,
        )
    )
    session.add(
        Execution(
            id="execution-1",
            task_id="IMPL-C19-CMP-011",
            executor_type="codex",
            status="succeeded",
            attempt=1,
            terminal_state="success",
        )
    )
    session.flush()


def seed_runtime_import(session: Session) -> None:
    session.add(
        RuntimePlanImport(
            id="rhi-plan-v1",
            project_id="project-1",
            plan_project_id="PRJ-AI-ENT",
            plan_id="PLAN-A",
            plan_version="1",
            status="imported",
            imported_at=datetime(2026, 9, 10, tzinfo=UTC),
            compiled_project_hash="a" * 64,
            capability_resolution_hash="b" * 64,
            trace_validation_hash="c" * 64,
            implementation_plan_hash="d" * 64,
            feasibility_hash="e" * 64,
            dry_run_hash="f" * 64,
            task_fingerprint_hash="1" * 64,
            dependency_graph_hash="2" * 64,
            importer_version="rhi-test",
            task_count=1,
            dependency_count=0,
            human_gate_count=1,
            effective_concurrency=1,
        )
    )
    session.add(
        RuntimeTaskPlanBinding(
            task_id="IMPL-C19-CMP-011",
            import_id="rhi-plan-v1",
            plan_id="PLAN-A",
            plan_version="1",
            fingerprint="f" * 64,
            risk_level="MEDIUM",
            agent_role="AGT-001",
            model_profile="CODING_STANDARD",
            executor="codex",
            verification_profile="STANDARD_REGRESSION",
            feasibility_status="READY",
            policy_decision="ALLOW",
            implements_json='{"requirements":["FR-006"],"capabilities":["C19"]}',
            write_scope_json='{"allowed":["src/ai_ent/**","tests/**"]}',
            acceptance_json='["FR-006 is deterministic"]',
        )
    )
    session.add(
        RuntimeHumanGate(
            id="GATE-C19",
            import_id="rhi-plan-v1",
            task_id="IMPL-C19-CMP-011",
            plan_id="PLAN-A",
            plan_version="1",
            status="approved",
            reason="operator accepted C19 execution boundary",
            risk_level="MEDIUM",
            approval_boundary="execution",
            expected_evidence_json='["pytest","ruff","pyright"]',
            downstream_task_ids_json="[]",
            resume_semantics="resume scheduler",
        )
    )
    session.flush()


def test_fr_006_records_context_decisions_and_execution_evidence() -> None:
    factory = session_factory()
    service = ProjectMemoryService()

    with factory() as session:
        seed_runtime_project(session)
        context = service.remember_context(
            session,
            project_id="project-1",
            task_id="IMPL-C19-CMP-011",
            summary="C19 runtime context imported",
            context={"requirement": "FR-006", "interfaces": ["IF-004", "IF-005"]},
        )
        decision = service.remember_decision(
            session,
            project_id="project-1",
            task_id="IMPL-C19-CMP-011",
            decision="Use runtime checkpoints for durable project memory",
            rationale="The existing persistence model already owns task and execution evidence.",
            decided_by="ProjectMemoryService",
        )
        evidence = service.remember_execution_evidence(
            session,
            project_id="project-1",
            task_id="IMPL-C19-CMP-011",
            execution_id="execution-1",
            summary="Verification passed",
            evidence={"commands": ["pytest", "ruff", "pyright"], "status": "passed"},
            commit_hash="a" * 40,
            tree_hash="b" * 40,
        )
        duplicate_context = service.remember_context(
            session,
            project_id="project-1",
            task_id="IMPL-C19-CMP-011",
            summary="C19 runtime context imported",
            context={"requirement": "FR-006", "interfaces": ["IF-004", "IF-005"]},
        )
        session.commit()

        records = service.list_records(session, project_id="project-1")
        persisted = session.scalars(select(Checkpoint).where(Checkpoint.checkpoint_type == "runtime")).all()

    assert duplicate_context.memory_id == context.memory_id
    assert {record.kind for record in records} == {"context", "decision", "execution_evidence"}
    assert len(records) == 3
    assert len(persisted) == 3
    assert context.payload["context"]["requirement"] == "FR-006"
    assert decision.payload["decision"] == "Use runtime checkpoints for durable project memory"
    assert evidence.execution_id == "execution-1"
    assert evidence.commit_hash == "a" * 40


def test_fr_006_snapshot_rehydrates_runtime_context_decisions_and_evidence() -> None:
    factory = session_factory()
    service = ProjectMemoryService()

    with factory() as session:
        seed_runtime_project(session)
        seed_runtime_import(session)
        session.add(
            Checkpoint(
                id="checkpoint-1",
                task_id="IMPL-C19-CMP-011",
                execution_id="execution-1",
                checkpoint_type="execution",
                state='{"status":"completed","success_kind":"NEW_VERIFIED_CHANGE"}',
                commit_hash="c" * 40,
                tree_hash="d" * 40,
            )
        )
        service.remember_context(
            session,
            project_id="project-1",
            task_id="IMPL-C19-CMP-011",
            summary="Operator context captured",
            context={"notes": ["project memory remains behind service boundary"]},
        )
        session.commit()

        first = service.snapshot(session, project_id="project-1")
        second = service.snapshot(session, project_id="project-1")

    entries = first.entries
    sources = {entry.source for entry in entries}
    assert first.memory_hash == second.memory_hash
    assert first.as_dict()["summary"] == {
        "entry_count": 5,
        "context_count": 3,
        "decision_count": 1,
        "execution_evidence_count": 1,
    }
    assert {
        "runtime-plan-import",
        "runtime-task-binding",
        "runtime-human-gate",
        "runtime-context",
        "execution-checkpoint",
    }.issubset(sources)
    assert any(entry.payload.get("implements", {}).get("requirements") == ["FR-006"] for entry in entries)


def test_project_memory_rejects_cross_boundary_execution_evidence() -> None:
    factory = session_factory()
    service = ProjectMemoryService()

    with factory() as session:
        seed_runtime_project(session)
        session.add(Project(id="project-2", name="Project Two"))
        session.add(Task(id="TASK-2", project_id="project-2", title="Other task"))
        session.add(Execution(id="execution-2", task_id="TASK-2", executor_type="codex"))
        session.flush()

        try:
            service.remember_execution_evidence(
                session,
                project_id="project-1",
                task_id="IMPL-C19-CMP-011",
                execution_id="execution-2",
                summary="invalid evidence",
                evidence={"status": "wrong-task"},
            )
        except ProjectMemoryError as exc:
            assert "execution is not part of task" in str(exc)
        else:
            raise AssertionError("cross-boundary execution evidence was not rejected")
