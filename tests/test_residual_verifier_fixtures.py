from __future__ import annotations

import sys
from pathlib import Path

from ai_ent.residual_dag import generate_residual_implementation_plan
from ai_ent.residual_runtime_handoff import load_residual_runtime_handoff_artifacts
from tests.residual_artifact_fixtures import (
    residual_feasibility_artifact_path,
    residual_test_environment,
    write_test_pir_artifact,
    write_test_rpg_artifact,
)


def test_residual_fixture_generation_does_not_require_ignored_runtime_artifacts(tmp_path: Path) -> None:
    missing_pir = tmp_path / "missing-artifacts" / "pir-001" / "PIR-001.json"
    missing_rpg = tmp_path / "missing-artifacts" / "rpg-001" / "RPG-001.json"
    assert not missing_pir.exists()
    assert not missing_rpg.exists()

    pir_artifact = write_test_pir_artifact(tmp_path)
    rpg_artifact = write_test_rpg_artifact(tmp_path, pir_artifact)
    plan = generate_residual_implementation_plan(pir_artifact=pir_artifact)
    artifacts = load_residual_runtime_handoff_artifacts(
        pir_artifact=pir_artifact,
        acceptance_artifact_path=rpg_artifact,
        frozen_plan_artifact=tmp_path / "compiled" / "residual-implementation-plan.json",
        accepted_feasibility_artifact=residual_feasibility_artifact_path(tmp_path),
    )

    assert plan.tasks
    assert artifacts.rpg_acceptance["gate_id"] == "RPG-001"
    assert not missing_pir.exists()
    assert not missing_rpg.exists()


def test_residual_test_environment_uses_active_interpreter() -> None:
    environment = residual_test_environment()

    assert environment.python_path == sys.executable
    assert Path(environment.python_path).exists()
