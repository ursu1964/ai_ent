from __future__ import annotations

from pathlib import Path

import pytest

from ai_ent import project_manifest
from ai_ent.manifest_compiler import (
    MANIFEST_COMPILER_CONTRACT_VERSION,
    MANIFEST_COMPILER_OUTPUT_SCHEMA_VERSION,
    ManifestCompilerService,
)

MANIFEST_ROOT = Path("manifest/project/ai-ent")


def test_fr_002_compiler_service_emits_canonical_semantic_project_model() -> None:
    result = ManifestCompilerService().compile(MANIFEST_ROOT)
    fr_002 = result.canonical_model.object_by_id("requirement:FR-002")
    c15 = result.canonical_model.object_by_id("capability:C15")

    assert result.contract_version == MANIFEST_COMPILER_CONTRACT_VERSION
    assert result.schema_version == MANIFEST_COMPILER_OUTPUT_SCHEMA_VERSION
    assert result.validation_passed
    assert (
        result.canonical_model.source_compiled_hash
        == result.compilation.lock.compiled_hash
    )
    assert fr_002 is not None
    assert fr_002.name == "Compile manifests into canonical semantic project models"
    assert c15 is not None
    assert c15.name == "Manifest compiler"
    assert len(result.output_hash) == 64


def test_acc_002_validation_blocks_before_task_dag_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "manifest"
    _copy_manifest(MANIFEST_ROOT, target)
    functional = target / "requirements" / "functional.yaml"
    functional.write_text(
        functional.read_text(encoding="utf-8").replace(
            "capabilities: [C03, C20]",
            "capabilities: [C03, C99]",
        ),
        encoding="utf-8",
    )

    def fail_if_dag_generation_starts(*_: object, **__: object) -> object:
        raise AssertionError("task DAG generation ran before manifest validation passed")

    monkeypatch.setattr(
        project_manifest,
        "_generated_task_specs",
        fail_if_dag_generation_starts,
    )

    with pytest.raises(ValueError, match="project manifest validation failed"):
        project_manifest.generate_implementation_plan(target)


def test_nfr_001_compiler_output_is_repeatable_for_canonical_inputs(
    tmp_path: Path,
) -> None:
    service = ManifestCompilerService()
    target = tmp_path / "manifest"
    _copy_manifest(MANIFEST_ROOT, target)
    project = target / "project.yaml"
    project.write_text(
        "status: DEVELOPMENT\n"
        "classification: NORMATIVE\n"
        "version: 0.1.0\n"
        "name: AI-Enterprise\n"
        "id: PRJ-AI-ENT\n"
        "source_corpus:\n"
        + project.read_text(encoding="utf-8").split("source_corpus:\n", 1)[1],
        encoding="utf-8",
    )

    first = service.compile(MANIFEST_ROOT)
    second = service.compile(target)

    assert first.output_hash == second.output_hash
    assert first.as_dict() == second.as_dict()


def test_nfr_001_task_plan_hash_is_repeatable_after_validated_compilation() -> None:
    service = ManifestCompilerService()
    first_compilation = service.compile_for_task_dag(MANIFEST_ROOT)
    second_compilation = service.compile_for_task_dag(MANIFEST_ROOT)
    first_plan = project_manifest.generate_implementation_plan(MANIFEST_ROOT)
    second_plan = project_manifest.generate_implementation_plan(MANIFEST_ROOT)

    assert first_compilation.validation.ok
    assert first_compilation.lock.compiled_hash == second_compilation.lock.compiled_hash
    assert first_plan.source_compiled_hash == first_compilation.lock.compiled_hash
    assert first_plan.implementation_plan_hash == second_plan.implementation_plan_hash
    assert first_plan.as_dict() == second_plan.as_dict()


def _copy_manifest(source: Path, target: Path) -> None:
    for path in source.rglob("*.yaml"):
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
