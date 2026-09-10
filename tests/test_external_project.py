from __future__ import annotations

from dataclasses import replace

from ai_ent.external_project import (
    CONTROL_PLANE_AUTHORITY,
    DEFAULT_PROHIBITED_PATHS,
    EXTERNAL_PROJECT_LIFECYCLE,
    MANDATORY_VERIFICATION_COMMANDS,
    ExternalProject,
    GeneratedArtifact,
    ProjectPlan,
    ProjectRepository,
    ProjectRuntimeBinding,
    ProjectWorkspace,
    external_project_model_record,
    validate_external_project,
)


def external_project() -> ExternalProject:
    workspace = ProjectWorkspace(
        workspace_id="WS-001",
        root_path="/srv/ai-enterprise/projects/customer-portal",
        owner_project_id="EXT-CUSTOMER-PORTAL",
        allowed_roots=("generated/customer-portal",),
    )
    repository = ProjectRepository(
        repository_id="REPO-001",
        workspace_id=workspace.workspace_id,
        remote_url="https://git.example/customer-portal.git",
        state="BOUND",
    )
    plan = ProjectPlan(
        plan_id="PLAN-001",
        version="1",
        state="FROZEN",
        task_ids=("TASK-001", "TASK-002"),
        required_human_gates=("GATE-001",),
    )
    binding = ProjectRuntimeBinding(
        binding_id="BIND-001",
        project_id="EXT-CUSTOMER-PORTAL",
        workspace_id=workspace.workspace_id,
        repository_id=repository.repository_id,
        plan_id=plan.plan_id,
        plan_version=plan.version,
        state="READY_FOR_IMPORT",
        approved_human_gates=("GATE-001",),
    )
    artifact = GeneratedArtifact(
        artifact_id="ART-001",
        project_id="EXT-CUSTOMER-PORTAL",
        workspace_id=workspace.workspace_id,
        kind="source",
        relative_path="src/app.py",
        content_hash="a" * 64,
        metadata={"mime_type": "text/x-python"},
    )
    return ExternalProject(
        project_id="EXT-CUSTOMER-PORTAL",
        name="Customer Portal",
        lifecycle_state="IMPORT",
        workspace=workspace,
        repository=repository,
        plan=plan,
        runtime_binding=binding,
        artifacts=(artifact,),
    )


def test_external_project_model_serializes_deterministically() -> None:
    first = external_project()
    second = external_project()

    assert first.as_dict() == second.as_dict()
    assert first.model_hash == second.model_hash
    assert first.as_dict()["model_hash"] == first.model_hash
    assert validate_external_project(first).ok
    assert first.plan.effective_plan_hash == second.plan.effective_plan_hash
    assert first.plan.verification_commands == MANDATORY_VERIFICATION_COMMANDS
    assert first.runtime_binding.control_plane_authority == CONTROL_PLANE_AUTHORITY
    assert first.as_dict()["lifecycle_state"] in EXTERNAL_PROJECT_LIFECYCLE


def test_model_record_keeps_control_plane_authority_external_to_project() -> None:
    original = external_project()
    project = replace(
        original,
        repository=replace(
            original.repository,
            remote_url="https://user:token@git.example/customer-portal.git",
        ),
    )
    record = external_project_model_record(project)

    assert record["record_kind"] == "external_project_model"
    assert record["validation"]["ok"] is True
    assert record["project"]["repository"]["remote_url"] == (
        "https://<redacted>@git.example/customer-portal.git"
    )
    assert record["project"]["repository"]["control_plane_write_authority"] is False
    assert record["project"]["runtime_binding"]["grants_control_plane_authority"] is False
    assert record["project"]["runtime_binding"]["verifier_bypass_authority"] is False
    assert record["project"]["runtime_binding"]["control_plane_authority"] == (
        "existing_ai_enterprise_control_plane"
    )


def test_human_gates_must_be_explicit_before_import_or_execution() -> None:
    project = external_project()
    blocked = replace(
        project,
        runtime_binding=replace(
            project.runtime_binding,
            approved_human_gates=(),
            pending_human_gates=("GATE-001",),
            implicit_human_gate_approval=True,
        ),
    )
    validation = validate_external_project(blocked)
    finding_ids = {finding.finding_id for finding in validation.findings}

    assert validation.ok is False
    assert "EPM-GATE-IMPLICIT-APPROVAL" in finding_ids
    assert "EPM-GATE-PENDING-GATE-001" in finding_ids


def test_independent_verification_commands_are_mandatory() -> None:
    project = external_project()
    blocked = replace(
        project,
        plan=replace(
            project.plan,
            independent_verification_required=False,
            verification_commands=(),
        ),
        runtime_binding=replace(project.runtime_binding, verification_commands=()),
    )
    validation = validate_external_project(blocked)
    messages = "\n".join(finding.message for finding in validation.findings)

    assert validation.ok is False
    assert "plan disables independent verification" in messages
    assert "/home/user/projects/ai_ent/aient/bin/python -m pytest -q" in messages
    assert "/home/user/projects/ai_ent/aient/bin/python -m ruff check ." in messages
    assert "/home/user/projects/ai_ent/aient/bin/python -m pyright" in messages


def test_artifact_projection_redacts_secret_metadata_and_validation_rejects_it() -> None:
    project = external_project()
    artifact = replace(
        project.artifacts[0],
        relative_path="../.env",
        contains_secret=True,
        metadata={
            "build": {"credential_ref": "should-not-render"},
            "mime_type": "text/plain",
        },
    )
    blocked = replace(project, artifacts=(artifact,))
    validation = validate_external_project(blocked)
    artifact_record = blocked.as_dict()["artifacts"][0]
    finding_ids = {finding.finding_id for finding in validation.findings}

    assert artifact_record["metadata"]["build"]["credential_ref"] == "<redacted>"
    assert "should-not-render" not in str(blocked.as_dict())
    assert validation.ok is False
    assert "EPM-ARTIFACT-SECRET-ART-001" in finding_ids
    assert "EPM-ARTIFACT-PATH-ART-001" in finding_ids
    assert "EPM-ARTIFACT-METADATA-SECRET-ART-001" in finding_ids


def test_storage_records_keep_domain_parts_and_policy_boundaries() -> None:
    project = external_project()
    records = project.as_storage_records()

    assert records["external_project"]["project_id"] == project.project_id
    assert records["project_workspace"]["prohibited_paths"] == list(DEFAULT_PROHIBITED_PATHS)
    assert records["project_repository"]["commit_policy"] == "verified_commits_only"
    assert records["project_plan"]["independent_verification_required"] is True
    assert records["project_runtime_binding"]["implicit_human_gate_approval"] is False
    assert records["generated_artifacts"][0]["authority_state"] == "NON_AUTHORITATIVE"


def test_scope_policy_rejects_allowed_prohibited_path_overlap() -> None:
    project = external_project()
    blocked = replace(
        project,
        workspace=replace(project.workspace, allowed_roots=(".build/generated-app",)),
    )
    validation = validate_external_project(blocked)
    finding_ids = {finding.finding_id for finding in validation.findings}

    assert validation.ok is False
    assert "EPM-SCOPE-CONFLICT-build-generated-app" in finding_ids
