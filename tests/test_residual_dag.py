from __future__ import annotations

import json
from pathlib import Path

from ai_ent.residual_dag import (
    _cycle,
    generate_residual_implementation_plan,
    write_residual_implementation_plan,
)
from tests.residual_artifact_fixtures import write_test_pir_artifact


def test_c08_implementation_gap_produces_bounded_implementation_tasks(tmp_path: Path) -> None:
    plan = generate_residual_implementation_plan(pir_artifact=write_test_pir_artifact(tmp_path))

    c08_tasks = [task for task in plan.tasks if "C08" in task.capabilities]

    assert [task.id for task in c08_tasks] == ["RES-C08-CONTRACT", "RES-C08-SERVICE", "RES-C08-VERIFICATION"]
    assert [task.task_type for task in c08_tasks] == [
        "IMPLEMENTATION_TASK",
        "IMPLEMENTATION_TASK",
        "VERIFICATION_TASK",
    ]
    assert not any(task.id == "RES-C08-EVIDENCE" for task in plan.tasks)


def test_evidence_gaps_do_not_become_code_implementation_tasks(tmp_path: Path) -> None:
    plan = generate_residual_implementation_plan(pir_artifact=write_test_pir_artifact(tmp_path))

    evidence_tasks = {
        task.capabilities[0]: task
        for task in plan.tasks
        if task.task_type == "EVIDENCE_CLOSURE_TASK"
    }

    assert sorted(evidence_tasks) == ["C05", "C06", "C07", "C09"]
    assert all(task.id.endswith("-EVIDENCE") for task in evidence_tasks.values())
    assert all("Do not implement new runtime behavior" in task.objective for task in evidence_tasks.values())


def test_all_pir_gaps_are_covered_and_no_orphan_tasks_exist(tmp_path: Path) -> None:
    plan = generate_residual_implementation_plan(pir_artifact=write_test_pir_artifact(tmp_path))
    gap_ids = {task.gap_ids[0] for task in plan.tasks}

    assert gap_ids == {"PIR-GAP-C05", "PIR-GAP-C06", "PIR-GAP-C07", "PIR-GAP-C08", "PIR-GAP-C09"}
    assert plan.ok
    assert not any(finding.finding_id.endswith("-ORPHAN") for finding in plan.findings)


def test_closed_capability_work_is_not_regenerated(tmp_path: Path) -> None:
    plan = generate_residual_implementation_plan(pir_artifact=write_test_pir_artifact(tmp_path))

    generated_capabilities = {capability for task in plan.tasks for capability in task.capabilities}

    assert generated_capabilities == {"C05", "C06", "C07", "C08", "C09"}
    assert "C20" not in generated_capabilities


def test_dependency_graph_waves_and_gates_are_deterministic(tmp_path: Path) -> None:
    pir_artifact = write_test_pir_artifact(tmp_path)
    first = write_residual_implementation_plan(output_dir=tmp_path / "compiled", pir_artifact=pir_artifact)
    first_bytes = (tmp_path / "compiled" / "residual-implementation-plan.json").read_bytes()
    second = write_residual_implementation_plan(output_dir=tmp_path / "compiled", pir_artifact=pir_artifact)

    assert first.residual_plan_hash == second.residual_plan_hash
    assert (tmp_path / "compiled" / "residual-implementation-plan.json").read_bytes() == first_bytes
    assert first.dependency_edges == second.dependency_edges
    assert first.waves == second.waves
    assert len(first.human_gates) == 2


def test_material_pir_gap_change_changes_plan_hash(tmp_path: Path) -> None:
    target = tmp_path / "PIR-001.json"
    source = write_test_pir_artifact(tmp_path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    target.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    original = generate_residual_implementation_plan(pir_artifact=target)

    payload["residual_gaps"][0]["missing_behavior_or_evidence"] = "changed evidence requirement"
    target.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    changed = generate_residual_implementation_plan(pir_artifact=target)

    assert original.residual_plan_hash != changed.residual_plan_hash


def test_invalid_residual_reference_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "not-pir.json"
    source = write_test_pir_artifact(tmp_path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["gate_id"] = "OTHER"
    target.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    try:
        generate_residual_implementation_plan(pir_artifact=target)
    except ValueError as exc:
        assert "not a PIR-001 artifact" in str(exc)
    else:
        raise AssertionError("invalid PIR artifact was accepted")


def test_cycle_detection_reports_deterministic_cycle() -> None:
    assert _cycle({"A": {"B"}, "B": {"C"}, "C": {"A"}}) == ["A", "B", "C", "A"]
