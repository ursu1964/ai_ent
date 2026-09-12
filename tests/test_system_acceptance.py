from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.persistence.models import Base, Execution, Project, RuntimePlanImport, Task
from ai_ent.system_acceptance import (
    SAAG_PROJECT_ID,
    evaluate_system_acceptance,
    write_system_acceptance,
)
from tests.test_post_implementation import (
    import_residual_plan,
    mark_residual_tasks_passed,
    write_rcg_artifact,
)
from tests.test_residual_runtime_handoff import seed_prior_plan_complete


def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def seed_complete_runtime(session: Session, tmp_path: Path, monkeypatch: Any) -> str:
    seed_prior_plan_complete(session)
    residual_plan_id = import_residual_plan(session, tmp_path, monkeypatch)
    assert residual_plan_id
    mark_residual_tasks_passed(session)
    write_rcg_artifact(tmp_path)
    session.flush()
    return residual_plan_id


def test_saag_accepts_complete_system_with_all_proofs(tmp_path: Path, monkeypatch: Any) -> None:
    factory = session_factory()
    with factory() as session:
        residual_plan_id = seed_complete_runtime(session, tmp_path, monkeypatch)

        result = evaluate_system_acceptance(
            session,
            artifacts_dir=tmp_path / "artifacts",
            residual_plan_id=residual_plan_id,
        )

        assert result.result == "ACCEPTED_WITH_LIMITATIONS"
        assert result.recommendation == "READY_FOR_FIRST_REAL_APPLICATION_CREATION_PROOF"
        assert len(result.proofs) == 13
        assert len(result.negative_tests) == 6
        assert all(proof.ok for proof in result.proofs)
        assert all(proof.ok for proof in result.negative_tests)
        assert (
            result.requirement_coverage["covered_requirements"]
            == result.requirement_coverage["normative_requirements"]
        )
        assert result.pir_002["residual_gap_count"] == 0
        assert session.scalar(select(func.count()).select_from(Project).where(Project.id == SAAG_PROJECT_ID)) == 0


def test_saag_rejects_without_rcg_acceptance(tmp_path: Path, monkeypatch: Any) -> None:
    factory = session_factory()
    with factory() as session:
        seed_prior_plan_complete(session)
        import_residual_plan(session, tmp_path, monkeypatch)
        mark_residual_tasks_passed(session)

        result = evaluate_system_acceptance(session, artifacts_dir=tmp_path / "artifacts")

        assert result.result == "REJECTED"
        assert result.recommendation == "REMEDIATION_REQUIRED"


def test_saag_writer_outputs_deterministic_artifacts(tmp_path: Path, monkeypatch: Any) -> None:
    factory = session_factory()
    with factory() as session:
        residual_plan_id = seed_complete_runtime(session, tmp_path, monkeypatch)

        first = write_system_acceptance(
            session,
            artifacts_dir=tmp_path / "artifacts",
            residual_plan_id=residual_plan_id,
        )
        first_bytes = (tmp_path / "artifacts" / "saag-001" / "SAAG-001.json").read_bytes()
        second = write_system_acceptance(
            session,
            artifacts_dir=tmp_path / "artifacts",
            residual_plan_id=residual_plan_id,
        )

        assert first.acceptance_hash == second.acceptance_hash
        assert (tmp_path / "artifacts" / "saag-001" / "SAAG-001.json").read_bytes() == first_bytes
        assert session.scalar(select(func.count()).select_from(Task).where(Task.id.like("SAAG-001%"))) == 0
        assert session.scalar(select(func.count()).select_from(Execution).where(Execution.id.like("SAAG-001%"))) == 0
        assert session.scalar(select(func.count()).select_from(RuntimePlanImport).where(RuntimePlanImport.id.like("saag-001%"))) == 0
