from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from ai_ent.external_project import (
    CONTROL_PLANE_AUTHORITY,
    DEFAULT_PROHIBITED_PATHS,
    EXTERNAL_PROJECT_LIFECYCLE,
    MANDATORY_VERIFICATION_COMMANDS,
    WORKSPACE_MARKER_PATH,
    ExternalProject,
    ExternalProjectFrozenPlan,
    ExternalProjectRuntimeTask,
    ExternalProjectServiceError,
    GeneratedAppWorkspaceManager,
    GeneratedArtifact,
    ProjectPlan,
    ProjectRepository,
    ProjectRuntimeBinding,
    ProjectWorkspace,
    external_project_frozen_plan_from_dict,
    external_project_frozen_plan_record,
    external_project_model_record,
    load_external_project_frozen_plan,
    validate_external_project,
    validate_external_project_frozen_plan,
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


def service_project(tmp_path: Path, *, remote_url: str | None = None) -> ExternalProject:
    project = external_project()
    workspace_root = tmp_path / "generated-apps" / "customer-portal"
    workspace = replace(
        project.workspace,
        root_path=str(workspace_root),
        state="DECLARED",
        allowed_roots=("src",),
    )
    repository = replace(
        project.repository,
        remote_url=remote_url,
        state="DECLARED",
    )
    artifact = replace(project.artifacts[0], relative_path="src/app.py")
    return replace(project, workspace=workspace, repository=repository, artifacts=(artifact,))


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
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


def test_workspace_manager_repeated_prepare_is_idempotent(tmp_path: Path) -> None:
    project = service_project(tmp_path)
    manager = GeneratedAppWorkspaceManager(
        tmp_path / "generated-apps",
        control_plane_root=tmp_path / "control-plane",
    )

    first = manager.prepare_workspace(project)
    second = manager.prepare_workspace(project)
    marker_payload = json.loads(Path(first.marker_path).read_text(encoding="utf-8"))

    assert first.as_dict() == second.as_dict()
    assert first.workspace.state == "PREPARED"
    assert first.prepared_paths == ("src",)
    assert (Path(first.root_path) / "src").is_dir()
    assert marker_payload["project_id"] == project.project_id
    assert marker_payload["workspace_id"] == project.workspace.workspace_id
    assert marker_payload["control_plane_authority"] == CONTROL_PLANE_AUTHORITY
    assert marker_payload["verification_commands"] == list(MANDATORY_VERIFICATION_COMMANDS)


def test_workspace_marker_serialization_round_trips_without_reordering_commands(
    tmp_path: Path,
) -> None:
    project = service_project(tmp_path)
    manager = GeneratedAppWorkspaceManager(tmp_path / "generated-apps")

    first = manager.prepare_workspace(project)
    first_marker = Path(first.marker_path).read_text(encoding="utf-8")
    first_payload = json.loads(first_marker)
    second = manager.prepare_workspace(project)
    second_marker = Path(second.marker_path).read_text(encoding="utf-8")

    assert first_marker == second_marker
    assert list(first_payload) == sorted(first_payload)
    assert first_payload["state"] == "PREPARED"
    assert first_payload["verification_commands"] == list(MANDATORY_VERIFICATION_COMMANDS)


def test_workspace_manager_reconciles_declared_input_to_prepared_marker(
    tmp_path: Path,
) -> None:
    project = service_project(tmp_path)
    manager = GeneratedAppWorkspaceManager(tmp_path / "generated-apps")

    prepared = manager.prepare_workspace(project)
    repeated = manager.prepare_workspace(project)

    assert project.workspace.state == "DECLARED"
    assert prepared.workspace.state == "PREPARED"
    assert repeated.workspace.state == "PREPARED"
    assert prepared.as_dict() == repeated.as_dict()


def test_workspace_manager_reconciles_prepared_input_to_prepared_marker(
    tmp_path: Path,
) -> None:
    project = service_project(tmp_path)
    manager = GeneratedAppWorkspaceManager(tmp_path / "generated-apps")

    prepared = manager.prepare_workspace(project)
    first_repeat = manager.prepare_workspace(prepared.project)
    second_repeat = manager.prepare_workspace(first_repeat.project)

    assert prepared.as_dict() == first_repeat.as_dict() == second_repeat.as_dict()
    assert second_repeat.project.workspace.state == "PREPARED"


def test_workspace_manager_accepts_owned_declared_marker_and_converges(
    tmp_path: Path,
) -> None:
    project = service_project(tmp_path)
    manager = GeneratedAppWorkspaceManager(tmp_path / "generated-apps")
    prepared = manager.prepare_workspace(project)
    marker_path = Path(prepared.marker_path)
    marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
    marker_payload["state"] = "DECLARED"
    marker_path.write_text(
        json.dumps(marker_payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    reconciled = manager.prepare_workspace(project)
    reconciled_payload = json.loads(marker_path.read_text(encoding="utf-8"))

    assert reconciled.workspace.state == "PREPARED"
    assert reconciled_payload["state"] == "PREPARED"


def test_workspace_manager_rejects_unowned_non_empty_workspace(tmp_path: Path) -> None:
    project = service_project(tmp_path)
    workspace_root = Path(project.workspace.root_path)
    workspace_root.mkdir(parents=True)
    (workspace_root / "README.md").write_text("not owned\n", encoding="utf-8")
    manager = GeneratedAppWorkspaceManager(tmp_path / "generated-apps")

    with pytest.raises(ExternalProjectServiceError, match="not ai-ent-owned"):
        manager.prepare_workspace(project)


def test_workspace_manager_rejects_root_traversal_outside_generated_root(
    tmp_path: Path,
) -> None:
    project = service_project(tmp_path)
    blocked = replace(
        project,
        workspace=replace(
            project.workspace,
            root_path=str(tmp_path / "generated-apps" / ".." / "control-plane"),
        ),
    )
    manager = GeneratedAppWorkspaceManager(tmp_path / "generated-apps")

    with pytest.raises(ExternalProjectServiceError, match="outside generated-app root"):
        manager.prepare_workspace(blocked)


def test_workspace_manager_rejects_control_plane_workspace_root(tmp_path: Path) -> None:
    project = service_project(tmp_path)
    blocked = replace(
        project,
        workspace=replace(
            project.workspace,
            root_path=str(tmp_path / "control-plane" / "generated-app"),
        ),
    )
    manager = GeneratedAppWorkspaceManager(
        tmp_path,
        control_plane_root=tmp_path / "control-plane",
    )

    with pytest.raises(ExternalProjectServiceError, match="control-plane repository"):
        manager.prepare_workspace(blocked)


def test_workspace_manager_rejects_symlink_escape(tmp_path: Path) -> None:
    project = service_project(tmp_path)
    manager = GeneratedAppWorkspaceManager(tmp_path / "generated-apps")
    prepared = manager.prepare_workspace(project)
    workspace_root = Path(prepared.root_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace_root / "src").rmdir()
    (workspace_root / "src").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ExternalProjectServiceError, match="escapes workspace"):
        manager.prepare_workspace(prepared.project)


def test_workspace_manager_rejects_foreign_marker(tmp_path: Path) -> None:
    project = service_project(tmp_path)
    manager = GeneratedAppWorkspaceManager(tmp_path / "generated-apps")
    prepared = manager.prepare_workspace(project)
    marker_path = Path(prepared.marker_path)
    marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
    marker_payload["project_id"] = "EXT-FOREIGN"
    marker_path.write_text(
        json.dumps(marker_payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ExternalProjectServiceError, match="project_id mismatch"):
        manager.prepare_workspace(project)


def test_repository_manager_initializes_local_git_without_control_authority(
    tmp_path: Path,
) -> None:
    project = service_project(tmp_path, remote_url=None)
    manager = GeneratedAppWorkspaceManager(tmp_path / "generated-apps")
    prepared = manager.prepare_workspace(project)

    repository = manager.initialize_repository(prepared.project)
    completed = run_git(["rev-parse", "--is-inside-work-tree"], cwd=Path(repository.root_path))

    assert completed.returncode == 0
    assert completed.stdout.strip() == "true"
    assert repository.repository.state == "INITIALIZED"
    assert repository.repository.control_plane_write_authority is False
    assert repository.repository.push_requires_human_gate is True
    assert repository.remote_name is None
    assert repository.as_dict()["verification_commands"] == list(MANDATORY_VERIFICATION_COMMANDS)


def test_repository_manager_repeated_initialization_is_convergent(tmp_path: Path) -> None:
    project = service_project(tmp_path, remote_url=None)
    manager = GeneratedAppWorkspaceManager(tmp_path / "generated-apps")
    prepared = manager.prepare_workspace(project)

    first = manager.initialize_repository(prepared.project)
    second = manager.initialize_repository(first.project)

    assert first.as_dict() == second.as_dict()
    assert run_git(["rev-parse", "--show-toplevel"], cwd=Path(first.root_path)).stdout.strip() == (
        first.root_path
    )


def test_repository_manager_accepts_existing_matching_local_repository(
    tmp_path: Path,
) -> None:
    project = service_project(tmp_path, remote_url=None)
    manager = GeneratedAppWorkspaceManager(tmp_path / "generated-apps")
    prepared = manager.prepare_workspace(project)
    init = run_git(["init", "-b", project.repository.default_branch], cwd=Path(prepared.root_path))
    assert init.returncode == 0

    repository = manager.initialize_repository(prepared.project)

    assert repository.repository.state == "INITIALIZED"
    assert repository.git_dir == str(Path(prepared.root_path) / ".git")


def test_repository_manager_rejects_foreign_repository_remote(tmp_path: Path) -> None:
    original = service_project(tmp_path, remote_url="https://git.example/one.git")
    manager = GeneratedAppWorkspaceManager(tmp_path / "generated-apps")
    prepared = manager.prepare_workspace(original)
    manager.initialize_repository(prepared.project)
    changed = replace(
        prepared.project,
        repository=replace(
            prepared.project.repository,
            remote_url="https://git.example/two.git",
            state="DECLARED",
        ),
    )

    with pytest.raises(ExternalProjectServiceError, match="repository remote mismatch"):
        manager.initialize_repository(changed)


def test_repository_manager_rejects_secret_remote_without_exposing_value(
    tmp_path: Path,
) -> None:
    project = service_project(
        tmp_path,
        remote_url="https://user:SUPERSECRET@git.example/customer-portal.git",
    )
    manager = GeneratedAppWorkspaceManager(tmp_path / "generated-apps")
    prepared = manager.prepare_workspace(project)

    with pytest.raises(ExternalProjectServiceError) as raised:
        manager.initialize_repository(prepared.project)

    message = str(raised.value)
    assert "https://<redacted>@git.example/customer-portal.git" in message
    assert "SUPERSECRET" not in message


def test_repository_manager_rejects_secret_remote_query_without_exposing_value(
    tmp_path: Path,
) -> None:
    project = service_project(
        tmp_path,
        remote_url="https://git.example/customer-portal.git?token=SUPERSECRET",
    )
    manager = GeneratedAppWorkspaceManager(tmp_path / "generated-apps")
    prepared = manager.prepare_workspace(project)

    with pytest.raises(ExternalProjectServiceError) as raised:
        manager.initialize_repository(prepared.project)

    message = str(raised.value)
    assert "https://git.example/customer-portal.git?<redacted>" in message
    assert "SUPERSECRET" not in message


def test_workspace_manager_preserves_control_plane_authority(tmp_path: Path) -> None:
    project = service_project(tmp_path, remote_url=None)
    blocked = replace(
        project,
        repository=replace(project.repository, control_plane_write_authority=True),
    )
    manager = GeneratedAppWorkspaceManager(tmp_path / "generated-apps")

    with pytest.raises(ExternalProjectServiceError, match="control-plane write authority"):
        manager.prepare_workspace(blocked)


def test_repository_manager_requires_workspace_marker_before_git_init(tmp_path: Path) -> None:
    project = service_project(tmp_path, remote_url=None)
    workspace_root = Path(project.workspace.root_path)
    workspace_root.mkdir(parents=True)
    run_git(["init", "-b", project.repository.default_branch], cwd=workspace_root)
    manager = GeneratedAppWorkspaceManager(tmp_path / "generated-apps")

    with pytest.raises(ExternalProjectServiceError, match="workspace marker missing"):
        manager.initialize_repository(project)


def test_workspace_marker_path_is_scoped_under_workspace(tmp_path: Path) -> None:
    project = service_project(tmp_path)
    manager = GeneratedAppWorkspaceManager(tmp_path / "generated-apps")

    prepared = manager.prepare_workspace(project)
    marker_path = Path(prepared.marker_path)

    assert marker_path == Path(prepared.root_path) / WORKSPACE_MARKER_PATH
    assert marker_path.resolve().is_relative_to(Path(prepared.root_path).resolve())


def frozen_plan_project(
    tmp_path: Path,
    *,
    remote_url: str | None = None,
) -> ExternalProjectFrozenPlan:
    workspace = ProjectWorkspace(
        workspace_id="WS-EXT-RUNTIME",
        root_path=str(tmp_path / "generated-apps" / "runtime-app"),
        owner_project_id="EXT-RUNTIME-APP",
        allowed_roots=("app", "tests"),
    )
    repository = ProjectRepository(
        repository_id="REPO-EXT-RUNTIME",
        workspace_id=workspace.workspace_id,
        remote_url=remote_url,
    )
    tasks = (
        ExternalProjectRuntimeTask(
            id="EXT-TASK-001",
            title="Create application shell",
            objective="Create the external application shell.",
            requirement_ids=("REQ-EXT-001",),
            capability_ids=("C20",),
            component_ids=("CMP-EXT-APP",),
            allowed_paths=("app", "tests"),
            acceptance_criteria=("application shell exists",),
            provenance={"source": "unit-test"},
        ),
        ExternalProjectRuntimeTask(
            id="EXT-TASK-002",
            title="Bind runtime evidence",
            objective="Bind runtime evidence for the generated app.",
            requirement_ids=("REQ-EXT-002",),
            capability_ids=("C20",),
            component_ids=("CMP-EXT-RUNTIME",),
            depends_on=("EXT-TASK-001",),
            allowed_paths=("app", "tests"),
            acceptance_criteria=("runtime evidence is recorded",),
            risk_level="HIGH",
            provenance={"source": "unit-test"},
        ),
    )
    plan = ProjectPlan(
        plan_id="PLAN-EXT-RUNTIME",
        version="1",
        state="FROZEN",
        task_ids=tuple(task.id for task in tasks),
        required_human_gates=("GATE-EXT-TASK-002",),
    )
    binding = ProjectRuntimeBinding(
        binding_id="BIND-EXT-RUNTIME",
        project_id="EXT-RUNTIME-APP",
        workspace_id=workspace.workspace_id,
        repository_id=repository.repository_id,
        plan_id=plan.plan_id,
        plan_version=plan.version,
        state="READY_FOR_IMPORT",
        pending_human_gates=("GATE-EXT-TASK-002",),
    )
    project = ExternalProject(
        project_id="EXT-RUNTIME-APP",
        name="Runtime Import App",
        lifecycle_state="APPROVE",
        workspace=workspace,
        repository=repository,
        plan=plan,
        runtime_binding=binding,
    )
    return ExternalProjectFrozenPlan(
        project=project,
        tasks=tasks,
        human_gate_definitions=(
            {
                "id": "GATE-EXT-TASK-002",
                "task_id": "EXT-TASK-002",
                "reason": "high-risk runtime binding requires explicit approval",
                "required_before": "execution",
            },
        ),
        effective_concurrency=1,
    )


def test_external_frozen_plan_record_is_deterministic_and_import_ready(
    tmp_path: Path,
) -> None:
    first = frozen_plan_project(tmp_path)
    second = frozen_plan_project(tmp_path)

    assert first.as_dict() == second.as_dict()
    assert first.frozen_plan_hash == second.frozen_plan_hash
    assert validate_external_project_frozen_plan(first).ok
    record = external_project_frozen_plan_record(first)
    assert record["validation"]["ok"] is True
    assert record["frozen_plan"]["project"]["repository"]["control_plane_write_authority"] is False
    assert record["frozen_plan"]["project"]["runtime_binding"]["verifier_bypass_authority"] is False
    assert record["frozen_plan"]["project"]["runtime_binding"]["pending_human_gates"] == [
        "GATE-EXT-TASK-002"
    ]


def test_external_frozen_plan_record_loads_as_generic_runtime_contract(
    tmp_path: Path,
) -> None:
    frozen = frozen_plan_project(tmp_path)
    record = external_project_frozen_plan_record(frozen)
    path = tmp_path / "external-frozen-plan.json"
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")

    loaded_from_record = external_project_frozen_plan_from_dict(record)
    loaded_from_file = load_external_project_frozen_plan(path)

    assert loaded_from_record.as_dict() == frozen.as_dict()
    assert loaded_from_file.as_dict() == frozen.as_dict()
    assert validate_external_project_frozen_plan(loaded_from_file).ok


def test_external_frozen_plan_validation_rejects_missing_explicit_gate(
    tmp_path: Path,
) -> None:
    frozen = frozen_plan_project(tmp_path)
    blocked = replace(
        frozen,
        project=replace(
            frozen.project,
            runtime_binding=replace(
                frozen.project.runtime_binding,
                pending_human_gates=(),
                approved_human_gates=(),
            ),
        ),
    )
    validation = validate_external_project_frozen_plan(blocked)

    assert validation.ok is False
    assert "human gate is not explicitly bound: GATE-EXT-TASK-002" in {
        finding.message for finding in validation.findings
    }


def test_external_frozen_plan_validation_rejects_secret_verification_command_without_leak(
    tmp_path: Path,
) -> None:
    frozen = frozen_plan_project(tmp_path)
    secret_command = "python -m pytest --token=SUPERSECRET"
    task = replace(
        frozen.tasks[0],
        verification_commands=(*MANDATORY_VERIFICATION_COMMANDS, secret_command),
    )
    blocked = replace(
        frozen,
        project=replace(
            frozen.project,
            plan=replace(
                frozen.project.plan,
                verification_commands=(*MANDATORY_VERIFICATION_COMMANDS, secret_command),
            ),
            runtime_binding=replace(
                frozen.project.runtime_binding,
                verification_commands=(*MANDATORY_VERIFICATION_COMMANDS, secret_command),
            ),
        ),
        tasks=(task, frozen.tasks[1]),
    )

    validation = validate_external_project_frozen_plan(blocked)
    rendered = json.dumps(validation.as_dict(), sort_keys=True)

    assert validation.ok is False
    assert "verification command contains secret material" in rendered
    assert "SUPERSECRET" not in rendered


def test_external_frozen_plan_validation_redacts_secret_remote(
    tmp_path: Path,
) -> None:
    frozen = frozen_plan_project(
        tmp_path,
        remote_url="https://user:SUPERSECRET@git.example/runtime-app.git",
    )
    validation = validate_external_project_frozen_plan(frozen)
    rendered = json.dumps(validation.as_dict(), sort_keys=True)

    assert validation.ok is False
    assert "https://<redacted>@git.example/runtime-app.git" in rendered
    assert "SUPERSECRET" not in rendered


def test_external_frozen_plan_record_redacts_secret_task_provenance(
    tmp_path: Path,
) -> None:
    frozen = frozen_plan_project(tmp_path)
    task = replace(
        frozen.tasks[0],
        provenance={"api_key": "SUPERSECRET", "source": "unit-test"},
    )
    blocked = replace(frozen, tasks=(task, frozen.tasks[1]))
    record = external_project_frozen_plan_record(blocked)
    rendered = json.dumps(record, sort_keys=True)

    assert record["validation"]["ok"] is False
    assert record["frozen_plan"]["tasks"][0]["provenance"]["api_key"] == "<redacted>"
    assert "SUPERSECRET" not in rendered
