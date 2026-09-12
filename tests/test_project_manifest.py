from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ai_ent.project_manifest import (
    EnvironmentProfile,
    GeneratedImplementationTask,
    compile_project_manifest,
    default_feasibility_policy,
    dry_run_evaluated_plan,
    dry_run_implementation_plan,
    evaluate_feasibility,
    evaluate_implementation_plan_feasibility,
    freeze_implementation_plan,
    generate_implementation_plan,
    load_manifest_documents,
    resolve_capabilities,
    validate_project_manifest,
    validate_traces,
    write_capability_resolution,
    write_compiled_project,
    write_dry_run_plan,
    write_feasibility_evaluation,
    write_implementation_plan,
    write_trace_validation,
)


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


def test_compile_project_manifest_is_repeatable() -> None:
    root = Path("manifest/project/ai-ent")
    first = compile_project_manifest(root)
    second = compile_project_manifest(root)

    assert first.lock.compiled_hash == second.lock.compiled_hash
    assert first.compiled_bytes == second.compiled_bytes
    assert first.lock.as_dict() == second.lock.as_dict()
    assert first.compiled.source_metadata["source_file_count"] == 30


def test_c05_evidence_gap_maps_to_existing_accepted_source_and_tests() -> None:
    compiled = compile_project_manifest(Path("manifest/project/ai-ent")).compiled
    by_id = {str(capability["id"]): capability for capability in compiled.capabilities}

    assert set(by_id["C05"]["evidence_refs"]) >= {
        "BEAG-001:src/ai_ent/manifest_compiler.py",
        "BEAG-001:src/ai_ent/project_manifest.py",
        "BEAG-001:tests/test_manifest_compiler.py",
        "BEAG-001:tests/test_project_manifest.py",
        "BEAG-001:pytest-regression-suite",
    }


def test_c06_evidence_gap_maps_to_existing_source_tests_and_runtime_evidence() -> None:
    compiled = compile_project_manifest(Path("manifest/project/ai-ent")).compiled
    by_id = {str(capability["id"]): capability for capability in compiled.capabilities}

    assert set(by_id["C06"]["evidence_refs"]) >= {
        "BEAG-001:src/ai_ent/generator_orchestrator.py",
        "BEAG-001:src/ai_ent/deterministic_validator.py",
        "BEAG-001:tests/test_generator_orchestrator.py",
        "BEAG-001:tests/test_deterministic_validator.py",
        "BEAG-001:scope-and-prohibited-path-checks",
        "BEAG-001:guarded-codex-execution",
        "BEAG-001:pytest-regression-suite",
    }


def test_c07_evidence_gap_maps_to_runtime_source_tests_and_accepted_evidence() -> None:
    compiled = compile_project_manifest(Path("manifest/project/ai-ent")).compiled
    by_id = {str(capability["id"]): capability for capability in compiled.capabilities}

    assert set(by_id["C07"]["evidence_refs"]) >= {
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


def test_c08_capability_maps_to_governance_source_tests_and_task_provenance() -> None:
    compiled = compile_project_manifest(Path("manifest/project/ai-ent")).compiled
    by_id = {str(capability["id"]): capability for capability in compiled.capabilities}

    assert by_id["C08"]["maturity"] == "VALIDATED"
    assert set(by_id["C08"]["source_refs"]) >= {"R8", "BEAG-001"}
    assert set(by_id["C08"]["evidence_refs"]) >= {
        "BEAG-001:src/ai_ent/governance_contract.py",
        "BEAG-001:src/ai_ent/governance_evolution.py",
        "BEAG-001:tests/test_governance_contract.py",
        "BEAG-001:tests/test_governance_evolution.py",
        "BEAG-001:tests/test_project_manifest.py",
        "BEAG-001:tests/test_post_implementation.py",
        "RPG-001:RES-C08-CONTRACT",
        "RPG-001:RES-C08-SERVICE",
        "RPG-001:RES-C08-VERIFICATION",
        "BEAG-001:pytest-regression-suite",
    }


def test_c09_evidence_gap_maps_to_kernel_source_tests_and_accepted_evidence() -> None:
    compiled = compile_project_manifest(Path("manifest/project/ai-ent")).compiled
    by_id = {str(capability["id"]): capability for capability in compiled.capabilities}

    assert set(by_id["C09"]["evidence_refs"]) >= {
        "BEAG-001:src/ai_ent/runtime_kernel.py",
        "BEAG-001:src/ai_ent/execution_planner.py",
        "BEAG-001:tests/test_runtime_kernel.py",
        "BEAG-001:tests/test_execution_planner.py",
        "BEAG-001:runtime-plan-import",
        "BEAG-001:guarded-autonomous-runner",
        "BEAG-001:pytest-regression-suite",
    }


def test_compiled_project_retains_source_provenance() -> None:
    compiled = compile_project_manifest(Path("manifest/project/ai-ent")).compiled

    assert compiled.capabilities[0]["source_manifest_file"] == "capabilities.yaml"
    assert compiled.requirements[0]["source_manifest_file"].startswith("requirements/")
    assert compiled.project["source_manifest_file"] == "project.yaml"


def test_compile_writes_generated_derivatives_without_secrets(tmp_path: Path) -> None:
    result = write_compiled_project(Path("manifest/project/ai-ent"), tmp_path / "compiled")

    project_json = tmp_path / "compiled" / "project.json"
    lock = tmp_path / "compiled" / "manifest.lock"
    first_project_bytes = project_json.read_bytes()
    first_lock_bytes = lock.read_bytes()
    result_again = write_compiled_project(Path("manifest/project/ai-ent"), tmp_path / "compiled")

    assert result.lock.compiled_hash == result_again.lock.compiled_hash
    assert project_json.read_bytes() == first_project_bytes
    assert lock.read_bytes() == first_lock_bytes
    assert "generated by ai_ent.project_manifest" in project_json.read_text(encoding="utf-8")
    assert "password" not in project_json.read_text(encoding="utf-8").lower()
    assert "secret" not in project_json.read_text(encoding="utf-8").lower()


def test_semantic_key_order_changes_preserve_compiled_hash(tmp_path: Path) -> None:
    source = Path("manifest/project/ai-ent")
    target = tmp_path / "manifest"
    _copy_manifest(source, target)
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

    assert compile_project_manifest(source).lock.compiled_hash == compile_project_manifest(target).lock.compiled_hash


def test_material_requirement_change_changes_compiled_hash(tmp_path: Path) -> None:
    source = Path("manifest/project/ai-ent")
    target = tmp_path / "manifest"
    _copy_manifest(source, target)
    functional = target / "requirements" / "functional.yaml"
    functional.write_text(
        functional.read_text(encoding="utf-8").replace("Parse approved project manifests", "Parse altered project manifests"),
        encoding="utf-8",
    )

    assert compile_project_manifest(source).lock.compiled_hash != compile_project_manifest(target).lock.compiled_hash


def test_material_capability_status_change_changes_compiled_hash(tmp_path: Path) -> None:
    source = Path("manifest/project/ai-ent")
    target = tmp_path / "manifest"
    _copy_manifest(source, target)
    capabilities = target / "capabilities.yaml"
    capabilities.write_text(
        capabilities.read_text(encoding="utf-8").replace("status: REQUIRED", "status: DEFERRED", 1),
        encoding="utf-8",
    )

    assert compile_project_manifest(source).lock.compiled_hash != compile_project_manifest(target).lock.compiled_hash


def test_invalid_reference_blocks_compilation(tmp_path: Path) -> None:
    source = Path("manifest/project/ai-ent")
    target = tmp_path / "manifest"
    _copy_manifest(source, target)
    interfaces = target / "architecture" / "interfaces.yaml"
    interfaces.write_text(
        interfaces.read_text(encoding="utf-8").replace("to_component: CMP-002", "to_component: CMP-999", 1),
        encoding="utf-8",
    )

    try:
        compile_project_manifest(target)
    except ValueError as exc:
        assert "validation failed" in str(exc)
    else:
        raise AssertionError("invalid reference did not block compilation")


def test_unknown_normative_field_changes_hash_without_being_dropped(tmp_path: Path) -> None:
    source = Path("manifest/project/ai-ent")
    target = tmp_path / "manifest"
    _copy_manifest(source, target)
    operations = target / "operations.yaml"
    operations.write_text(
        operations.read_text(encoding="utf-8") + "unknown_normative_field: must remain visible\n",
        encoding="utf-8",
    )

    compiled = compile_project_manifest(target)

    assert compiled.lock.compiled_hash != compile_project_manifest(source).lock.compiled_hash
    assert compiled.compiled.as_dict()["operations"]


def test_capability_resolver_resolves_all_twenty_capabilities() -> None:
    resolution = resolve_capabilities(Path("manifest/project/ai-ent"))

    assert len(resolution.capabilities) == 20
    assert resolution.resolver_version == "mmc-003.1"
    assert len(resolution.source_compiled_hash) == 64
    assert len(resolution.capability_resolution_hash) == 64
    assert "C12" in resolution.satisfied_capabilities
    assert "C03" in resolution.partially_satisfied_capabilities
    assert "C01" in resolution.unsatisfied_capabilities


def test_capability_resolver_expands_direct_and_transitive_dependencies() -> None:
    resolution = resolve_capabilities(Path("manifest/project/ai-ent"))
    by_id = {capability.capability_id: capability for capability in resolution.capabilities}

    assert "C19" in by_id["C20"].direct_dependencies
    assert "C16" in by_id["C20"].transitive_dependencies
    assert ("C20", "C19") in resolution.direct_edges
    assert ("C20", "C01") in resolution.transitive_edges


def test_capability_conditional_dependency_false_is_not_applied() -> None:
    resolution = resolve_capabilities(Path("manifest/project/ai-ent"))
    by_id = {capability.capability_id: capability for capability in resolution.capabilities}

    assert by_id["C11"].conditional_dependencies_applied == ()
    assert "C06" not in by_id["C11"].direct_dependencies
    assert "C11" in resolution.deferred_capabilities


def test_capability_conditional_dependency_true_is_applied(tmp_path: Path) -> None:
    source = Path("manifest/project/ai-ent")
    target = tmp_path / "manifest"
    _copy_manifest(source, target)
    deployment = target / "deployment.yaml"
    deployment.write_text(
        deployment.read_text(encoding="utf-8").replace("current_mode: local_development", "current_mode: production_cloud"),
        encoding="utf-8",
    )

    resolution = resolve_capabilities(target)
    by_id = {capability.capability_id: capability for capability in resolution.capabilities}

    assert "C11" in resolution.conditional_capabilities_activated
    assert "C06" in by_id["C11"].direct_dependencies
    assert by_id["C11"].conditional_dependencies_applied


def test_capability_unknown_dependency_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "manifest"
    _copy_manifest(Path("manifest/project/ai-ent"), target)
    capabilities = target / "capabilities.yaml"
    capabilities.write_text(
        capabilities.read_text(encoding="utf-8").replace("depends_on: [C01]", "depends_on: [C99]", 1),
        encoding="utf-8",
    )

    try:
        resolve_capabilities(target)
    except ValueError as exc:
        assert "unknown capability C99" in str(exc)
    else:
        raise AssertionError("unknown capability dependency was not rejected")


def test_capability_self_dependency_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "manifest"
    _copy_manifest(Path("manifest/project/ai-ent"), target)
    capabilities = target / "capabilities.yaml"
    capabilities.write_text(
        capabilities.read_text(encoding="utf-8").replace("depends_on: [C01]", "depends_on: [C02]", 1),
        encoding="utf-8",
    )

    try:
        resolve_capabilities(target)
    except ValueError as exc:
        assert "C02 cannot depend on itself" in str(exc)
    else:
        raise AssertionError("self dependency was not rejected")


def test_capability_cycle_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "manifest"
    _copy_manifest(Path("manifest/project/ai-ent"), target)
    capabilities = target / "capabilities.yaml"
    capabilities.write_text(
        capabilities.read_text(encoding="utf-8").replace("depends_on: [C01]", "depends_on: [C01, C20]", 1),
        encoding="utf-8",
    )

    try:
        resolve_capabilities(target)
    except ValueError as exc:
        assert "capability dependency cycle" in str(exc)
    else:
        raise AssertionError("cycle was not rejected")


def test_capability_resolution_is_repeatable_and_writes_artifacts(tmp_path: Path) -> None:
    first = write_capability_resolution(Path("manifest/project/ai-ent"), tmp_path / "compiled-a")
    second = write_capability_resolution(Path("manifest/project/ai-ent"), tmp_path / "compiled-b")

    assert first.capability_resolution_hash == second.capability_resolution_hash
    assert (tmp_path / "compiled-a" / "capability-resolution.json").read_bytes() == (
        tmp_path / "compiled-b" / "capability-resolution.json"
    ).read_bytes()
    assert (tmp_path / "compiled-a" / "capability-graph.json").exists()
    assert (tmp_path / "compiled-a" / "capability-gaps.json").exists()


def test_material_capability_change_changes_resolution_hash(tmp_path: Path) -> None:
    target = tmp_path / "manifest"
    _copy_manifest(Path("manifest/project/ai-ent"), target)
    capabilities = target / "capabilities.yaml"
    capabilities.write_text(
        capabilities.read_text(encoding="utf-8").replace("maturity: VALIDATED", "maturity: NOT_IMPLEMENTED", 1),
        encoding="utf-8",
    )

    assert resolve_capabilities(Path("manifest/project/ai-ent")).capability_resolution_hash != resolve_capabilities(
        target
    ).capability_resolution_hash


def test_trace_validator_accepts_actual_manifest() -> None:
    result = validate_traces(Path("manifest/project/ai-ent"))
    summary = result.as_dict()["summary"]

    assert result.ok
    assert result.trace_validator_version == "mmc-004.1"
    assert len(result.source_compiled_hash) == 64
    assert len(result.capability_resolution_hash) == 64
    assert len(result.trace_validation_hash) == 64
    assert summary["normative_requirements"] >= 20
    assert summary["error_count"] == 0
    assert summary["justified_components"] >= 8
    assert summary["justified_interfaces"] >= 5


def test_trace_validator_retains_evidence_and_partial_coverage() -> None:
    result = validate_traces(Path("manifest/project/ai-ent"))
    by_requirement = {path.requirement_id: path for path in result.trace_paths}

    assert any(ref.startswith("BEAG-001:") for ref in by_requirement["ACC-001"].evidence_refs)
    assert by_requirement["NFR-003"].coverage == "PARTIALLY_COVERED"
    assert any(finding.finding_id == "TRACE-NFR-003-C20-GAP" for finding in result.findings)


def test_trace_validator_propagates_unsatisfied_capability_to_blocked_requirement() -> None:
    result = validate_traces(Path("manifest/project/ai-ent"))
    by_requirement = {path.requirement_id: path for path in result.trace_paths}

    assert by_requirement["FR-002"].coverage == "BLOCKED"
    assert any(finding.finding_id == "TRACE-FR-002-C02-GAP" for finding in result.findings)


def test_trace_validator_deferred_requirement_is_not_mandatory() -> None:
    result = validate_traces(Path("manifest/project/ai-ent"))
    by_requirement = {path.requirement_id: path for path in result.trace_paths}

    assert by_requirement["INT-001"].coverage == "DEFERRED"
    assert any(finding.finding_id == "TRACE-INT-001-DEFERRED" for finding in result.findings)


def test_trace_validator_detects_missing_architecture_path(tmp_path: Path) -> None:
    target = tmp_path / "manifest"
    _copy_manifest(Path("manifest/project/ai-ent"), target)
    system = target / "architecture" / "system.yaml"
    system.write_text(
        system.read_text(encoding="utf-8").replace("capabilities: [C01, C14]", "capabilities: [C14]"),
        encoding="utf-8",
    )
    extra = target / "requirements" / "trace-no-architecture.yaml"
    extra.write_text(
        "requirements:\n"
        "  - id: TRACE-NO-ARCH\n"
        "    title: Requirements engineering trace with no component\n"
        "    classification: NORMATIVE\n"
        "    source_refs: [R1]\n"
        "    capabilities: [C01]\n",
        encoding="utf-8",
    )

    result = validate_traces(target)

    assert not result.ok
    assert any(finding.finding_id == "TRACE-TRACE-NO-ARCH-NO-ARCH" for finding in result.findings)


def test_trace_validator_detects_missing_verification_path(tmp_path: Path) -> None:
    target = tmp_path / "manifest"
    _copy_manifest(Path("manifest/project/ai-ent"), target)
    verification = target / "verification.yaml"
    verification.write_text("verification:\n  gates: []\n  commands: []\n", encoding="utf-8")

    result = validate_traces(target)

    assert not result.ok
    assert any(finding.finding_id == "TRACE-FR-001-NO-VERIFY" for finding in result.findings)


def test_trace_validator_detects_orphan_component(tmp_path: Path) -> None:
    target = tmp_path / "manifest"
    _copy_manifest(Path("manifest/project/ai-ent"), target)
    components = target / "architecture" / "components.yaml"
    components.write_text(
        components.read_text(encoding="utf-8")
        + "\n  - id: CMP-999\n"
        "    name: Orphan implementation component\n"
        "    classification: NORMATIVE\n"
        "    source_refs: [R11]\n"
        "    capabilities: [C11]\n",
        encoding="utf-8",
    )

    result = validate_traces(target)

    assert not result.ok
    assert any(finding.finding_id == "TRACE-CMP-999-ORPHAN" for finding in result.findings)


def test_trace_validator_detects_orphan_interface(tmp_path: Path) -> None:
    target = tmp_path / "manifest"
    _copy_manifest(Path("manifest/project/ai-ent"), target)
    interfaces = target / "architecture" / "interfaces.yaml"
    interfaces.write_text(
        interfaces.read_text(encoding="utf-8").replace("to_component: CMP-002", "to_component: CMP-999", 1),
        encoding="utf-8",
    )

    try:
        validate_traces(target)
    except ValueError as exc:
        assert "validation failed" in str(exc)
    else:
        raise AssertionError("invalid interface endpoint did not block trace validation")


def test_trace_validator_detects_missing_authentication_architecture(tmp_path: Path) -> None:
    target = tmp_path / "manifest"
    _copy_manifest(Path("manifest/project/ai-ent"), target)
    extra = target / "requirements" / "trace-auth.yaml"
    extra.write_text(
        "requirements:\n"
        "  - id: TRACE-AUTH\n"
        "    title: Authenticated request must be accepted\n"
        "    classification: NORMATIVE\n"
        "    source_refs: [R1]\n"
        "    capabilities: [C01]\n",
        encoding="utf-8",
    )

    result = validate_traces(target)

    assert not result.ok
    assert any(finding.finding_id == "TRACE-TRACE-AUTH-AUTH-ARCH" for finding in result.findings)


def test_trace_validator_detects_requirement_deployment_contradiction(tmp_path: Path) -> None:
    target = tmp_path / "manifest"
    _copy_manifest(Path("manifest/project/ai-ent"), target)
    extra = target / "requirements" / "trace-deployment.yaml"
    extra.write_text(
        "requirements:\n"
        "  - id: TRACE-DEPLOY\n"
        "    title: Production deployment must be active in current release\n"
        "    classification: NORMATIVE\n"
        "    source_refs: [R12]\n"
        "    capabilities: [C12]\n",
        encoding="utf-8",
    )

    result = validate_traces(target)

    assert not result.ok
    assert any(finding.finding_id == "TRACE-TRACE-DEPLOY-DEPLOYMENT-CONFLICT" for finding in result.findings)


def test_trace_validation_output_is_repeatable_and_writes_artifacts(tmp_path: Path) -> None:
    first = write_trace_validation(Path("manifest/project/ai-ent"), tmp_path / "compiled-a")
    second = write_trace_validation(Path("manifest/project/ai-ent"), tmp_path / "compiled-b")

    assert first.trace_validation_hash == second.trace_validation_hash
    assert (tmp_path / "compiled-a" / "trace-validation.json").read_bytes() == (
        tmp_path / "compiled-b" / "trace-validation.json"
    ).read_bytes()
    assert (tmp_path / "compiled-a" / "trace-graph.json").exists()
    assert (tmp_path / "compiled-a" / "requirement-coverage.json").exists()
    assert (tmp_path / "compiled-a" / "architecture-coverage.json").exists()
    assert (tmp_path / "compiled-a" / "implementation-gaps.json").exists()


def test_material_trace_change_changes_trace_hash(tmp_path: Path) -> None:
    target = tmp_path / "manifest"
    _copy_manifest(Path("manifest/project/ai-ent"), target)
    verification = target / "verification.yaml"
    verification.write_text(
        verification.read_text(encoding="utf-8").replace("        - R22\n", "", 1),
        encoding="utf-8",
    )

    assert validate_traces(Path("manifest/project/ai-ent")).trace_validation_hash != validate_traces(
        target
    ).trace_validation_hash


def test_implementation_plan_generates_remaining_task_dag() -> None:
    plan = generate_implementation_plan(Path("manifest/project/ai-ent"))
    summary = plan.as_dict()["summary"]

    assert plan.ok
    assert plan.generator_version == "mmc-005.1"
    assert len(plan.implementation_plan_hash) == 64
    assert summary["executable_task_count"] == 13
    assert summary["dependency_edge_count"] == 23
    assert summary["wave_count"] == 10
    assert summary["critical_path_task_count"] == 10
    assert summary["human_gate_count"] == 6


def test_implementation_plan_covers_all_trace_gaps_without_orphan_tasks() -> None:
    plan = generate_implementation_plan(Path("manifest/project/ai-ent"))
    trace = validate_traces(Path("manifest/project/ai-ent"))
    covered = {capability_id for task in plan.tasks for capability_id in task.implements["capabilities"]}

    assert set(trace.implementation_gaps) <= covered
    for task in plan.tasks:
        assert task.implements["requirements"] or task.implements["capabilities"] or task.implements["components"]
        assert task.verification["acceptance_criteria"]
        assert task.provenance["trace_validation_hash"] == plan.trace_validation_hash


def test_implementation_plan_does_not_regenerate_satisfied_bootstrap_capabilities() -> None:
    plan = generate_implementation_plan(Path("manifest/project/ai-ent"))
    executable_capabilities = {capability_id for task in plan.tasks for capability_id in task.implements["capabilities"]}
    satisfied_capabilities = {capability_id for unit in plan.satisfied_units for capability_id in unit.capabilities}

    assert {"C12", "C13"} <= satisfied_capabilities
    assert "C12" not in executable_capabilities
    assert "C13" not in executable_capabilities


def test_implementation_plan_dependency_graph_is_acyclic_and_complete() -> None:
    plan = generate_implementation_plan(Path("manifest/project/ai-ent"))
    task_ids = {task.id for task in plan.tasks}

    assert all(task_id in task_ids and dependency_id in task_ids for task_id, dependency_id in plan.dependency_edges)
    assert all(task_id != dependency_id for task_id, dependency_id in plan.dependency_edges)
    assert plan.critical_path[0] == "IMPL-C01-CMP-001"
    assert plan.critical_path[-1] == "IMPL-C20-CMP-007"


def test_implementation_plan_classifies_trace_warnings() -> None:
    root = Path("manifest/project/ai-ent")
    plan = generate_implementation_plan(root)
    trace = validate_traces(root)

    assert len(plan.warning_classifications) == trace.warning_count
    assert {warning.classification for warning in plan.warning_classifications} == {"PLANNING_RELEVANT"}
    assert {"DATA-004", "OPS-004", "OPS-005", "OPS-006"} <= {
        warning.subject_id for warning in plan.warning_classifications
    }


def test_implementation_plan_writes_repeatable_artifacts(tmp_path: Path) -> None:
    first = write_implementation_plan(Path("manifest/project/ai-ent"), tmp_path / "compiled-a")
    second = write_implementation_plan(Path("manifest/project/ai-ent"), tmp_path / "compiled-b")

    assert first.implementation_plan_hash == second.implementation_plan_hash
    assert (tmp_path / "compiled-a" / "implementation-plan.json").read_bytes() == (
        tmp_path / "compiled-b" / "implementation-plan.json"
    ).read_bytes()
    assert (tmp_path / "compiled-a" / "implementation-dag.json").exists()
    assert (tmp_path / "compiled-a" / "implementation-waves.json").exists()
    assert (tmp_path / "compiled-a" / "critical-path.json").exists()
    assert (tmp_path / "compiled-a" / "human-gates.json").exists()
    assert (tmp_path / "compiled-a" / "task-generation-findings.json").exists()
    assert len(list((tmp_path / "compiled-a" / "generated-tasks").glob("*.json"))) == len(first.tasks)


def test_material_manifest_change_changes_implementation_plan_hash(tmp_path: Path) -> None:
    target = tmp_path / "manifest"
    _copy_manifest(Path("manifest/project/ai-ent"), target)
    functional = target / "requirements" / "functional.yaml"
    functional.write_text(
        functional.read_text(encoding="utf-8").replace("Persist project memory", "Persist durable project memory"),
        encoding="utf-8",
    )

    assert generate_implementation_plan(Path("manifest/project/ai-ent")).implementation_plan_hash != generate_implementation_plan(
        target
    ).implementation_plan_hash


def test_trace_error_blocks_implementation_plan_generation(tmp_path: Path) -> None:
    target = tmp_path / "manifest"
    _copy_manifest(Path("manifest/project/ai-ent"), target)
    verification = target / "verification.yaml"
    verification.write_text("verification:\n  gates: []\n  commands: []\n", encoding="utf-8")

    try:
        generate_implementation_plan(target)
    except ValueError as exc:
        assert "trace validation failed" in str(exc)
    else:
        raise AssertionError("trace errors did not block implementation DAG generation")


def test_generated_implementation_dag_cycle_is_rejected() -> None:
    from ai_ent.project_manifest import _validate_implementation_plan

    first = _generated_test_task("IMPL-TEST-A", ("IMPL-TEST-B",), "C01")
    second = _generated_test_task("IMPL-TEST-B", ("IMPL-TEST-A",), "C02")

    findings = _validate_implementation_plan(
        (first, second),
        (("IMPL-TEST-A", "IMPL-TEST-B"), ("IMPL-TEST-B", "IMPL-TEST-A")),
        ("C01", "C02"),
    )

    assert any(finding.finding_id == "PLAN-CYCLE" for finding in findings)


def test_feasibility_evaluator_processes_all_generated_tasks() -> None:
    evaluation = evaluate_feasibility(
        Path("manifest/project/ai-ent"),
        environment=_environment(codex_configured=False, codex_available=True),
    )
    summary = evaluation.as_dict()["summary"]

    assert evaluation.ok
    assert evaluation.evaluator_version == "mmc-006.1"
    assert len(evaluation.feasibility_hash) == 64
    assert summary["total_tasks"] == 13
    assert summary["human_gates"] == 6
    assert summary["blocked"] == 0
    assert summary["technical_blockers"] == 0
    assert summary["feasible_with_conditions"] == 7
    assert summary["human_approval_required"] == 6


def test_feasibility_evaluator_marks_safe_task_feasible_when_configured() -> None:
    plan = generate_implementation_plan(Path("manifest/project/ai-ent"))
    evaluation = evaluate_implementation_plan_feasibility(
        plan,
        _environment(codex_configured=True, codex_available=True),
        default_feasibility_policy(plan),
    )
    by_task = {result.task_id: result for result in evaluation.task_results}

    assert by_task["IMPL-C01-CMP-001"].status == "FEASIBLE"
    assert by_task["IMPL-C01-CMP-001"].policy_decision == "AUTO_ALLOWED"


def test_feasibility_evaluator_marks_missing_codex_command_as_condition() -> None:
    plan = generate_implementation_plan(Path("manifest/project/ai-ent"))
    evaluation = evaluate_implementation_plan_feasibility(
        plan,
        _environment(codex_configured=False, codex_available=True),
        default_feasibility_policy(plan),
    )
    by_task = {result.task_id: result for result in evaluation.task_results}

    assert by_task["IMPL-C01-CMP-001"].status == "FEASIBLE_WITH_CONDITIONS"
    assert "set AIENT_CODEX_COMMAND before real execution" in by_task["IMPL-C01-CMP-001"].conditions


def test_feasibility_evaluator_blocks_missing_agent_role() -> None:
    plan = generate_implementation_plan(Path("manifest/project/ai-ent"))
    policy = replace(default_feasibility_policy(plan), agent_roles=("AGT-002", "AGT-003", "AGT-004"))
    evaluation = evaluate_implementation_plan_feasibility(plan, _environment(), policy)
    by_task = {result.task_id: result for result in evaluation.task_results}

    assert by_task["IMPL-C01-CMP-001"].status == "BLOCKED"
    assert any("missing agent role AGT-001" in blocker for blocker in by_task["IMPL-C01-CMP-001"].blockers)


def test_feasibility_evaluator_blocks_missing_model_profile() -> None:
    plan = generate_implementation_plan(Path("manifest/project/ai-ent"))
    policy = replace(default_feasibility_policy(plan), model_profiles=("ARCHITECTURE_REASONING", "SECURITY_REVIEW"))
    evaluation = evaluate_implementation_plan_feasibility(plan, _environment(), policy)
    by_task = {result.task_id: result for result in evaluation.task_results}

    assert by_task["IMPL-C01-CMP-001"].status == "BLOCKED"
    assert any("missing model profile CODING_STANDARD" in blocker for blocker in by_task["IMPL-C01-CMP-001"].blockers)


def test_feasibility_evaluator_blocks_unavailable_tool() -> None:
    plan = generate_implementation_plan(Path("manifest/project/ai-ent"))
    evaluation = evaluate_implementation_plan_feasibility(
        plan,
        _environment(codex_configured=False, codex_available=False),
        default_feasibility_policy(plan),
    )

    assert evaluation.plan_status == "BLOCKED"
    assert any("Codex executable is unavailable" in blocker for blocker in evaluation.technical_blockers)


def test_feasibility_evaluator_blocks_invalid_write_scope() -> None:
    plan = generate_implementation_plan(Path("manifest/project/ai-ent"))
    bad_task = replace(
        plan.tasks[0],
        write_scope={"allowed": ("../outside",), "prohibited": (".env",)},
    )
    bad_plan = replace(plan, tasks=(bad_task, *plan.tasks[1:]))
    evaluation = evaluate_implementation_plan_feasibility(bad_plan, _environment(), default_feasibility_policy(plan))
    by_task = {result.task_id: result for result in evaluation.task_results}

    assert by_task[bad_task.id].status == "BLOCKED"
    assert any("write scope escapes repository" in blocker for blocker in by_task[bad_task.id].blockers)


def test_feasibility_evaluator_blocks_missing_verification_profile() -> None:
    plan = generate_implementation_plan(Path("manifest/project/ai-ent"))
    policy = replace(default_feasibility_policy(plan), verification_profiles=("FULL_REGRESSION",))
    evaluation = evaluate_implementation_plan_feasibility(plan, _environment(), policy)
    by_task = {result.task_id: result for result in evaluation.task_results}

    assert by_task["IMPL-C01-CMP-001"].status == "BLOCKED"
    assert any("missing verification profile STANDARD_REGRESSION" in blocker for blocker in by_task["IMPL-C01-CMP-001"].blockers)


def test_feasibility_evaluator_marks_human_gated_task() -> None:
    plan = generate_implementation_plan(Path("manifest/project/ai-ent"))
    evaluation = evaluate_implementation_plan_feasibility(plan, _environment(), default_feasibility_policy(plan))
    by_task = {result.task_id: result for result in evaluation.task_results}

    assert by_task["IMPL-C14-CMP-001"].status == "HUMAN_APPROVAL_REQUIRED"
    assert by_task["IMPL-C14-CMP-001"].required_human_gates == ("GATE-IMPL-C14-CMP-001",)


def test_feasibility_evaluator_high_risk_cannot_bypass_policy() -> None:
    plan = generate_implementation_plan(Path("manifest/project/ai-ent"))
    policy = replace(default_feasibility_policy(plan), guarded_allowed_risks=("LOW", "MEDIUM"))
    evaluation = evaluate_implementation_plan_feasibility(plan, _environment(), policy)
    by_task = {result.task_id: result for result in evaluation.task_results}

    assert by_task["IMPL-C14-CMP-001"].status == "BLOCKED"
    assert by_task["IMPL-C14-CMP-001"].policy_decision == "PROHIBITED"


def test_feasibility_evaluator_propagates_blocked_prerequisite() -> None:
    plan = generate_implementation_plan(Path("manifest/project/ai-ent"))
    bad_task = replace(plan.tasks[0], write_scope={"allowed": ("../outside",), "prohibited": (".env",)})
    bad_plan = replace(plan, tasks=(bad_task, *plan.tasks[1:]))
    evaluation = evaluate_implementation_plan_feasibility(bad_plan, _environment(), default_feasibility_policy(plan))
    by_task = {result.task_id: result for result in evaluation.task_results}

    assert by_task["IMPL-C02-CONTRACT"].status == "BLOCKED"
    assert any(blocker == "BLOCKED_BY_DEPENDENCY:IMPL-C01-CMP-001" for blocker in by_task["IMPL-C02-CONTRACT"].blockers)


def test_feasibility_evaluator_detects_resource_constraint() -> None:
    plan = generate_implementation_plan(Path("manifest/project/ai-ent"))
    gpu_task = replace(plan.tasks[0], execution={**plan.tasks[0].execution, "model_profile": "GPU_REQUIRED"})
    gpu_plan = replace(plan, tasks=(gpu_task, *plan.tasks[1:]))
    policy = replace(default_feasibility_policy(plan), model_profiles=(*default_feasibility_policy(plan).model_profiles, "GPU_REQUIRED"))
    evaluation = evaluate_implementation_plan_feasibility(
        gpu_plan,
        _environment(gpu_available=False),
        policy,
    )
    by_task = {result.task_id: result for result in evaluation.task_results}

    assert by_task[gpu_task.id].status == "BLOCKED"
    assert any("GPU resource is required" in blocker for blocker in by_task[gpu_task.id].blockers)


def test_feasibility_evaluation_hash_is_repeatable_and_changes_with_policy(tmp_path: Path) -> None:
    environment = _environment()
    first = write_feasibility_evaluation(Path("manifest/project/ai-ent"), tmp_path / "compiled-a")
    second = write_feasibility_evaluation(Path("manifest/project/ai-ent"), tmp_path / "compiled-b")
    plan = generate_implementation_plan(Path("manifest/project/ai-ent"))
    changed_policy = replace(default_feasibility_policy(plan), max_parallel_width=1)
    changed = evaluate_implementation_plan_feasibility(plan, environment, changed_policy)

    assert first.feasibility_hash == second.feasibility_hash
    assert (tmp_path / "compiled-a" / "feasibility-report.json").read_bytes() == (
        tmp_path / "compiled-b" / "feasibility-report.json"
    ).read_bytes()
    assert first.feasibility_hash != changed.feasibility_hash
    assert (tmp_path / "compiled-a" / "task-feasibility.json").exists()
    assert (tmp_path / "compiled-a" / "policy-decisions.json").exists()
    assert (tmp_path / "compiled-a" / "human-gate-plan.json").exists()
    assert (tmp_path / "compiled-a" / "resource-plan.json").exists()
    assert (tmp_path / "compiled-a" / "concurrency-plan.json").exists()


def test_dry_run_simulates_complete_current_plan() -> None:
    dry_run = dry_run_implementation_plan(
        Path("manifest/project/ai-ent"),
        environment=_environment(codex_configured=False, codex_available=True),
    )

    assert dry_run.ok
    assert dry_run.dry_runner_version == "mmc-007.1"
    assert dry_run.plan_acceptance_state == "READY_WITH_EXECUTION_PREREQUISITES"
    assert dry_run.frozen_plan_state == "FROZEN"
    assert len(dry_run.import_preview) == 13
    assert dry_run.dependency_edge_count == 23
    assert dry_run.wave_count == 10
    assert dry_run.theoretical_parallel_width == 2
    assert dry_run.effective_parallel_width == 1
    assert len(dry_run.lock.human_gate_definitions) == 6
    assert len(dry_run.execution_batches) == 10
    assert "set AIENT_CODEX_COMMAND before real execution" in dry_run.execution_prerequisites


def test_dry_run_freezes_task_fingerprints_and_import_preview() -> None:
    dry_run = dry_run_implementation_plan(Path("manifest/project/ai-ent"), environment=_environment())
    preview_by_id = {preview.task_id: preview for preview in dry_run.import_preview}

    assert set(dry_run.lock.task_fingerprints) == set(preview_by_id)
    assert preview_by_id["IMPL-C01-CMP-001"].execution_class == "implementation"
    assert preview_by_id["IMPL-C01-CMP-001"].schedulable
    assert preview_by_id["IMPL-C01-CMP-001"].verification_profile == "STANDARD_REGRESSION"
    assert preview_by_id["IMPL-C20-CMP-007"].risk_level == "HIGH"


def test_dry_run_batches_preserve_human_gate_boundaries() -> None:
    dry_run = dry_run_implementation_plan(Path("manifest/project/ai-ent"), environment=_environment())
    gated_batches = [batch for batch in dry_run.execution_batches if batch.required_human_gates]

    assert dry_run.execution_batches[0].task_ids == ("IMPL-C01-CMP-001", "IMPL-C02-CONTRACT")
    assert len(gated_batches) == 6
    assert gated_batches[0].required_human_gates == ("GATE-IMPL-C14-CMP-001",)
    assert gated_batches[0].task_ids == ("IMPL-C14-CMP-001",)


def test_dry_run_defines_repair_and_recovery_paths_for_each_task() -> None:
    dry_run = dry_run_implementation_plan(Path("manifest/project/ai-ent"), environment=_environment())
    task_ids = {preview.task_id for preview in dry_run.import_preview}

    assert set(dry_run.repair_path_coverage) == task_ids
    assert {
        "before_task",
        "after_claim",
        "after_executor",
        "after_verification",
        "after_verified_commit",
        "after_db_completion",
        "between_batches",
        "at_human_gates",
    } <= set(dry_run.recovery_points)


def test_dry_run_blocks_on_missing_technical_dependency() -> None:
    plan = generate_implementation_plan(Path("manifest/project/ai-ent"))
    evaluation = evaluate_implementation_plan_feasibility(
        plan,
        replace(_environment(), docker_available=False),
        default_feasibility_policy(plan),
    )
    dry_run = dry_run_evaluated_plan(plan, evaluation)

    assert dry_run.plan_acceptance_state == "BLOCKED"
    assert dry_run.frozen_plan_state == "INVALIDATED"
    assert any(finding.severity == "ERROR" for finding in dry_run.findings)


def test_dry_run_output_and_freeze_are_repeatable(tmp_path: Path) -> None:
    first = freeze_implementation_plan(Path("manifest/project/ai-ent"), tmp_path / "compiled-a")
    second = freeze_implementation_plan(Path("manifest/project/ai-ent"), tmp_path / "compiled-b")

    assert first.dry_run_hash == second.dry_run_hash
    assert first.lock.as_dict() == second.lock.as_dict()
    assert (tmp_path / "compiled-a" / "implementation-dry-run.json").read_bytes() == (
        tmp_path / "compiled-b" / "implementation-dry-run.json"
    ).read_bytes()
    assert (tmp_path / "compiled-a" / "execution-batches.json").exists()
    assert (tmp_path / "compiled-a" / "task-import-preview.json").exists()
    assert (tmp_path / "compiled-a" / "recovery-plan.json").exists()
    assert (tmp_path / "compiled-a" / "dry-run-findings.json").exists()
    assert (tmp_path / "compiled-a" / "implementation-plan.lock").exists()
    assert (tmp_path / "compiled-a" / "plan-freeze.json").exists()


def test_material_upstream_change_changes_dry_run_hash(tmp_path: Path) -> None:
    target = tmp_path / "manifest"
    _copy_manifest(Path("manifest/project/ai-ent"), target)
    functional = target / "requirements" / "functional.yaml"
    functional.write_text(
        functional.read_text(encoding="utf-8").replace(
            "Parse approved project manifests",
            "Parse accepted project manifests",
        ),
        encoding="utf-8",
    )

    assert dry_run_implementation_plan(Path("manifest/project/ai-ent")).dry_run_hash != dry_run_implementation_plan(
        target
    ).dry_run_hash


def test_task_fingerprint_change_changes_dry_run_hash() -> None:
    plan = generate_implementation_plan(Path("manifest/project/ai-ent"))
    evaluation = evaluate_implementation_plan_feasibility(plan, _environment(), default_feasibility_policy(plan))
    original = dry_run_evaluated_plan(plan, evaluation)
    changed_task = replace(plan.tasks[0], fingerprint="changed")
    changed_plan = replace(plan, tasks=(changed_task, *plan.tasks[1:]))
    changed_evaluation = evaluate_implementation_plan_feasibility(
        changed_plan,
        _environment(),
        default_feasibility_policy(changed_plan),
    )
    changed = dry_run_evaluated_plan(changed_plan, changed_evaluation)

    assert original.dry_run_hash != changed.dry_run_hash


def test_write_dry_run_plan_does_not_freeze_lock(tmp_path: Path) -> None:
    write_dry_run_plan(Path("manifest/project/ai-ent"), tmp_path / "compiled")

    assert (tmp_path / "compiled" / "implementation-dry-run.json").exists()
    assert not (tmp_path / "compiled" / "implementation-plan.lock").exists()


def test_operation_source_refs_reject_requirement_id_namespace(tmp_path: Path) -> None:
    source = Path("manifest/project/ai-ent")
    target = tmp_path / "manifest"
    _copy_manifest(source, target)
    operations = target / "operations.yaml"
    operations.write_text(
        operations.read_text(encoding="utf-8").replace(
            "source_refs: [BEAG-001, R17, R20, R21, R22]",
            "source_refs: [OPS-004]",
            1,
        ),
        encoding="utf-8",
    )

    result = validate_project_manifest(target)

    assert not result.ok
    assert any("unresolved source_ref OPS-004" in error for error in result.validation_errors)


def test_operation_source_refs_preserve_exact_source_corpus_provenance() -> None:
    compiled = compile_project_manifest(Path("manifest/project/ai-ent")).compiled
    operations = {str(operation["id"]): operation for operation in compiled.operations}

    assert operations["OP-003"]["source_refs"] == ["BEAG-001", "R17", "R20", "R21", "R22"]
    assert operations["OP-004"]["source_refs"] == ["BEAG-001", "R17", "R20", "R22"]
    assert operations["OP-005"]["source_refs"] == ["BEAG-001", "R17", "R22"]


def test_operation_source_refs_serialize_readback_deterministically(tmp_path: Path) -> None:
    first = write_compiled_project(Path("manifest/project/ai-ent"), tmp_path / "first")
    second = write_compiled_project(Path("manifest/project/ai-ent"), tmp_path / "second")
    first_ops = first.compiled.as_dict()["operations"]
    second_ops = second.compiled.as_dict()["operations"]

    assert first.lock.compiled_hash == second.lock.compiled_hash
    assert first_ops == second_ops
    assert {
        operation["id"]: operation["source_refs"]
        for operation in first_ops
        if operation["id"] in {"OP-003", "OP-004", "OP-005"}
    } == {
        "OP-003": ["BEAG-001", "R17", "R20", "R21", "R22"],
        "OP-004": ["BEAG-001", "R17", "R20", "R22"],
        "OP-005": ["BEAG-001", "R17", "R22"],
    }


def _generated_test_task(
    task_id: str,
    depends_on: tuple[str, ...],
    capability_id: str,
) -> GeneratedImplementationTask:
    return GeneratedImplementationTask(
        id=task_id,
        title=task_id,
        objective=task_id,
        implements={
            "requirements": ("REQ-001",),
            "capabilities": (capability_id,),
            "components": (),
            "interfaces": (),
        },
        depends_on=depends_on,
        write_scope={"allowed": ("src/ai_ent/**",), "prohibited": (".env",)},
        execution={"agent_role": "AGT-001", "model_profile": "CODING_STANDARD", "executor": "codex"},
        verification={"profile": "STANDARD_REGRESSION", "acceptance_criteria": ("passes",)},
        risk={"level": "LOW", "reason": "test"},
        provenance={
            "compiled_project_hash": "compiled",
            "capability_resolution_hash": "capabilities",
            "trace_validation_hash": "trace",
            "generator_version": "test",
        },
        fingerprint=task_id,
        epic_id="EPC-001",
        feature_id="FEA-001",
    )


def _environment(
    *,
    codex_configured: bool = True,
    codex_available: bool = True,
    gpu_available: bool = False,
) -> EnvironmentProfile:
    return EnvironmentProfile(
        profile_id="test",
        repository_path="/repo",
        git_available=True,
        postgresql_available=True,
        alembic_available=True,
        docker_available=True,
        docker_compose_available=True,
        codex_command_configured=codex_configured,
        codex_executable_available=codex_available,
        python_path="/repo/aient/bin/python",
        python_available=True,
        ram_mb=8192,
        gpu_available=gpu_available,
        external_network="available",
        configured_secret_names=(),
    )


def _copy_manifest(source: Path, target: Path) -> None:
    for path in source.rglob("*.yaml"):
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
