from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.persistence.models import Base, Execution, Task
from ai_ent.post_implementation import (
    evaluate_post_implementation,
    evaluate_post_residual_implementation,
    write_post_implementation_review,
)
from ai_ent.residual_runtime_handoff import ResidualRuntimePlanImporter
from ai_ent.runtime_handoff import RuntimePlanImporter, load_runtime_handoff_artifacts
from tests.residual_artifact_fixtures import (
    residual_feasibility_artifact_path,
    write_test_pir_artifact,
    write_test_rpg_artifact,
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


def import_runtime_plan(session: Session, tmp_path: Path) -> str:
    artifacts = load_runtime_handoff_artifacts(output_dir=tmp_path / "compiled", accepted_artifact_path=None)
    result = RuntimePlanImporter().import_frozen_plan(
        session,
        artifacts,
        require_clean_git=False,
        require_codex_command=False,
    )
    assert result.ok
    return result.plan_id


def mark_all_imported_tasks_passed(session: Session) -> None:
    for task in session.scalars(select(Task).where(Task.id.like("IMPL-%")).order_by(Task.id)).all():
        task.status = "passed"
        session.add(
            Execution(
                id=f"exec-{task.id.lower()}-1",
                task_id=task.id,
                executor_type="codex",
                status="succeeded",
                attempt=1,
                terminal_state="success",
                candidate_tree_hash=f"{task.id.lower().replace('-', ''):0<64}"[:64],
                commit_hash=f"{task.id.lower().replace('-', ''):1<64}"[:64],
            )
        )
    session.flush()


def mark_residual_tasks_passed(session: Session) -> None:
    for task in session.scalars(select(Task).where(Task.id.like("RES-%")).order_by(Task.id)).all():
        task.status = "passed"
        session.add(
            Execution(
                id=f"exec-{task.id.lower()}-1",
                task_id=task.id,
                executor_type="codex",
                status="succeeded",
                attempt=1,
                terminal_state="success",
                candidate_tree_hash=f"{task.id.lower().replace('-', ''):2<64}"[:64],
                commit_hash=f"{task.id.lower().replace('-', ''):3<64}"[:64],
            )
        )
    session.flush()


def import_residual_plan(session: Session, tmp_path: Path, monkeypatch: Any) -> str:
    from ai_ent.residual_runtime_handoff import load_residual_runtime_handoff_artifacts

    pir_artifact = write_test_pir_artifact(tmp_path)
    rpg_artifact = write_test_rpg_artifact(tmp_path, pir_artifact)
    artifacts = load_residual_runtime_handoff_artifacts(
        pir_artifact=pir_artifact,
        acceptance_artifact_path=rpg_artifact,
        frozen_plan_artifact=tmp_path / "compiled" / "residual-implementation-plan.json",
        accepted_feasibility_artifact=residual_feasibility_artifact_path(tmp_path),
    )
    monkeypatch.setattr("ai_ent.residual_runtime_handoff.RESIDUAL_PLAN_ID", artifacts.dry_run.lock.residual_plan_id)
    result = ResidualRuntimePlanImporter().import_residual_plan(
        session,
        artifacts,
        require_clean_git=False,
        require_codex_command=False,
    )
    assert result.ok
    return result.residual_plan_id


def write_rcg_artifact(tmp_path: Path) -> None:
    path = tmp_path / "artifacts" / "rcg-001" / "RCG-001.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"result":"ACCEPTED_WITH_LIMITATIONS"}\n', encoding="utf-8")
    beag = tmp_path / "artifacts" / "beag-001" / "BEAG-001.json"
    beag.parent.mkdir(parents=True, exist_ok=True)
    beag.write_text('{"result":"ACCEPTED"}\n', encoding="utf-8")


def test_post_implementation_uses_runtime_evidence_to_close_frozen_plan_gaps(tmp_path: Path) -> None:
    factory = session_factory()
    with factory() as session:
        plan_id = import_runtime_plan(session, tmp_path)
        mark_all_imported_tasks_passed(session)

        review = evaluate_post_implementation(session, artifacts_dir=tmp_path / "artifacts", plan_id=plan_id)

    by_id = {row.capability_id: row for row in review.capability_matrix}
    assert by_id["C01"].new_satisfaction == "SATISFIED"
    assert by_id["C20"].new_satisfaction == "SATISFIED"
    assert by_id["C08"].new_satisfaction == "SATISFIED"
    assert review.result == "MANIFEST_DECISION_REQUIRED"
    assert not any(gap.gap_id == "PIR-GAP-C08" for gap in review.residual_gaps)
    assert not any(gap.gap_type == "IMPLEMENTATION_GAP" for gap in review.residual_gaps)


def test_failed_execution_attempt_is_historical_not_implementation_evidence(tmp_path: Path) -> None:
    factory = session_factory()
    with factory() as session:
        plan_id = import_runtime_plan(session, tmp_path)
        task = session.get(Task, "IMPL-C01-CMP-001")
        assert task is not None
        task.status = "failed"
        session.add(
            Execution(
                id="exec-impl-c01-cmp-001-1",
                task_id=task.id,
                executor_type="codex",
                status="failed",
                attempt=1,
                terminal_state="verification_failed",
            )
        )
        session.flush()

        review = evaluate_post_implementation(session, artifacts_dir=tmp_path / "artifacts", plan_id=plan_id)

    by_id = {row.capability_id: row for row in review.capability_matrix}
    assert by_id["C01"].new_satisfaction == "UNSATISFIED"
    assert "exec-impl-c01-cmp-001-1" not in by_id["C01"].evidence_added


def test_post_implementation_outputs_are_deterministic(tmp_path: Path) -> None:
    factory = session_factory()
    with factory() as session:
        plan_id = import_runtime_plan(session, tmp_path)
        mark_all_imported_tasks_passed(session)

        first = write_post_implementation_review(
            session,
            output_dir=tmp_path / "compiled",
            artifacts_dir=tmp_path / "artifacts",
            plan_id=plan_id,
        )
        first_bytes = (tmp_path / "artifacts" / "pir-001" / "PIR-001.json").read_bytes()
        second = write_post_implementation_review(
            session,
            output_dir=tmp_path / "compiled",
            artifacts_dir=tmp_path / "artifacts",
            plan_id=plan_id,
        )

    assert first.residual_gap_hash == second.residual_gap_hash
    assert (tmp_path / "artifacts" / "pir-001" / "PIR-001.json").read_bytes() == first_bytes


def test_artifact_evidence_graph_is_non_authoritative_in_pir(tmp_path: Path) -> None:
    factory = session_factory()
    with factory() as session:
        plan_id = import_runtime_plan(session, tmp_path)
        mark_all_imported_tasks_passed(session)

        review = evaluate_post_implementation(session, artifacts_dir=tmp_path / "artifacts", plan_id=plan_id)

    assert review.evidence_authority["artifact_evidence_graph"] == "NON_AUTHORITATIVE provenance/index only"


def test_pir_gap_c07_maps_to_runtime_source_tests_and_accepted_evidence(tmp_path: Path) -> None:
    factory = session_factory()
    with factory() as session:
        plan_id = import_runtime_plan(session, tmp_path)
        mark_all_imported_tasks_passed(session)

        review = evaluate_post_implementation(session, artifacts_dir=tmp_path / "artifacts", plan_id=plan_id)

    by_gap = {gap.gap_id: gap for gap in review.residual_gaps}

    assert set(by_gap["PIR-GAP-C07"].current_evidence) >= {
        "BEAG-001:src/ai_ent/runtime_handoff.py",
        "BEAG-001:src/ai_ent/runtime_kernel.py",
        "BEAG-001:src/ai_ent/scheduler/bounded.py",
        "BEAG-001:tests/test_runtime_handoff.py",
        "BEAG-001:tests/test_runtime_kernel.py",
        "BEAG-001:tests/test_bounded_scheduler_runner.py",
        "BEAG-001:guarded-autonomous-runner",
        "BEAG-001:runtime-plan-import",
        "BEAG-001:pytest-regression-suite",
    }


def test_pir_gap_c09_maps_to_kernel_source_tests_and_accepted_evidence(tmp_path: Path) -> None:
    factory = session_factory()
    with factory() as session:
        plan_id = import_runtime_plan(session, tmp_path)
        mark_all_imported_tasks_passed(session)

        review = evaluate_post_implementation(session, artifacts_dir=tmp_path / "artifacts", plan_id=plan_id)

    by_gap = {gap.gap_id: gap for gap in review.residual_gaps}

    assert set(by_gap["PIR-GAP-C09"].current_evidence) >= {
        "BEAG-001:src/ai_ent/runtime_kernel.py",
        "BEAG-001:src/ai_ent/execution_planner.py",
        "BEAG-001:tests/test_runtime_kernel.py",
        "BEAG-001:tests/test_execution_planner.py",
        "BEAG-001:runtime-plan-import",
        "BEAG-001:guarded-autonomous-runner",
        "BEAG-001:pytest-regression-suite",
    }
    assert review.evidence_authority["artifact_evidence_graph"] == "NON_AUTHORITATIVE provenance/index only"


def test_pir_c08_gap_closes_with_manifest_evidence_without_authority_expansion(
    tmp_path: Path,
) -> None:
    factory = session_factory()
    with factory() as session:
        plan_id = import_runtime_plan(session, tmp_path)
        mark_all_imported_tasks_passed(session)

        review = evaluate_post_implementation(session, artifacts_dir=tmp_path / "artifacts", plan_id=plan_id)

    by_id = {row.capability_id: row for row in review.capability_matrix}
    c08 = by_id["C08"]

    assert c08.previous_satisfaction == "UNSATISFIED"
    assert c08.new_satisfaction == "SATISFIED"
    assert c08.new_maturity == "VALIDATED"
    assert c08.remaining_missing_evidence == ()
    assert c08.blockers == ()
    assert set(c08.evidence_added) >= {
        "BEAG-001:src/ai_ent/governance_contract.py",
        "BEAG-001:src/ai_ent/governance_evolution.py",
        "BEAG-001:tests/test_governance_contract.py",
        "BEAG-001:tests/test_governance_evolution.py",
        "RPG-001:RES-C08-CONTRACT",
        "RPG-001:RES-C08-SERVICE",
        "RPG-001:RES-C08-VERIFICATION",
        "BEAG-001:pytest-regression-suite",
    }
    assert "PIR-GAP-C08" not in {gap.gap_id for gap in review.residual_gaps}
    assert {
        row["state"]
        for row in review.previous_gap_evaluation
        if row["capability_id"] == "C08"
    } == {"CLOSED"}
    assert review.evidence_authority == {
        "postgresql_runtime_records": "runtime authority",
        "git_commits": "accepted source and commit authority",
        "independent_verifier": "verification authority",
        "human_gate_records": "approval authority",
        "artifact_evidence_graph": "NON_AUTHORITATIVE provenance/index only",
    }


def test_pir_002_closes_residual_gap_set_with_combined_runtime_evidence(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    factory = session_factory()
    with factory() as session:
        prior_plan_id = "PLAN-1a75a2e3c5a7"
        seed_prior_plan_complete(session)
        residual_plan_id = import_residual_plan(session, tmp_path, monkeypatch)
        mark_residual_tasks_passed(session)
        write_rcg_artifact(tmp_path)

        review = evaluate_post_residual_implementation(
            session,
            artifacts_dir=tmp_path / "artifacts",
            prior_plan_id=prior_plan_id,
            residual_plan_id=residual_plan_id,
        )

    assert review.as_dict()["gate_id"] == "PIR-002"
    assert review.result == "NO_RESIDUAL_IMPLEMENTATION_GAPS"
    assert review.residual_gaps == ()
    assert (
        review.requirement_coverage["after"]["covered_requirements"]
        == review.requirement_coverage["after"]["normative_requirements"]
    )
    assert review.requirement_coverage["after"]["blocked_requirements"] == 0
    assert review.requirement_coverage["after"]["warning_count"] == 0

    by_id = {row.capability_id: row for row in review.capability_matrix}
    for capability_id in ("C05", "C06", "C07", "C08", "C09"):
        assert by_id[capability_id].previous_satisfaction in {"PARTIALLY_SATISFIED", "UNSATISFIED"}
        assert by_id[capability_id].new_satisfaction == "SATISFIED"
        assert by_id[capability_id].remaining_missing_evidence == ()


def test_pir_002_keeps_deferred_capabilities_and_evidence_graph_non_authoritative(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    factory = session_factory()
    with factory() as session:
        prior_plan_id = "PLAN-1a75a2e3c5a7"
        seed_prior_plan_complete(session)
        residual_plan_id = import_residual_plan(session, tmp_path, monkeypatch)
        mark_residual_tasks_passed(session)
        write_rcg_artifact(tmp_path)

        review = evaluate_post_residual_implementation(
            session,
            artifacts_dir=tmp_path / "artifacts",
            prior_plan_id=prior_plan_id,
            residual_plan_id=residual_plan_id,
        )

    by_id = {row.capability_id: row for row in review.capability_matrix}
    assert by_id["C10"].new_satisfaction == "NOT_APPLICABLE"
    assert by_id["C11"].new_satisfaction == "NOT_APPLICABLE"
    assert review.evidence_authority["artifact_evidence_graph"] == "NON_AUTHORITATIVE provenance/index only"
