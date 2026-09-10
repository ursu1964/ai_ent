from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.e2e_application_proof import (
    TARGET_PROJECT_ID,
    run_first_application_creation_proof,
    write_first_application_creation_proof,
)
from ai_ent.persistence.models import Base
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
    mark_residual_tasks_passed(session)
    write_rcg_artifact(tmp_path)
    session.flush()
    return residual_plan_id


def test_e2e_001_generates_runnable_team_work_tracker(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    factory = session_factory()
    with factory() as session:
        residual_plan_id = seed_complete_runtime(session, tmp_path, monkeypatch)

        result = run_first_application_creation_proof(
            session,
            target_workspace=tmp_path / "team-work-tracker",
            artifacts_dir=tmp_path / "artifacts",
            residual_plan_id=residual_plan_id,
            run_docker=False,
        )

    assert result.result == "ACCEPTED_WITH_LIMITATIONS"
    assert result.target_project_id == TARGET_PROJECT_ID
    assert result.application_commit
    assert all(proof.status in {"PASS", "SKIPPED"} for proof in result.proofs)
    assert (tmp_path / "team-work-tracker" / "src" / "team_work_tracker" / "app.py").exists()
    assert (tmp_path / "team-work-tracker" / ".ai-enterprise" / "manifest" / "implementation-plan.json").exists()


def test_e2e_001_writer_is_deterministic_for_same_inputs(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    factory = session_factory()
    with factory() as session:
        residual_plan_id = seed_complete_runtime(session, tmp_path, monkeypatch)

        first = write_first_application_creation_proof(
            session,
            target_workspace=tmp_path / "team-work-tracker",
            artifacts_dir=tmp_path / "artifacts",
            residual_plan_id=residual_plan_id,
            run_docker=False,
        )
        first_bytes = (tmp_path / "artifacts" / "e2e-001" / "E2E-001.json").read_bytes()
        second = write_first_application_creation_proof(
            session,
            target_workspace=tmp_path / "team-work-tracker",
            artifacts_dir=tmp_path / "artifacts",
            residual_plan_id=residual_plan_id,
            run_docker=False,
        )

    assert first.acceptance_hash == second.acceptance_hash
    assert (tmp_path / "artifacts" / "e2e-001" / "E2E-001.json").read_bytes() == first_bytes
