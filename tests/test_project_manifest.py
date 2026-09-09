from __future__ import annotations

from pathlib import Path

from ai_ent.project_manifest import load_manifest_documents, validate_project_manifest


def test_ai_ent_project_manifest_validates_cleanly() -> None:
    result = validate_project_manifest(Path("manifest/project/ai-ent"))

    assert result.ok
    assert result.requirements_count >= 20
    assert result.components_count >= 8
    assert result.interfaces_count >= 5
    assert len(result.capabilities_selected) == 20
    assert "C12" in result.already_satisfied
    assert "C15" in result.still_missing
    assert len(result.compilation_hash) == 64


def test_project_manifest_hash_is_deterministic() -> None:
    root = Path("manifest/project/ai-ent")
    first = validate_project_manifest(root)
    second = validate_project_manifest(root)

    assert first.compilation_hash == second.compilation_hash
    assert first.manifest_files == second.manifest_files


def test_manifest_validator_detects_unresolved_capability_reference(tmp_path: Path) -> None:
    source = Path("manifest/project/ai-ent")
    target = tmp_path / "manifest"
    _copy_manifest(source, target)
    functional = target / "requirements" / "functional.yaml"
    functional.write_text(
        functional.read_text(encoding="utf-8").replace("capabilities: [C03, C20]", "capabilities: [C03, C99]"),
        encoding="utf-8",
    )

    result = validate_project_manifest(target)

    assert not result.ok
    assert any("unknown capability C99" in error for error in result.validation_errors)


def test_manifest_validator_detects_duplicate_ids(tmp_path: Path) -> None:
    source = Path("manifest/project/ai-ent")
    target = tmp_path / "manifest"
    _copy_manifest(source, target)
    extra = target / "requirements" / "duplicate.yaml"
    extra.write_text(
        "requirements:\n"
        "  - id: FR-001\n"
        "    title: Duplicate\n"
        "    classification: NORMATIVE\n"
        "    source_refs: [R1]\n"
        "    capabilities: [C01]\n",
        encoding="utf-8",
    )

    result = validate_project_manifest(target)

    assert not result.ok
    assert any("duplicate id FR-001" in error for error in result.validation_errors)


def test_manifest_loader_finds_all_yaml_documents() -> None:
    documents = load_manifest_documents(Path("manifest/project/ai-ent"))

    assert Path("manifest/project/ai-ent/project.yaml") in documents
    assert Path("manifest/project/ai-ent/capabilities.yaml") in documents


def _copy_manifest(source: Path, target: Path) -> None:
    for path in source.rglob("*.yaml"):
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
