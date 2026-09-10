from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

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


def external_project_model_record(project: ExternalProject) -> dict[str, Any]:
    validation = validate_external_project(project)
    return {
        "record_kind": "external_project_model",
        "project": project.as_dict(),
        "validation": validation.as_dict(),
    }


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


def _hash(payload: Any) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _redact_url(value: str | None) -> str | None:
    if value is None:
        return None
    if "@" not in value:
        return value
    scheme, separator, rest = value.partition("://")
    if separator:
        _, _, host = rest.rpartition("@")
        return f"{scheme}://<redacted>@{host}"
    _, _, host = value.rpartition("@")
    return f"<redacted>@{host}"


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
