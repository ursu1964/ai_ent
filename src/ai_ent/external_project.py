from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from ai_ent.project_manifest import canonical_bytes

EXTERNAL_PROJECT_MODEL_SCHEMA_VERSION = "ai-ent-external-project-model-v0.1"
EXTERNAL_PROJECT_MODEL_CONTRACT_VERSION = "prd-task-001.1"

ExternalProjectLifecycleState = Literal[
    "CREATE",
    "INTAKE",
    "PLAN",
    "FREEZE",
    "APPROVE",
    "IMPORT",
    "EXECUTE",
    "VERIFY",
    "COMPLETE",
    "RUN",
    "ARCHIVE",
]
WorkspaceState = Literal["DECLARED", "PREPARED", "ARCHIVED"]
RepositoryState = Literal["DECLARED", "INITIALIZED", "BOUND", "ARCHIVED"]
PlanState = Literal["DRAFT", "FROZEN", "IMPORTED", "INVALIDATED"]
BindingState = Literal["PENDING_GATES", "READY_FOR_IMPORT", "IMPORTED", "EXECUTING", "COMPLETE"]
ArtifactKind = Literal["source", "documentation", "log", "package", "evidence", "runtime_config"]
ArtifactAuthorityState = Literal["NON_AUTHORITATIVE", "VALIDATED"]
ExternalProjectFindingSeverity = Literal["ERROR"]
ExternalFrozenPlanFindingSeverity = Literal["ERROR"]

EXTERNAL_PROJECT_LIFECYCLE: tuple[ExternalProjectLifecycleState, ...] = (
    "CREATE",
    "INTAKE",
    "PLAN",
    "FREEZE",
    "APPROVE",
    "IMPORT",
    "EXECUTE",
    "VERIFY",
    "COMPLETE",
    "RUN",
    "ARCHIVE",
)
CONTROL_PLANE_AUTHORITY = "existing_ai_enterprise_control_plane"
MANDATORY_VERIFICATION_COMMANDS = (
    "/home/user/projects/ai_ent/aient/bin/python -m pytest -q",
    "/home/user/projects/ai_ent/aient/bin/python -m ruff check .",
    "/home/user/projects/ai_ent/aient/bin/python -m pyright",
)
DEFAULT_PROHIBITED_PATHS = (".bootstrap/**", ".build/**", ".env", ".env.*", "aient/**")
SECRET_FIELD_MARKERS = ("secret", "token", "password", "credential", "private_key", "api_key")
WORKSPACE_MARKER_PATH = ".ai-enterprise/workspace.json"
GitCommandRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]


class ExternalProjectServiceError(RuntimeError):
    """Raised when generated-app workspace or repository policy is violated."""


@dataclass(frozen=True)
class ProjectWorkspace:
    workspace_id: str
    root_path: str
    state: WorkspaceState = "DECLARED"
    owner_project_id: str | None = None
    allowed_roots: tuple[str, ...] = ()
    prohibited_paths: tuple[str, ...] = DEFAULT_PROHIBITED_PATHS

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "root_path": self.root_path,
            "state": self.state,
            "owner_project_id": self.owner_project_id,
            "allowed_roots": list(self.allowed_roots),
            "prohibited_paths": list(self.prohibited_paths),
        }


@dataclass(frozen=True)
class ProjectRepository:
    repository_id: str
    workspace_id: str
    remote_url: str | None = None
    default_branch: str = "main"
    state: RepositoryState = "DECLARED"
    commit_policy: str = "verified_commits_only"
    control_plane_write_authority: bool = False
    push_requires_human_gate: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "workspace_id": self.workspace_id,
            "remote_url": _redact_url(self.remote_url),
            "default_branch": self.default_branch,
            "state": self.state,
            "commit_policy": self.commit_policy,
            "control_plane_write_authority": self.control_plane_write_authority,
            "push_requires_human_gate": self.push_requires_human_gate,
        }


@dataclass(frozen=True)
class ProjectPlan:
    plan_id: str
    version: str
    state: PlanState = "DRAFT"
    task_ids: tuple[str, ...] = ()
    plan_hash: str | None = None
    required_human_gates: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = MANDATORY_VERIFICATION_COMMANDS
    independent_verification_required: bool = True
    control_plane_authority: str = CONTROL_PLANE_AUTHORITY

    @property
    def effective_plan_hash(self) -> str:
        if self.plan_hash is not None:
            return self.plan_hash
        payload = {
            "control_plane_authority": self.control_plane_authority,
            "independent_verification_required": self.independent_verification_required,
            "plan_id": self.plan_id,
            "required_human_gates": sorted(self.required_human_gates),
            "state": self.state,
            "task_ids": sorted(self.task_ids),
            "verification_commands": list(self.verification_commands),
            "version": self.version,
        }
        return _hash(payload)

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "version": self.version,
            "state": self.state,
            "task_ids": list(self.task_ids),
            "plan_hash": self.effective_plan_hash,
            "required_human_gates": list(self.required_human_gates),
            "verification_commands": list(self.verification_commands),
            "independent_verification_required": self.independent_verification_required,
            "control_plane_authority": self.control_plane_authority,
        }


@dataclass(frozen=True)
class ProjectRuntimeBinding:
    binding_id: str
    project_id: str
    workspace_id: str
    repository_id: str
    plan_id: str
    plan_version: str
    state: BindingState = "PENDING_GATES"
    pending_human_gates: tuple[str, ...] = ()
    approved_human_gates: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = MANDATORY_VERIFICATION_COMMANDS
    control_plane_authority: str = CONTROL_PLANE_AUTHORITY
    grants_control_plane_authority: bool = False
    verifier_bypass_authority: bool = False
    implicit_human_gate_approval: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "project_id": self.project_id,
            "workspace_id": self.workspace_id,
            "repository_id": self.repository_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "state": self.state,
            "pending_human_gates": list(self.pending_human_gates),
            "approved_human_gates": list(self.approved_human_gates),
            "verification_commands": list(self.verification_commands),
            "control_plane_authority": self.control_plane_authority,
            "grants_control_plane_authority": self.grants_control_plane_authority,
            "verifier_bypass_authority": self.verifier_bypass_authority,
            "implicit_human_gate_approval": self.implicit_human_gate_approval,
        }


@dataclass(frozen=True)
class GeneratedArtifact:
    artifact_id: str
    project_id: str
    workspace_id: str
    kind: ArtifactKind
    relative_path: str
    content_hash: str
    producer: str = "external_project_runtime"
    authority_state: ArtifactAuthorityState = "NON_AUTHORITATIVE"
    contains_secret: bool = False
    metadata: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "project_id": self.project_id,
            "workspace_id": self.workspace_id,
            "kind": self.kind,
            "relative_path": self.relative_path,
            "content_hash": self.content_hash,
            "producer": self.producer,
            "authority_state": self.authority_state,
            "contains_secret": self.contains_secret,
            "metadata": _redact_mapping(self.metadata or {}),
        }


@dataclass(frozen=True)
class ExternalProjectRuntimeTask:
    id: str
    title: str
    objective: str
    requirement_ids: tuple[str, ...]
    capability_ids: tuple[str, ...]
    component_ids: tuple[str, ...] = ()
    interface_ids: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    prohibited_paths: tuple[str, ...] = DEFAULT_PROHIBITED_PATHS
    agent_role: str = "external-project-implementation"
    model_profile: str = "STANDARD_CODING"
    executor: str = "codex"
    verification_profile: str = "STANDARD_REGRESSION"
    verification_commands: tuple[str, ...] = MANDATORY_VERIFICATION_COMMANDS
    acceptance_criteria: tuple[str, ...] = ()
    risk_level: str = "LOW"
    risk_reason: str = "bounded external-project runtime task"
    provenance: dict[str, str] = field(default_factory=dict)
    fingerprint: str | None = None

    @property
    def effective_fingerprint(self) -> str:
        if self.fingerprint is not None:
            return self.fingerprint
        return _hash(self._payload(include_fingerprint=False))

    def as_dict(self) -> dict[str, Any]:
        return self._payload(include_fingerprint=True)

    def _payload(self, *, include_fingerprint: bool) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "title": self.title,
            "objective": self.objective,
            "implements": {
                "requirements": sorted(self.requirement_ids),
                "capabilities": sorted(self.capability_ids),
                "components": sorted(self.component_ids),
                "interfaces": sorted(self.interface_ids),
            },
            "depends_on": sorted(self.depends_on),
            "write_scope": {
                "allowed": sorted(self.allowed_paths),
                "prohibited": sorted(self.prohibited_paths),
            },
            "execution": {
                "agent_role": self.agent_role,
                "model_profile": self.model_profile,
                "executor": self.executor,
            },
            "verification": {
                "profile": self.verification_profile,
                "commands": list(self.verification_commands),
                "acceptance_criteria": list(self.acceptance_criteria),
            },
            "risk": {
                "level": self.risk_level,
                "reason": self.risk_reason,
            },
            "provenance": _redact_mapping(dict(self.provenance)),
        }
        if include_fingerprint:
            payload["fingerprint"] = self.effective_fingerprint
        return payload


@dataclass(frozen=True)
class ExternalProjectFrozenPlan:
    project: ExternalProject
    tasks: tuple[ExternalProjectRuntimeTask, ...]
    human_gate_definitions: tuple[dict[str, Any], ...] = ()
    effective_concurrency: int = 1

    @property
    def task_ids(self) -> tuple[str, ...]:
        return tuple(sorted(task.id for task in self.tasks))

    @property
    def dependency_edges(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            sorted(
                (task.id, dependency)
                for task in self.tasks
                for dependency in task.depends_on
            )
        )

    @property
    def frozen_plan_hash(self) -> str:
        return _hash(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "frozen_plan_hash": self.frozen_plan_hash}

    def _payload(self) -> dict[str, Any]:
        return {
            "record_kind": "external_project_frozen_plan",
            "schema_version": self.project.schema_version,
            "contract_version": self.project.contract_version,
            "project": self.project.as_dict(),
            "tasks": [
                task.as_dict()
                for task in sorted(self.tasks, key=lambda item: item.id)
            ],
            "dependency_edges": [list(edge) for edge in self.dependency_edges],
            "human_gate_definitions": [
                _redact_value(gate)
                for gate in sorted(
                    self.human_gate_definitions,
                    key=lambda item: str(item.get("id", "")),
                )
            ],
            "effective_concurrency": self.effective_concurrency,
        }


@dataclass(frozen=True)
class ExternalProject:
    project_id: str
    name: str
    lifecycle_state: ExternalProjectLifecycleState
    workspace: ProjectWorkspace
    repository: ProjectRepository
    plan: ProjectPlan
    runtime_binding: ProjectRuntimeBinding
    artifacts: tuple[GeneratedArtifact, ...] = ()
    schema_version: str = EXTERNAL_PROJECT_MODEL_SCHEMA_VERSION
    contract_version: str = EXTERNAL_PROJECT_MODEL_CONTRACT_VERSION

    @property
    def model_hash(self) -> str:
        return _hash(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "model_hash": self.model_hash}

    def as_storage_records(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_version": self.contract_version,
            "project_id": self.project_id,
            "model_hash": self.model_hash,
            "external_project": self.as_dict(),
            "project_workspace": self.workspace.as_dict(),
            "project_repository": self.repository.as_dict(),
            "project_plan": self.plan.as_dict(),
            "project_runtime_binding": self.runtime_binding.as_dict(),
            "generated_artifacts": [artifact.as_dict() for artifact in self.artifacts],
        }

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_version": self.contract_version,
            "project_id": self.project_id,
            "name": self.name,
            "lifecycle_state": self.lifecycle_state,
            "workspace": self.workspace.as_dict(),
            "repository": self.repository.as_dict(),
            "plan": self.plan.as_dict(),
            "runtime_binding": self.runtime_binding.as_dict(),
            "artifacts": [artifact.as_dict() for artifact in self.artifacts],
        }


@dataclass(frozen=True)
class WorkspaceProvisioningRecord:
    project: ExternalProject
    workspace: ProjectWorkspace
    root_path: str
    marker_path: str
    prepared_paths: tuple[str, ...]
    scope_policy_hash: str

    @property
    def record_hash(self) -> str:
        return _hash(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "record_hash": self.record_hash}

    def _payload(self) -> dict[str, Any]:
        return {
            "record_kind": "generated_app_workspace_provisioning",
            "project_id": self.project.project_id,
            "workspace": self.workspace.as_dict(),
            "root_path": self.root_path,
            "marker_path": self.marker_path,
            "prepared_paths": list(self.prepared_paths),
            "scope_policy_hash": self.scope_policy_hash,
            "control_plane_authority": self.project.runtime_binding.control_plane_authority,
            "verification_commands": list(MANDATORY_VERIFICATION_COMMANDS),
        }


@dataclass(frozen=True)
class RepositoryProvisioningRecord:
    project: ExternalProject
    repository: ProjectRepository
    root_path: str
    git_dir: str
    remote_name: str | None = None

    @property
    def record_hash(self) -> str:
        return _hash(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "record_hash": self.record_hash}

    def _payload(self) -> dict[str, Any]:
        return {
            "record_kind": "generated_app_repository_provisioning",
            "project_id": self.project.project_id,
            "repository": self.repository.as_dict(),
            "root_path": self.root_path,
            "git_dir": self.git_dir,
            "remote_name": self.remote_name,
            "commit_policy": self.repository.commit_policy,
            "human_gate_required_for_push": self.repository.push_requires_human_gate,
            "control_plane_write_authority": self.repository.control_plane_write_authority,
            "verification_commands": list(MANDATORY_VERIFICATION_COMMANDS),
        }


class GeneratedAppWorkspaceManager:
    def __init__(
        self,
        generated_apps_root: Path,
        *,
        control_plane_root: Path | None = None,
        git_runner: GitCommandRunner | None = None,
    ) -> None:
        self.generated_apps_root = generated_apps_root.resolve()
        self.control_plane_root = control_plane_root.resolve() if control_plane_root else None
        self._git_runner = git_runner

    def prepare_workspace(self, project: ExternalProject) -> WorkspaceProvisioningRecord:
        _require_service_safe_project(project)
        workspace_root = self._resolve_workspace_root(project)
        _require_safe_workspace_target(workspace_root, self.control_plane_root)
        self._require_owned_or_empty_workspace(project, workspace_root)

        workspace_root.mkdir(parents=True, exist_ok=True)
        prepared_paths = tuple(
            sorted({_normalize_relative_path(path) for path in project.workspace.allowed_roots})
        )
        for relative_path in prepared_paths:
            target = (workspace_root / relative_path).resolve()
            if not target.is_relative_to(workspace_root):
                raise ExternalProjectServiceError(
                    f"workspace allowed root escapes workspace: {relative_path}"
                )
            target.mkdir(parents=True, exist_ok=True)

        prepared_workspace = replace(project.workspace, state="PREPARED")
        prepared_project = replace(project, workspace=prepared_workspace)
        marker_path = workspace_root / WORKSPACE_MARKER_PATH
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_bytes(_deterministic_marker_bytes(prepared_project, workspace_root))
        return WorkspaceProvisioningRecord(
            project=prepared_project,
            workspace=prepared_workspace,
            root_path=str(workspace_root),
            marker_path=str(marker_path),
            prepared_paths=prepared_paths,
            scope_policy_hash=_scope_policy_hash(project),
        )

    def initialize_repository(self, project: ExternalProject) -> RepositoryProvisioningRecord:
        _require_service_safe_project(project)
        if project.repository.remote_url and _remote_url_has_secret_material(
            project.repository.remote_url
        ):
            raise ExternalProjectServiceError(
                "repository remote contains secret-bearing material: "
                f"{_redact_remote_url_for_error(project.repository.remote_url)}"
            )

        workspace_root = self._resolve_workspace_root(project)
        _require_safe_workspace_target(workspace_root, self.control_plane_root)
        marker = self._require_workspace_marker(project, workspace_root)
        git_dir = workspace_root / ".git"
        if git_dir.exists() and not git_dir.is_dir():
            raise ExternalProjectServiceError(
                f"repository git path is not a directory: {git_dir}"
            )
        if git_dir.exists():
            self._require_matching_git_repository(workspace_root)
        else:
            self._require_git(["init", "-b", project.repository.default_branch], cwd=workspace_root)

        remote_name: str | None = None
        if project.repository.remote_url:
            remote_name = "origin"
            self._ensure_remote(workspace_root, remote_name, project.repository.remote_url)

        repository_state: RepositoryState = (
            "BOUND" if project.repository.remote_url else "INITIALIZED"
        )
        repository = replace(project.repository, state=repository_state)
        provisioned_project = replace(project, workspace=marker, repository=repository)
        return RepositoryProvisioningRecord(
            project=provisioned_project,
            repository=repository,
            root_path=str(workspace_root),
            git_dir=str(git_dir),
            remote_name=remote_name,
        )

    def _resolve_workspace_root(self, project: ExternalProject) -> Path:
        workspace_root = Path(project.workspace.root_path).resolve()
        if not workspace_root.is_relative_to(self.generated_apps_root):
            raise ExternalProjectServiceError(
                "workspace root is outside generated-app root: "
                f"{workspace_root} not under {self.generated_apps_root}"
            )
        return workspace_root

    def _require_owned_or_empty_workspace(
        self,
        project: ExternalProject,
        workspace_root: Path,
    ) -> None:
        if not workspace_root.exists():
            return
        if not workspace_root.is_dir():
            raise ExternalProjectServiceError(
                f"workspace root is not a directory: {workspace_root}"
            )
        marker = workspace_root / WORKSPACE_MARKER_PATH
        if marker.exists():
            self._read_workspace_marker(project, workspace_root)
            return
        if any(workspace_root.iterdir()):
            raise ExternalProjectServiceError(
                f"existing workspace is not ai-ent-owned: {workspace_root}"
            )

    def _require_workspace_marker(
        self,
        project: ExternalProject,
        workspace_root: Path,
    ) -> ProjectWorkspace:
        if not workspace_root.exists() or not workspace_root.is_dir():
            raise ExternalProjectServiceError(f"workspace has not been prepared: {workspace_root}")
        return self._read_workspace_marker(project, workspace_root)

    def _read_workspace_marker(
        self,
        project: ExternalProject,
        workspace_root: Path,
    ) -> ProjectWorkspace:
        marker = workspace_root / WORKSPACE_MARKER_PATH
        if not marker.exists():
            raise ExternalProjectServiceError(f"workspace marker missing: {marker}")
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ExternalProjectServiceError(
                f"workspace marker is not valid JSON: {marker}"
            ) from exc
        expected = _workspace_marker_payload(
            replace(project, workspace=replace(project.workspace, state="PREPARED")),
            workspace_root,
        )
        _require_marker_value(payload, "marker_kind", expected["marker_kind"])
        _require_marker_value(payload, "schema_version", expected["schema_version"])
        _require_marker_value(payload, "contract_version", expected["contract_version"])
        _require_marker_value(payload, "project_id", expected["project_id"])
        _require_marker_value(payload, "workspace_id", expected["workspace_id"])
        _require_marker_value(payload, "root_path", expected["root_path"])
        _require_marker_value(payload, "repository_id", expected["repository_id"])
        _require_marker_value(payload, "plan_id", expected["plan_id"])
        _require_marker_value(payload, "plan_hash", expected["plan_hash"])
        _require_marker_value(payload, "control_plane_authority", expected["control_plane_authority"])
        _require_marker_value(
            payload,
            "push_requires_human_gate",
            expected["push_requires_human_gate"],
        )
        _require_marker_sequence(payload, "allowed_roots", expected["allowed_roots"])
        _require_marker_sequence(payload, "prohibited_paths", expected["prohibited_paths"])
        _require_marker_sequence(
            payload,
            "verification_commands",
            expected["verification_commands"],
        )
        state = payload.get("state")
        if state not in {"DECLARED", "PREPARED"}:
            raise ExternalProjectServiceError("workspace marker state mismatch")
        return replace(project.workspace, state="PREPARED")

    def _require_matching_git_repository(self, workspace_root: Path) -> None:
        top_level = self._require_git(["rev-parse", "--show-toplevel"], cwd=workspace_root)
        if Path(top_level).resolve() != workspace_root:
            raise ExternalProjectServiceError("repository root mismatch")

    def _ensure_remote(self, cwd: Path, name: str, url: str) -> None:
        existing = self._run_git(["remote", "get-url", name], cwd=cwd)
        if existing.returncode == 0:
            configured_url = existing.stdout.strip()
            if configured_url != url:
                raise ExternalProjectServiceError(
                    "repository remote mismatch: "
                    f"{_redact_remote_url_for_error(configured_url)} != "
                    f"{_redact_remote_url_for_error(url)}"
                )
            return
        self._require_git(["remote", "add", name, url], cwd=cwd)

    def _require_git(self, args: list[str], *, cwd: Path) -> str:
        completed = self._run_git(args, cwd=cwd)
        if completed.returncode != 0:
            output = completed.stdout.strip()
            raise ExternalProjectServiceError(output or f"git {' '.join(args)} failed")
        return completed.stdout.strip()

    def _run_git(self, args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        if self._git_runner is not None:
            return self._git_runner(args, cwd)
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )


@dataclass(frozen=True)
class ExternalProjectModelFinding:
    finding_id: str
    severity: ExternalProjectFindingSeverity
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass(frozen=True)
class ExternalProjectModelValidation:
    project_id: str
    model_hash: str
    ok: bool
    findings: tuple[ExternalProjectModelFinding, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "model_hash": self.model_hash,
            "ok": self.ok,
            "findings": [finding.as_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class ExternalProjectFrozenPlanFinding:
    finding_id: str
    severity: ExternalFrozenPlanFindingSeverity
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass(frozen=True)
class ExternalProjectFrozenPlanValidation:
    project_id: str
    frozen_plan_hash: str
    ok: bool
    findings: tuple[ExternalProjectFrozenPlanFinding, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "frozen_plan_hash": self.frozen_plan_hash,
            "ok": self.ok,
            "findings": [finding.as_dict() for finding in self.findings],
        }


def validate_external_project(project: ExternalProject) -> ExternalProjectModelValidation:
    findings: list[ExternalProjectModelFinding] = []
    _validate_identity_bindings(project, findings)
    _validate_scope_policy(project, findings)
    _validate_control_plane_authority(project, findings)
    _validate_human_gates(project, findings)
    _validate_verification(project, findings)
    _validate_artifacts(project, findings)
    sorted_findings = tuple(sorted(findings, key=lambda item: item.finding_id))
    return ExternalProjectModelValidation(
        project_id=project.project_id,
        model_hash=project.model_hash,
        ok=not sorted_findings,
        findings=sorted_findings,
    )


def validate_external_project_frozen_plan(
    frozen_plan: ExternalProjectFrozenPlan,
) -> ExternalProjectFrozenPlanValidation:
    findings: list[ExternalProjectFrozenPlanFinding] = []
    project = frozen_plan.project
    project_validation = validate_external_project(project)
    findings.extend(
        ExternalProjectFrozenPlanFinding(
            finding_id=f"EFP-MODEL-{finding.finding_id}",
            severity="ERROR",
            message=finding.message,
        )
        for finding in project_validation.findings
    )
    _validate_external_project_import_state(frozen_plan, findings)
    _validate_external_project_task_bindings(frozen_plan, findings)
    _validate_external_project_gate_definitions(frozen_plan, findings)
    _validate_external_project_runtime_scope(frozen_plan, findings)
    _validate_external_project_secret_surfaces(frozen_plan, findings)
    sorted_findings = tuple(sorted(findings, key=lambda item: item.finding_id))
    return ExternalProjectFrozenPlanValidation(
        project_id=project.project_id,
        frozen_plan_hash=frozen_plan.frozen_plan_hash,
        ok=not sorted_findings,
        findings=sorted_findings,
    )


def external_project_model_record(project: ExternalProject) -> dict[str, Any]:
    validation = validate_external_project(project)
    return {
        "record_kind": "external_project_model",
        "project": project.as_dict(),
        "validation": validation.as_dict(),
    }


def external_project_frozen_plan_record(
    frozen_plan: ExternalProjectFrozenPlan,
) -> dict[str, Any]:
    validation = validate_external_project_frozen_plan(frozen_plan)
    return {
        "record_kind": "external_project_frozen_plan",
        "frozen_plan": frozen_plan.as_dict(),
        "validation": validation.as_dict(),
    }


def load_external_project_frozen_plan(path: Path) -> ExternalProjectFrozenPlan:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return external_project_frozen_plan_from_dict(
        _require_mapping(payload, "external frozen plan file")
    )


def external_project_frozen_plan_from_dict(
    payload: Mapping[str, Any],
) -> ExternalProjectFrozenPlan:
    raw_plan = payload.get("frozen_plan", payload)
    plan_payload = _require_mapping(raw_plan, "external frozen plan")
    project = _external_project_from_dict(
        _require_mapping(plan_payload.get("project"), "external project")
    )
    tasks = tuple(
        _external_project_runtime_task_from_dict(item)
        for item in _require_mapping_sequence(plan_payload.get("tasks"), "runtime tasks")
    )
    human_gate_definitions = tuple(
        dict(_require_mapping(item, "human gate definition"))
        for item in _optional_mapping_sequence(
            plan_payload.get("human_gate_definitions", ()),
            "human gate definitions",
        )
    )
    return ExternalProjectFrozenPlan(
        project=project,
        tasks=tasks,
        human_gate_definitions=human_gate_definitions,
        effective_concurrency=_optional_int(
            plan_payload,
            "effective_concurrency",
            default=1,
        ),
    )


def _external_project_from_dict(payload: Mapping[str, Any]) -> ExternalProject:
    workspace = _project_workspace_from_dict(
        _require_mapping(payload.get("workspace"), "project workspace")
    )
    repository = _project_repository_from_dict(
        _require_mapping(payload.get("repository"), "project repository")
    )
    plan = _project_plan_from_dict(_require_mapping(payload.get("plan"), "project plan"))
    runtime_binding = _project_runtime_binding_from_dict(
        _require_mapping(payload.get("runtime_binding"), "project runtime binding")
    )
    artifacts = tuple(
        _generated_artifact_from_dict(item)
        for item in _optional_mapping_sequence(payload.get("artifacts", ()), "generated artifacts")
    )
    return ExternalProject(
        project_id=_required_str(payload, "project_id"),
        name=_required_str(payload, "name"),
        lifecycle_state=cast(
            ExternalProjectLifecycleState,
            _required_str(payload, "lifecycle_state"),
        ),
        workspace=workspace,
        repository=repository,
        plan=plan,
        runtime_binding=runtime_binding,
        artifacts=artifacts,
        schema_version=_optional_str(
            payload,
            "schema_version",
            default=EXTERNAL_PROJECT_MODEL_SCHEMA_VERSION,
        ),
        contract_version=_optional_str(
            payload,
            "contract_version",
            default=EXTERNAL_PROJECT_MODEL_CONTRACT_VERSION,
        ),
    )


def _project_workspace_from_dict(payload: Mapping[str, Any]) -> ProjectWorkspace:
    return ProjectWorkspace(
        workspace_id=_required_str(payload, "workspace_id"),
        root_path=_required_str(payload, "root_path"),
        state=cast(WorkspaceState, _optional_str(payload, "state", default="DECLARED")),
        owner_project_id=_optional_nullable_str(payload, "owner_project_id"),
        allowed_roots=_optional_str_tuple(payload, "allowed_roots"),
        prohibited_paths=_optional_str_tuple(
            payload,
            "prohibited_paths",
            default=DEFAULT_PROHIBITED_PATHS,
        ),
    )


def _project_repository_from_dict(payload: Mapping[str, Any]) -> ProjectRepository:
    return ProjectRepository(
        repository_id=_required_str(payload, "repository_id"),
        workspace_id=_required_str(payload, "workspace_id"),
        remote_url=_optional_nullable_str(payload, "remote_url"),
        default_branch=_optional_str(payload, "default_branch", default="main"),
        state=cast(RepositoryState, _optional_str(payload, "state", default="DECLARED")),
        commit_policy=_optional_str(payload, "commit_policy", default="verified_commits_only"),
        control_plane_write_authority=_optional_bool(
            payload,
            "control_plane_write_authority",
            default=False,
        ),
        push_requires_human_gate=_optional_bool(
            payload,
            "push_requires_human_gate",
            default=True,
        ),
    )


def _project_plan_from_dict(payload: Mapping[str, Any]) -> ProjectPlan:
    return ProjectPlan(
        plan_id=_required_str(payload, "plan_id"),
        version=_required_str(payload, "version"),
        state=cast(PlanState, _optional_str(payload, "state", default="DRAFT")),
        task_ids=_optional_str_tuple(payload, "task_ids"),
        plan_hash=_optional_nullable_str(payload, "plan_hash"),
        required_human_gates=_optional_str_tuple(payload, "required_human_gates"),
        verification_commands=_optional_str_tuple(
            payload,
            "verification_commands",
            default=MANDATORY_VERIFICATION_COMMANDS,
        ),
        independent_verification_required=_optional_bool(
            payload,
            "independent_verification_required",
            default=True,
        ),
        control_plane_authority=_optional_str(
            payload,
            "control_plane_authority",
            default=CONTROL_PLANE_AUTHORITY,
        ),
    )


def _project_runtime_binding_from_dict(payload: Mapping[str, Any]) -> ProjectRuntimeBinding:
    return ProjectRuntimeBinding(
        binding_id=_required_str(payload, "binding_id"),
        project_id=_required_str(payload, "project_id"),
        workspace_id=_required_str(payload, "workspace_id"),
        repository_id=_required_str(payload, "repository_id"),
        plan_id=_required_str(payload, "plan_id"),
        plan_version=_required_str(payload, "plan_version"),
        state=cast(
            BindingState,
            _optional_str(payload, "state", default="PENDING_GATES"),
        ),
        pending_human_gates=_optional_str_tuple(payload, "pending_human_gates"),
        approved_human_gates=_optional_str_tuple(payload, "approved_human_gates"),
        verification_commands=_optional_str_tuple(
            payload,
            "verification_commands",
            default=MANDATORY_VERIFICATION_COMMANDS,
        ),
        control_plane_authority=_optional_str(
            payload,
            "control_plane_authority",
            default=CONTROL_PLANE_AUTHORITY,
        ),
        grants_control_plane_authority=_optional_bool(
            payload,
            "grants_control_plane_authority",
            default=False,
        ),
        verifier_bypass_authority=_optional_bool(
            payload,
            "verifier_bypass_authority",
            default=False,
        ),
        implicit_human_gate_approval=_optional_bool(
            payload,
            "implicit_human_gate_approval",
            default=False,
        ),
    )


def _generated_artifact_from_dict(payload: Mapping[str, Any]) -> GeneratedArtifact:
    metadata = payload.get("metadata")
    return GeneratedArtifact(
        artifact_id=_required_str(payload, "artifact_id"),
        project_id=_required_str(payload, "project_id"),
        workspace_id=_required_str(payload, "workspace_id"),
        kind=cast(ArtifactKind, _required_str(payload, "kind")),
        relative_path=_required_str(payload, "relative_path"),
        content_hash=_required_str(payload, "content_hash"),
        producer=_optional_str(payload, "producer", default="external_project_runtime"),
        authority_state=cast(
            ArtifactAuthorityState,
            _optional_str(
                payload,
                "authority_state",
                default="NON_AUTHORITATIVE",
            ),
        ),
        contains_secret=_optional_bool(payload, "contains_secret", default=False),
        metadata=(
            dict(_require_mapping(metadata, "artifact metadata"))
            if metadata is not None
            else None
        ),
    )


def _external_project_runtime_task_from_dict(
    payload: Mapping[str, Any],
) -> ExternalProjectRuntimeTask:
    implements = _require_mapping(payload.get("implements"), "runtime task implements")
    write_scope = _require_mapping(payload.get("write_scope"), "runtime task write scope")
    execution = _require_mapping(payload.get("execution"), "runtime task execution")
    verification = _require_mapping(payload.get("verification"), "runtime task verification")
    risk = _require_mapping(payload.get("risk"), "runtime task risk")
    return ExternalProjectRuntimeTask(
        id=_required_str(payload, "id"),
        title=_required_str(payload, "title"),
        objective=_required_str(payload, "objective"),
        requirement_ids=_required_nested_str_tuple(implements, "requirements"),
        capability_ids=_required_nested_str_tuple(implements, "capabilities"),
        component_ids=_optional_str_tuple(implements, "components"),
        interface_ids=_optional_str_tuple(implements, "interfaces"),
        depends_on=_optional_str_tuple(payload, "depends_on"),
        allowed_paths=_required_nested_str_tuple(write_scope, "allowed"),
        prohibited_paths=_optional_str_tuple(
            write_scope,
            "prohibited",
            default=DEFAULT_PROHIBITED_PATHS,
        ),
        agent_role=_optional_str(
            execution,
            "agent_role",
            default="external-project-implementation",
        ),
        model_profile=_optional_str(execution, "model_profile", default="STANDARD_CODING"),
        executor=_optional_str(execution, "executor", default="codex"),
        verification_profile=_optional_str(
            verification,
            "profile",
            default="STANDARD_REGRESSION",
        ),
        verification_commands=_optional_str_tuple(
            verification,
            "commands",
            default=MANDATORY_VERIFICATION_COMMANDS,
        ),
        acceptance_criteria=_optional_str_tuple(verification, "acceptance_criteria"),
        risk_level=_optional_str(risk, "level", default="LOW"),
        risk_reason=_optional_str(
            risk,
            "reason",
            default="bounded external-project runtime task",
        ),
        provenance=dict(
            _require_mapping(payload.get("provenance", {}), "runtime task provenance")
        ),
        fingerprint=_optional_nullable_str(payload, "fingerprint"),
    )


def _require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExternalProjectServiceError(f"{context} must be an object")
    return value


def _require_mapping_sequence(value: Any, context: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list | tuple):
        raise ExternalProjectServiceError(f"{context} must be a list")
    return tuple(_require_mapping(item, context) for item in value)


def _optional_mapping_sequence(value: Any, context: str) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    return _require_mapping_sequence(value, context)


def _required_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ExternalProjectServiceError(f"{key} must be a non-empty string")
    return value


def _optional_str(payload: Mapping[str, Any], key: str, *, default: str) -> str:
    value = payload.get(key, default)
    if not isinstance(value, str) or not value:
        raise ExternalProjectServiceError(f"{key} must be a non-empty string")
    return value


def _optional_nullable_str(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExternalProjectServiceError(f"{key} must be a string")
    return value


def _required_nested_str_tuple(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list | tuple) or not all(isinstance(item, str) for item in value):
        raise ExternalProjectServiceError(f"{key} must be a list of strings")
    return tuple(str(item) for item in value)


def _optional_str_tuple(
    payload: Mapping[str, Any],
    key: str,
    *,
    default: tuple[str, ...] = (),
) -> tuple[str, ...]:
    value = payload.get(key, default)
    if not isinstance(value, list | tuple) or not all(isinstance(item, str) for item in value):
        raise ExternalProjectServiceError(f"{key} must be a list of strings")
    return tuple(str(item) for item in value)


def _optional_bool(payload: Mapping[str, Any], key: str, *, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ExternalProjectServiceError(f"{key} must be a boolean")
    return value


def _optional_int(payload: Mapping[str, Any], key: str, *, default: int) -> int:
    value = payload.get(key, default)
    if not isinstance(value, int):
        raise ExternalProjectServiceError(f"{key} must be an integer")
    return value


def _require_service_safe_project(project: ExternalProject) -> None:
    validation = validate_external_project(project)
    if not validation.ok:
        messages = "; ".join(
            f"{finding.finding_id}: {finding.message}" for finding in validation.findings
        )
        raise ExternalProjectServiceError(f"external project model invalid: {messages}")
    for artifact in project.artifacts:
        if not _path_in_allowed_roots(artifact.relative_path, project.workspace.allowed_roots):
            raise ExternalProjectServiceError(
                "artifact path is outside workspace allowed roots: "
                f"{artifact.artifact_id}"
            )


def _validate_external_project_import_state(
    frozen_plan: ExternalProjectFrozenPlan,
    findings: list[ExternalProjectFrozenPlanFinding],
) -> None:
    project = frozen_plan.project
    if project.plan.state != "FROZEN":
        _append_frozen(
            finding_id="EFP-PLAN-NOT-FROZEN",
            message=f"external project plan state is {project.plan.state}",
            findings=findings,
        )
    if project.runtime_binding.state != "READY_FOR_IMPORT":
        _append_frozen(
            finding_id="EFP-BINDING-NOT-READY",
            message=f"runtime binding state is {project.runtime_binding.state}",
            findings=findings,
        )
    if frozen_plan.effective_concurrency < 1:
        _append_frozen(
            finding_id="EFP-CONCURRENCY",
            message="effective concurrency must be at least 1",
            findings=findings,
        )


def _validate_external_project_task_bindings(
    frozen_plan: ExternalProjectFrozenPlan,
    findings: list[ExternalProjectFrozenPlanFinding],
) -> None:
    declared_task_ids = tuple(sorted(frozen_plan.project.plan.task_ids))
    task_ids = frozen_plan.task_ids
    if len(task_ids) != len(set(task_ids)):
        _append_frozen(
            finding_id="EFP-TASK-DUPLICATE",
            message="external frozen plan contains duplicate task ids",
            findings=findings,
        )
    if task_ids != declared_task_ids:
        _append_frozen(
            finding_id="EFP-TASK-PLAN-MISMATCH",
            message="frozen task ids do not match project plan task ids",
            findings=findings,
        )
    task_id_set = set(task_ids)
    for task in frozen_plan.tasks:
        for dependency_id in task.depends_on:
            if dependency_id not in task_id_set:
                _append_frozen(
                    finding_id=f"EFP-TASK-DEPENDENCY-{_slug(task.id)}-{_slug(dependency_id)}",
                    message=f"task dependency is not in frozen plan: {task.id}",
                    findings=findings,
                )
        missing_commands = sorted(
            set(MANDATORY_VERIFICATION_COMMANDS) - set(task.verification_commands)
        )
        for command in missing_commands:
            _append_frozen(
                finding_id=f"EFP-TASK-VERIFY-{_slug(task.id)}-{_slug(command)}",
                message=f"runtime task missing mandatory verification command: {task.id}",
                findings=findings,
            )
        for index, command in enumerate(task.verification_commands, start=1):
            if _command_has_secret_material(command):
                _append_frozen(
                    finding_id=f"EFP-TASK-VERIFY-SECRET-{_slug(task.id)}-{index}",
                    message=(
                        "runtime task verification command contains secret material: "
                        f"{task.id}"
                    ),
                    findings=findings,
                )
        if task.executor != "codex":
            _append_frozen(
                finding_id=f"EFP-TASK-EXECUTOR-{_slug(task.id)}",
                message=f"runtime task executor is not codex: {task.id}",
                findings=findings,
            )
    if _has_dependency_cycle(frozen_plan.dependency_edges):
        _append_frozen(
            finding_id="EFP-TASK-DEPENDENCY-CYCLE",
            message="external frozen plan dependency graph contains a cycle",
            findings=findings,
        )


def _validate_external_project_gate_definitions(
    frozen_plan: ExternalProjectFrozenPlan,
    findings: list[ExternalProjectFrozenPlanFinding],
) -> None:
    project = frozen_plan.project
    scoped_gates = set(project.plan.required_human_gates)
    approved = set(project.runtime_binding.approved_human_gates)
    pending = set(project.runtime_binding.pending_human_gates)
    if approved & pending:
        _append_frozen(
            finding_id="EFP-GATE-DUPLICATE-STATE",
            message="human gate cannot be both pending and approved",
            findings=findings,
        )
    missing_bound = sorted(scoped_gates - approved - pending)
    for gate_id in missing_bound:
        _append_frozen(
            finding_id=f"EFP-GATE-UNBOUND-{gate_id}",
            message=f"human gate is not explicitly bound: {gate_id}",
            findings=findings,
        )
    definitions_by_id = {
        str(gate.get("id", "")): gate
        for gate in frozen_plan.human_gate_definitions
    }
    definition_ids = set(definitions_by_id)
    for gate_id in sorted(scoped_gates - definition_ids):
        _append_frozen(
            finding_id=f"EFP-GATE-DEFINITION-MISSING-{gate_id}",
            message=f"human gate definition is missing: {gate_id}",
            findings=findings,
        )
    for gate_id in sorted(definition_ids - scoped_gates):
        _append_frozen(
            finding_id=f"EFP-GATE-DEFINITION-UNKNOWN-{gate_id}",
            message=f"human gate definition is not plan-scoped: {gate_id}",
            findings=findings,
        )
    task_ids = set(frozen_plan.task_ids)
    for gate_id, gate in definitions_by_id.items():
        task_id = str(gate.get("task_id", ""))
        if task_id not in task_ids:
            _append_frozen(
                finding_id=f"EFP-GATE-TASK-{gate_id}",
                message=f"human gate target task is not in frozen plan: {gate_id}",
                findings=findings,
            )


def _validate_external_project_runtime_scope(
    frozen_plan: ExternalProjectFrozenPlan,
    findings: list[ExternalProjectFrozenPlanFinding],
) -> None:
    allowed_roots = frozen_plan.project.workspace.allowed_roots
    prohibited_paths = frozen_plan.project.workspace.prohibited_paths
    for task in frozen_plan.tasks:
        for path in (*task.allowed_paths, *task.prohibited_paths):
            if _is_unsafe_relative_path(path):
                _append_frozen(
                    finding_id=f"EFP-SCOPE-PATH-{_slug(task.id)}-{_slug(path)}",
                    message=f"runtime task path is not project-relative: {task.id}",
                    findings=findings,
                )
        for allowed_path in task.allowed_paths:
            if _is_unsafe_relative_path(allowed_path):
                continue
            if not _path_in_allowed_roots(allowed_path, allowed_roots):
                _append_frozen(
                    finding_id=f"EFP-SCOPE-OUTSIDE-WORKSPACE-{_slug(task.id)}",
                    message=f"runtime task allowed path is outside workspace roots: {task.id}",
                    findings=findings,
                )
            if _matches_prohibited_path(allowed_path, prohibited_paths):
                _append_frozen(
                    finding_id=f"EFP-SCOPE-CONFLICT-{_slug(task.id)}-{_slug(allowed_path)}",
                    message=(
                        "runtime task allowed path conflicts with prohibited policy: "
                        f"{task.id}"
                    ),
                    findings=findings,
                )


def _validate_external_project_secret_surfaces(
    frozen_plan: ExternalProjectFrozenPlan,
    findings: list[ExternalProjectFrozenPlanFinding],
) -> None:
    remote_url = frozen_plan.project.repository.remote_url
    if remote_url and _remote_url_has_secret_material(remote_url):
        _append_frozen(
            finding_id="EFP-SECRET-REMOTE",
            message=(
                "repository remote contains secret-bearing material: "
                f"{_redact_remote_url_for_error(remote_url)}"
            ),
            findings=findings,
        )
    for task in frozen_plan.tasks:
        if _mapping_has_secret_field(dict(task.provenance)):
            _append_frozen(
                finding_id=f"EFP-SECRET-TASK-PROVENANCE-{_slug(task.id)}",
                message=f"runtime task provenance contains secret-bearing fields: {task.id}",
                findings=findings,
            )
    for gate in frozen_plan.human_gate_definitions:
        if _mapping_has_secret_field(gate):
            _append_frozen(
                finding_id=f"EFP-SECRET-GATE-{_slug(str(gate.get('id', 'unknown')))}",
                message=(
                    "human gate definition contains secret-bearing fields: "
                    f"{gate.get('id', 'unknown')}"
                ),
                findings=findings,
            )


def _require_safe_workspace_target(
    workspace_root: Path,
    control_plane_root: Path | None,
) -> None:
    if control_plane_root and workspace_root.is_relative_to(control_plane_root):
        raise ExternalProjectServiceError(
            "workspace root is inside the control-plane repository: "
            f"{workspace_root}"
        )


def _workspace_marker_payload(project: ExternalProject, workspace_root: Path) -> dict[str, Any]:
    return {
        "marker_kind": "generated_app_workspace",
        "schema_version": project.schema_version,
        "contract_version": project.contract_version,
        "project_id": project.project_id,
        "workspace_id": project.workspace.workspace_id,
        "root_path": str(workspace_root),
        "state": project.workspace.state,
        "allowed_roots": sorted(
            {_normalize_relative_path(path) for path in project.workspace.allowed_roots}
        ),
        "prohibited_paths": sorted(project.workspace.prohibited_paths),
        "repository_id": project.repository.repository_id,
        "plan_id": project.plan.plan_id,
        "plan_hash": project.plan.effective_plan_hash,
        "control_plane_authority": project.runtime_binding.control_plane_authority,
        "verification_commands": list(MANDATORY_VERIFICATION_COMMANDS),
        "push_requires_human_gate": project.repository.push_requires_human_gate,
    }


def _deterministic_marker_bytes(project: ExternalProject, workspace_root: Path) -> bytes:
    return (
        json.dumps(
            _workspace_marker_payload(project, workspace_root),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        + b"\n"
    )


def _scope_policy_hash(project: ExternalProject) -> str:
    return _hash(
        {
            "allowed_roots": sorted(
                {_normalize_relative_path(path) for path in project.workspace.allowed_roots}
            ),
            "prohibited_paths": sorted(project.workspace.prohibited_paths),
            "control_plane_authority": project.runtime_binding.control_plane_authority,
            "verification_commands": list(MANDATORY_VERIFICATION_COMMANDS),
            "push_requires_human_gate": project.repository.push_requires_human_gate,
        }
    )


def _require_marker_value(payload: dict[str, Any], key: str, expected: object) -> None:
    if payload.get(key) != expected:
        raise ExternalProjectServiceError(f"workspace marker {key} mismatch")


def _require_marker_sequence(
    payload: dict[str, Any],
    key: str,
    expected: list[str],
) -> None:
    raw_value = payload.get(key)
    if not isinstance(raw_value, list) or not all(
        isinstance(item, str) for item in raw_value
    ):
        raise ExternalProjectServiceError(f"workspace marker {key} mismatch")
    if raw_value != expected:
        raise ExternalProjectServiceError(f"workspace marker {key} mismatch")


def _normalize_relative_path(path: str) -> str:
    normalized = str(PurePosixPath(path.strip("/")))
    if _is_unsafe_relative_path(normalized):
        raise ExternalProjectServiceError(f"unsafe workspace path: {path}")
    return normalized


def _path_in_allowed_roots(path: str, allowed_roots: tuple[str, ...]) -> bool:
    normalized = _normalize_relative_path(path)
    for allowed_root in allowed_roots:
        normalized_root = _normalize_relative_path(allowed_root)
        if normalized_root == "**":
            return True
        if normalized == normalized_root or normalized.startswith(f"{normalized_root}/"):
            return True
    return False


def _remote_url_has_secret_material(url: str) -> bool:
    scheme, separator, rest = url.partition("://")
    if not separator or scheme.lower() not in {"http", "https"}:
        return False
    userinfo, at, _host = rest.partition("@")
    if at and userinfo:
        return True
    query = rest.partition("?")[2]
    fragment = rest.partition("#")[2]
    normalized = f"{query} {fragment}".lower().replace("-", "_")
    return any(marker in normalized for marker in SECRET_FIELD_MARKERS)


def _command_has_secret_material(command: str) -> bool:
    normalized = command.lower().replace("-", "_")
    return any(
        f"{marker}=" in normalized or f"{marker} " in normalized
        for marker in SECRET_FIELD_MARKERS
    )


def _redact_remote_url_for_error(url: str) -> str:
    redacted = _redact_url(url)
    if redacted is None:
        return ""
    redacted = redacted.partition("?")[0] + ("?<redacted>" if "?" in redacted else "")
    redacted = redacted.partition("#")[0] + ("#<redacted>" if "#" in redacted else "")
    return redacted


def _validate_identity_bindings(
    project: ExternalProject,
    findings: list[ExternalProjectModelFinding],
) -> None:
    if project.workspace.owner_project_id not in {None, project.project_id}:
        _append(
            finding_id="EPM-IDENTITY-WORKSPACE",
            message="workspace owner does not match project",
            findings=findings,
        )
    if project.repository.workspace_id != project.workspace.workspace_id:
        _append(
            finding_id="EPM-IDENTITY-REPOSITORY",
            message="repository is not bound to project workspace",
            findings=findings,
        )
    binding = project.runtime_binding
    expected = {
        "project_id": project.project_id,
        "workspace_id": project.workspace.workspace_id,
        "repository_id": project.repository.repository_id,
        "plan_id": project.plan.plan_id,
        "plan_version": project.plan.version,
    }
    for key, expected_value in expected.items():
        if getattr(binding, key) != expected_value:
            _append(
                finding_id=f"EPM-IDENTITY-BINDING-{key.upper()}",
                message=f"runtime binding {key} mismatch",
                findings=findings,
            )


def _validate_scope_policy(
    project: ExternalProject,
    findings: list[ExternalProjectModelFinding],
) -> None:
    missing_prohibited = sorted(
        set(DEFAULT_PROHIBITED_PATHS) - set(project.workspace.prohibited_paths)
    )
    for path in missing_prohibited:
        _append(
            finding_id=f"EPM-SCOPE-PROHIBITED-{_slug(path)}",
            message=f"workspace missing prohibited path policy: {path}",
            findings=findings,
        )
    for allowed_root in project.workspace.allowed_roots:
        if _is_unsafe_relative_path(allowed_root):
            _append(
                finding_id=f"EPM-SCOPE-ALLOWED-{_slug(allowed_root)}",
                message=f"workspace allowed root is not project-relative: {allowed_root}",
                findings=findings,
            )
        if _matches_prohibited_path(allowed_root, project.workspace.prohibited_paths):
            _append(
                finding_id=f"EPM-SCOPE-CONFLICT-{_slug(allowed_root)}",
                message=(
                    "workspace allowed root conflicts with prohibited path policy: "
                    f"{allowed_root}"
                ),
                findings=findings,
            )


def _validate_control_plane_authority(
    project: ExternalProject,
    findings: list[ExternalProjectModelFinding],
) -> None:
    if project.repository.control_plane_write_authority:
        _append(
            finding_id="EPM-AUTHORITY-REPOSITORY",
            message="external repository grants control-plane write authority",
            findings=findings,
        )
    if not project.repository.push_requires_human_gate:
        _append(
            finding_id="EPM-AUTHORITY-PUSH-GATE",
            message="external repository push no longer requires a human gate",
            findings=findings,
        )
    if project.plan.control_plane_authority != CONTROL_PLANE_AUTHORITY:
        _append(
            finding_id="EPM-AUTHORITY-PLAN-SOURCE",
            message="plan does not preserve existing control-plane authority",
            findings=findings,
        )
    binding = project.runtime_binding
    if binding.control_plane_authority != CONTROL_PLANE_AUTHORITY:
        _append(
            finding_id="EPM-AUTHORITY-SOURCE",
            message="runtime binding does not preserve existing control-plane authority",
            findings=findings,
        )
    if binding.grants_control_plane_authority:
        _append(
            finding_id="EPM-AUTHORITY-GRANT",
            message="runtime binding grants control-plane authority",
            findings=findings,
        )
    if binding.verifier_bypass_authority:
        _append(
            finding_id="EPM-AUTHORITY-VERIFIER-BYPASS",
            message="runtime binding grants verifier bypass authority",
            findings=findings,
        )


def _validate_human_gates(
    project: ExternalProject,
    findings: list[ExternalProjectModelFinding],
) -> None:
    binding = project.runtime_binding
    if binding.implicit_human_gate_approval:
        _append(
            finding_id="EPM-GATE-IMPLICIT-APPROVAL",
            message="runtime binding allows implicit human gate approval",
            findings=findings,
        )
    scoped_gates = set(project.plan.required_human_gates)
    requested_gates = set(binding.approved_human_gates) | set(binding.pending_human_gates)
    unknown_gates = sorted(requested_gates - scoped_gates)
    for gate_id in unknown_gates:
        _append(
            finding_id=f"EPM-GATE-UNKNOWN-{gate_id}",
            message=f"human gate is not plan-scoped: {gate_id}",
            findings=findings,
        )
    execution_states = {"IMPORT", "EXECUTE", "VERIFY", "COMPLETE", "RUN", "ARCHIVE"}
    if project.lifecycle_state in execution_states:
        missing = sorted(
            set(project.plan.required_human_gates) - set(binding.approved_human_gates)
        )
        for gate_id in missing:
            _append(
                finding_id=f"EPM-GATE-PENDING-{gate_id}",
                message=f"human gate remains pending: {gate_id}",
                findings=findings,
            )


def _validate_verification(
    project: ExternalProject,
    findings: list[ExternalProjectModelFinding],
) -> None:
    if not project.plan.independent_verification_required:
        _append(
            finding_id="EPM-VERIFY-PLAN-DISABLED",
            message="plan disables independent verification",
            findings=findings,
        )
    plan_commands = set(project.plan.verification_commands)
    binding_commands = set(project.runtime_binding.verification_commands)
    missing_plan = sorted(set(MANDATORY_VERIFICATION_COMMANDS) - plan_commands)
    missing_binding = sorted(set(MANDATORY_VERIFICATION_COMMANDS) - binding_commands)
    for command in missing_plan:
        _append(
            finding_id=f"EPM-VERIFY-PLAN-{_slug(command)}",
            message=f"plan missing mandatory verification command: {command}",
            findings=findings,
        )
    for command in missing_binding:
        _append(
            finding_id=f"EPM-VERIFY-BINDING-{_slug(command)}",
            message=f"runtime binding missing mandatory verification command: {command}",
            findings=findings,
        )
    for index, command in enumerate(project.plan.verification_commands, start=1):
        if _command_has_secret_material(command):
            _append(
                finding_id=f"EPM-VERIFY-PLAN-SECRET-{index}",
                message="plan verification command contains secret material",
                findings=findings,
            )
    for index, command in enumerate(project.runtime_binding.verification_commands, start=1):
        if _command_has_secret_material(command):
            _append(
                finding_id=f"EPM-VERIFY-BINDING-SECRET-{index}",
                message="runtime binding verification command contains secret material",
                findings=findings,
            )


def _validate_artifacts(
    project: ExternalProject,
    findings: list[ExternalProjectModelFinding],
) -> None:
    for artifact in project.artifacts:
        if artifact.project_id != project.project_id:
            _append(
                finding_id=f"EPM-ARTIFACT-PROJECT-{artifact.artifact_id}",
                message=f"artifact project mismatch: {artifact.artifact_id}",
                findings=findings,
            )
        if artifact.workspace_id != project.workspace.workspace_id:
            _append(
                finding_id=f"EPM-ARTIFACT-WORKSPACE-{artifact.artifact_id}",
                message=f"artifact workspace mismatch: {artifact.artifact_id}",
                findings=findings,
            )
        if not _is_sha256_hex(artifact.content_hash):
            _append(
                finding_id=f"EPM-ARTIFACT-HASH-{artifact.artifact_id}",
                message=(
                    "artifact content hash is not a SHA-256 hex digest: "
                    f"{artifact.artifact_id}"
                ),
                findings=findings,
            )
        if artifact.contains_secret:
            _append(
                finding_id=f"EPM-ARTIFACT-SECRET-{artifact.artifact_id}",
                message=f"artifact declares secret content: {artifact.artifact_id}",
                findings=findings,
            )
        if _is_unsafe_relative_path(artifact.relative_path):
            _append(
                finding_id=f"EPM-ARTIFACT-PATH-{artifact.artifact_id}",
                message=f"artifact path is not project-relative: {artifact.artifact_id}",
                findings=findings,
            )
        if _matches_prohibited_path(artifact.relative_path, project.workspace.prohibited_paths):
            _append(
                finding_id=f"EPM-ARTIFACT-PROHIBITED-PATH-{artifact.artifact_id}",
                message=f"artifact path is prohibited: {artifact.artifact_id}",
                findings=findings,
            )
        if _mapping_has_secret_field(artifact.metadata or {}):
            _append(
                finding_id=f"EPM-ARTIFACT-METADATA-SECRET-{artifact.artifact_id}",
                message=f"artifact metadata contains secret-bearing fields: {artifact.artifact_id}",
                findings=findings,
            )


def _append(
    *,
    finding_id: str,
    message: str,
    findings: list[ExternalProjectModelFinding],
) -> None:
    findings.append(
        ExternalProjectModelFinding(finding_id=finding_id, severity="ERROR", message=message)
    )


def _append_frozen(
    *,
    finding_id: str,
    message: str,
    findings: list[ExternalProjectFrozenPlanFinding],
) -> None:
    findings.append(
        ExternalProjectFrozenPlanFinding(
            finding_id=finding_id,
            severity="ERROR",
            message=message,
        )
    )


def _hash(payload: Any) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _redact_url(value: str | None) -> str | None:
    if value is None:
        return None
    redacted = value
    if "@" not in value:
        return _redact_url_query_or_fragment(redacted)
    scheme, separator, rest = redacted.partition("://")
    if separator:
        _, _, host = rest.rpartition("@")
        redacted = f"{scheme}://<redacted>@{host}"
    else:
        _, _, host = redacted.rpartition("@")
        redacted = f"<redacted>@{host}"
    return _redact_url_query_or_fragment(redacted)


def _redact_url_query_or_fragment(value: str) -> str:
    base, query_separator, query_and_fragment = value.partition("?")
    if query_separator:
        query, fragment_separator, fragment = query_and_fragment.partition("#")
        if _text_has_secret_marker(query):
            query = "<redacted>"
        if fragment_separator and _text_has_secret_marker(fragment):
            fragment = "<redacted>"
        return f"{base}?{query}{fragment_separator}{fragment}"
    base, fragment_separator, fragment = value.partition("#")
    if fragment_separator and _text_has_secret_marker(fragment):
        return f"{base}#<redacted>"
    return value


def _text_has_secret_marker(value: str) -> bool:
    normalized = value.lower().replace("-", "_")
    return any(marker in normalized for marker in SECRET_FIELD_MARKERS)


def _redact_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: ("<redacted>" if _is_secret_key(str(key)) else _redact_value(item))
        for key, item in sorted(value.items())
    }


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def _mapping_has_secret_field(value: dict[str, Any]) -> bool:
    return any(
        _is_secret_key(str(key))
        or (isinstance(item, dict) and _mapping_has_secret_field(item))
        or (
            isinstance(item, list)
            and any(isinstance(entry, dict) and _mapping_has_secret_field(entry) for entry in item)
        )
        for key, item in value.items()
    )


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(marker in normalized for marker in SECRET_FIELD_MARKERS)


def _is_unsafe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return path.is_absolute() or ".." in path.parts or value in {"", "."}


def _matches_prohibited_path(value: str, prohibited_paths: tuple[str, ...]) -> bool:
    normalized = value.strip("/")
    for prohibited in prohibited_paths:
        normalized_prohibited = prohibited.strip("/")
        if normalized_prohibited.endswith("/**"):
            prefix = normalized_prohibited[:-3]
            if normalized == prefix or normalized.startswith(f"{prefix}/"):
                return True
        elif normalized == normalized_prohibited:
            return True
    return False


def _is_sha256_hex(value: str) -> bool:
    if len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value.lower())


def _slug(value: str) -> str:
    slug = "".join(character if character.isalnum() else "-" for character in value.lower())
    return slug.strip("-")[:80] or "empty"


def _has_dependency_cycle(edges: tuple[tuple[str, str], ...]) -> bool:
    dependencies: dict[str, set[str]] = {}
    for task_id, depends_on in edges:
        dependencies.setdefault(task_id, set()).add(depends_on)
        dependencies.setdefault(depends_on, set())
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> bool:
        if task_id in visiting:
            return True
        if task_id in visited:
            return False
        visiting.add(task_id)
        for dependency_id in dependencies.get(task_id, set()):
            if visit(dependency_id):
                return True
        visiting.remove(task_id)
        visited.add(task_id)
        return False

    return any(visit(task_id) for task_id in sorted(dependencies))
