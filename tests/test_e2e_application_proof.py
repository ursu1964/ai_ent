from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.e2e_application_proof import (
    TARGET_PROJECT_ID,
    E2EApplicationProofResult,
    E2ELimitation,
    E2EProof,
    E2EResultValue,
    _acceptance_hash,
    run_first_application_creation_proof,
    write_first_application_creation_proof,
)
from ai_ent.persistence.models import Base, RuntimePlanImport, RuntimeTaskPlanBinding
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
        runtime_proof = next(proof for proof in result.proofs if proof.proof_id == "I")
        runtime_import = runtime_proof.details["runtime_import"]
        assert all(proof.status in {"PASS", "SKIPPED"} for proof in result.proofs)
        assert runtime_import["status"] == "IMPORTED"
        assert runtime_import["control_plane_authority"] == "existing_ai_enterprise_control_plane"
        assert runtime_import["grants_control_plane_authority"] is False
        assert runtime_import["verifier_bypass_authority"] is False
        assert runtime_import["implicit_human_gate_approval"] is False
        assert runtime_import["independent_verification_required"] is True
        assert session.get(RuntimePlanImport, "rhi-plan-e2e-team-work-tracker-v1") is not None
        assert session.get(RuntimeTaskPlanBinding, "TWT-006-TEST-E2E") is not None
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
        first_status = _runtime_import_status(first)
        second = write_first_application_creation_proof(
            session,
            target_workspace=tmp_path / "team-work-tracker",
            artifacts_dir=tmp_path / "artifacts",
            residual_plan_id=residual_plan_id,
            run_docker=False,
        )
        second_status = _runtime_import_status(second)

    assert first.acceptance_hash == second.acceptance_hash
    assert first_status == "IMPORTED"
    assert second_status == "ALREADY_IMPORTED"


def test_acceptance_hash_normalizes_equivalent_runtime_import_success_statuses() -> None:
    expected = _hash_for_runtime_import_status("IMPORTED")

    assert _hash_for_runtime_import_status("ALREADY_IMPORTED") == expected
    assert _hash_for_runtime_import_status("IN_SYNC") == expected


def test_acceptance_hash_changes_for_material_runtime_and_proof_changes() -> None:
    baseline = _hash_for_runtime_import_status("IMPORTED")

    assert _hash_for_runtime_import_status("CONFLICT", runtime_ok=False, result="REJECTED") != baseline
    assert _hash_for_runtime_import_status("IMPORTED", canonical_hash="changed-requirements") != baseline
    assert _hash_for_runtime_import_status("IMPORTED", target_plan_hash="changed-plan") != baseline
    assert _hash_for_runtime_import_status("IMPORTED", journey_status="done") != baseline
    assert _hash_for_runtime_import_status("IMPORTED", persisted_tasks_total=0) != baseline


def _runtime_import_status(result: E2EApplicationProofResult) -> str:
    proof = next(proof for proof in result.proofs if proof.proof_id == "I")
    return str(proof.details["runtime_import"]["status"])


def _hash_for_runtime_import_status(
    status: str,
    *,
    runtime_ok: bool = True,
    result: E2EResultValue = "ACCEPTED_WITH_LIMITATIONS",
    canonical_hash: str = "canonical-hash",
    target_plan_hash: str = "target-plan-hash",
    journey_status: str = "in_progress",
    persisted_tasks_total: int = 1,
) -> str:
    runtime_proof_status = "PASS" if runtime_ok else "FAIL"
    return _acceptance_hash(
        head="head",
        version="e2e-001.test",
        target_project_id=TARGET_PROJECT_ID,
        intake_hash="intake-hash",
        canonical_hash=canonical_hash,
        architecture_hash="architecture-hash",
        target_plan_hash=target_plan_hash,
        application_commit="application-commit",
        proofs=(
            E2EProof("A", "Project intake", "PASS", {"manifest_hash": "intake-hash"}),
            E2EProof(
                "B",
                "Canonical project model",
                "PASS",
                {"requirements": [{"id": "REQ-001", "title": canonical_hash}]},
            ),
            E2EProof(
                "F",
                "Generated task DAG",
                "PASS",
                {"task_count": 6, "target_plan_hash": target_plan_hash},
            ),
            E2EProof(
                "I",
                "Guarded implementation",
                runtime_proof_status,
                {
                    "workspace_prepared": {
                        "created": "/tmp/path-that-must-not-drive-semantic-hash",
                        "git_initialized": True,
                    },
                    "runtime_import": {
                        "ok": runtime_ok,
                        "status": status,
                        "import_id": "rhi-plan-e2e-team-work-tracker-v1",
                        "importer_version": "epri-001.1",
                        "project_id": TARGET_PROJECT_ID,
                        "plan_id": "PLAN-E2E-TEAM-WORK-TRACKER",
                        "plan_version": "1",
                        "tasks_imported": 6,
                        "dependency_edges_imported": 6,
                        "human_gates_bound": 0,
                        "runtime_ready_tasks": ["TWT-001-MANIFEST"],
                        "gated_not_ready_tasks": [],
                        "blockers": [] if runtime_ok else ["conflict"],
                        "mandatory_verification_commands": [
                            "/tmp/venv/bin/python -m pytest -q",
                            "/tmp/venv/bin/python -m ruff check .",
                            "/tmp/venv/bin/python -m pyright",
                        ],
                        "frozen_plan_hash": "/tmp/path-derived-hash",
                    },
                    "application_commit": "application-commit",
                },
            ),
            E2EProof(
                "K",
                "Functional E2E and restart persistence",
                "PASS",
                {
                    "journey": {"updated_status": journey_status},
                    "persisted_after_restart": {
                        "projects_total": 1,
                        "tasks_total": persisted_tasks_total,
                    },
                    "restart_persistence": persisted_tasks_total == 1,
                },
            ),
        ),
        limitations=(E2ELimitation("ACCEPTED_LIMITATION", "docker skipped"),),
        result=result,
    )
