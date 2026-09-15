from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from ai_ent_product_api.errors import redact_secret_details
from ai_ent_product_api.schemas import PRODUCT_API_DECISION_DEPENDENCIES

ProjectValidationStatus = Literal["DRAFT", "ACCEPTED", "REJECTED"]
HumanGateStatus = Literal["explicit_control_plane_gate_required"]

PROJECT_INTAKE_TARGET = "manifest/project/ai-ent/intake.yaml"


@dataclass(frozen=True)
class ProjectIntakeDraft:
    project_id: str
    name: str
    summary: str
    actor_id: str
    domain: str = "business"
    evolution_type: str = "business"
    target_manifest_paths: tuple[str, ...] = (PROJECT_INTAKE_TARGET,)
    requested_authorities: tuple[str, ...] = ("recommend", "record_evidence")
    learning_sources: tuple[str, ...] = ("approved_manifests",)
    evidence_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def governance_payload(self) -> dict[str, Any]:
        return {
            "request_id": f"PROJECT-INTAKE-{self.project_id}",
            "actor_id": self.actor_id,
            "domain": self.domain,
            "evolution_type": self.evolution_type,
            "summary": f"Project intake request for {self.name}: {self.summary}",
            "target_manifest_paths": list(self.target_manifest_paths),
            "requested_authorities": list(self.requested_authorities),
            "learning_sources": list(self.learning_sources),
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class BoundaryView:
    control_plane_authority: str
    human_gate_policy: HumanGateStatus
    independent_verification_required: bool
    mandatory_verification_commands: tuple[str, ...]
    decision_dependencies: tuple[str, ...] = PRODUCT_API_DECISION_DEPENDENCIES
    grants_control_plane_authority: bool = False
    implicit_human_gate_approval: bool = False
    secret_values_exposed: bool = False

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> BoundaryView:
        return cls(
            control_plane_authority=str(payload["control_plane_authority"]),
            human_gate_policy="explicit_control_plane_gate_required",
            independent_verification_required=bool(payload["independent_verification_required"]),
            mandatory_verification_commands=tuple(
                str(command) for command in payload["mandatory_verification_commands"]
            ),
            decision_dependencies=tuple(str(item) for item in payload["decision_dependencies"]),
            grants_control_plane_authority=bool(payload["grants_control_plane_authority"]),
            implicit_human_gate_approval=bool(payload["implicit_human_gate_approval"]),
            secret_values_exposed=bool(payload["secret_values_exposed"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "control_plane_authority": self.control_plane_authority,
            "decision_dependencies": list(self.decision_dependencies),
            "grants_control_plane_authority": self.grants_control_plane_authority,
            "human_gate_policy": self.human_gate_policy,
            "implicit_human_gate_approval": self.implicit_human_gate_approval,
            "independent_verification_required": self.independent_verification_required,
            "mandatory_verification_commands": list(self.mandatory_verification_commands),
            "secret_values_exposed": self.secret_values_exposed,
        }


@dataclass(frozen=True)
class ValidationView:
    status: ProjectValidationStatus
    request_id: str | None = None
    accepted_authorities: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    required_evolution_sequence: tuple[str, ...] = ()
    gate_approval_granted: bool = False
    verifier_bypass_granted: bool = False
    commit_boundary_bypass_granted: bool = False
    self_scheduling_granted: bool = False
    runtime_execution_granted: bool = False
    policy_weakening_granted: bool = False

    @classmethod
    def draft(cls) -> ValidationView:
        return cls(status="DRAFT")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ValidationView:
        summary = payload.get("summary", {})
        if not isinstance(summary, Mapping):
            summary = {}
        return cls(
            status=_validation_status(payload.get("status")),
            request_id=str(payload.get("request_id", "")),
            accepted_authorities=tuple(str(item) for item in payload["accepted_authorities"]),
            blockers=tuple(str(item) for item in payload["blockers"]),
            required_evolution_sequence=tuple(
                str(item) for item in payload["required_evolution_sequence"]
            ),
            gate_approval_granted=bool(summary.get("gate_approval_granted", False)),
            verifier_bypass_granted=bool(summary.get("verifier_bypass_granted", False)),
            commit_boundary_bypass_granted=bool(
                summary.get("commit_boundary_bypass_granted", False)
            ),
            self_scheduling_granted=bool(summary.get("self_scheduling_granted", False)),
            runtime_execution_granted=bool(summary.get("runtime_execution_granted", False)),
            policy_weakening_granted=bool(summary.get("policy_weakening_granted", False)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted_authorities": list(self.accepted_authorities),
            "blockers": list(self.blockers),
            "commit_boundary_bypass_granted": self.commit_boundary_bypass_granted,
            "gate_approval_granted": self.gate_approval_granted,
            "policy_weakening_granted": self.policy_weakening_granted,
            "request_id": self.request_id,
            "required_evolution_sequence": list(self.required_evolution_sequence),
            "runtime_execution_granted": self.runtime_execution_granted,
            "self_scheduling_granted": self.self_scheduling_granted,
            "status": self.status,
            "verifier_bypass_granted": self.verifier_bypass_granted,
        }


@dataclass(frozen=True)
class ProjectListItem:
    project_id: str
    name: str
    summary: str
    validation: ValidationView
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_draft(
        cls,
        draft: ProjectIntakeDraft,
        *,
        validation: ValidationView | None = None,
    ) -> ProjectListItem:
        return cls(
            project_id=draft.project_id,
            name=draft.name,
            summary=draft.summary,
            validation=validation or ValidationView.draft(),
            metadata=_safe_metadata(draft.metadata),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "metadata": _safe_metadata(self.metadata),
            "name": self.name,
            "project_id": self.project_id,
            "summary": self.summary,
            "validation": self.validation.as_dict(),
        }


@dataclass(frozen=True)
class ProjectListView:
    projects: tuple[ProjectListItem, ...]
    boundary: BoundaryView
    selected_project_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "boundary": self.boundary.as_dict(),
            "projects": [project.as_dict() for project in self.projects],
            "selected_project_id": self.selected_project_id,
        }


@dataclass(frozen=True)
class NewProjectView:
    draft: ProjectIntakeDraft
    boundary: BoundaryView
    validation: ValidationView

    def as_dict(self) -> dict[str, Any]:
        return {
            "boundary": self.boundary.as_dict(),
            "draft": ProjectListItem.from_draft(self.draft).as_dict(),
            "validation": self.validation.as_dict(),
        }


@dataclass(frozen=True)
class ProjectOverviewView:
    project: ProjectListItem
    boundary: BoundaryView

    @property
    def human_gate_status(self) -> HumanGateStatus:
        return self.boundary.human_gate_policy

    @property
    def independent_verification_status(self) -> Literal["mandatory"]:
        return "mandatory"

    @property
    def control_plane_created(self) -> bool:
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "boundary": self.boundary.as_dict(),
            "control_plane_created": self.control_plane_created,
            "human_gate_status": self.human_gate_status,
            "independent_verification_status": self.independent_verification_status,
            "project": self.project.as_dict(),
        }


def sorted_projects(projects: tuple[ProjectListItem, ...]) -> tuple[ProjectListItem, ...]:
    return tuple(sorted(projects, key=lambda project: (project.name.casefold(), project.project_id)))


def _validation_status(value: object) -> ProjectValidationStatus:
    if value == "ACCEPTED":
        return "ACCEPTED"
    if value == "REJECTED":
        return "REJECTED"
    return "DRAFT"


def _safe_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    redacted = redact_secret_details(dict(metadata))
    if isinstance(redacted, dict):
        return redacted
    return {}
