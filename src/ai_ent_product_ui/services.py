from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ai_ent.product_runtime_handoff import (
    ProductRuntimeHandoffArtifacts,
    load_product_runtime_handoff_artifacts,
)
from ai_ent_product_api.errors import redact_secret_details
from ai_ent_product_api.services import ProductApiServices
from ai_ent_product_ui.errors import ProductUiError
from ai_ent_product_ui.schemas import (
    PRODUCT_UI_PREFIX,
    ArtifactBrowserView,
    ArtifactRecord,
    AuthenticatedOperator,
    DashboardAuthoritySummary,
    DashboardGateSummary,
    DashboardMetric,
    DashboardRequest,
    DashboardVerificationSummary,
    DashboardView,
    EvidenceBrowserView,
    EvidenceRecord,
    LoginRequest,
    LoginSession,
    LoginView,
    ProductUiAuthoritySnapshot,
    ProvenanceEdge,
    ProvenanceNode,
    ProvenancePathView,
    UiHealthStatus,
    UiNavigationItem,
    UiShell,
)

PASSWORD_HASH_CONTEXT = "ai-ent-product-ui-password-v1"
SESSION_CONTEXT = "ai-ent-product-ui-session-v1"
ArtifactsLoader = Callable[[], ProductRuntimeHandoffArtifacts]


@dataclass(frozen=True)
class OperatorAccount:
    username: str
    display_name: str
    password_hash: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class OperatorSessionRecord:
    session_id: str
    operator: AuthenticatedOperator


@dataclass
class OperatorSessionStore:
    _sessions: dict[str, OperatorSessionRecord]

    @classmethod
    def empty(cls) -> OperatorSessionStore:
        return cls(_sessions={})

    def create(self, account: OperatorAccount) -> OperatorSessionRecord:
        operator = AuthenticatedOperator(
            username=account.username,
            display_name=account.display_name,
            roles=account.roles,
        )
        session_id = _session_id(operator)
        record = OperatorSessionRecord(session_id=session_id, operator=operator)
        self._sessions[session_id] = record
        return record

    def require(self, session_id: str) -> OperatorSessionRecord:
        record = self._sessions.get(session_id)
        if record is None:
            raise ProductUiError(
                status_code=401,
                code="AUTHENTICATION_REQUIRED",
                message="an authenticated operator session is required",
            )
        return record


@dataclass(frozen=True)
class OperatorDirectory:
    _accounts: dict[str, OperatorAccount]

    @classmethod
    def empty(cls) -> OperatorDirectory:
        return cls(_accounts={})

    @classmethod
    def with_accounts(cls, accounts: tuple[OperatorAccount, ...]) -> OperatorDirectory:
        return cls(_accounts={account.username: account for account in accounts})

    def authenticate(self, request: LoginRequest) -> OperatorAccount:
        account = self._accounts.get(request.username)
        if account is None or account.password_hash != hash_password(request.password):
            raise ProductUiError(
                status_code=401,
                code="INVALID_CREDENTIALS",
                message="username or password was not accepted",
            )
        return account


@dataclass(frozen=True)
class DashboardProjection:
    project_count: int = 0
    pending_gate_count: int = 0
    failed_execution_count: int = 0
    system_health_issue_count: int = 0

    def metrics(self) -> tuple[DashboardMetric, ...]:
        return (
            DashboardMetric(label="Projects", value=self.project_count, status="ok"),
            DashboardMetric(
                label="Pending human gates",
                value=self.pending_gate_count,
                status="attention" if self.pending_gate_count else "ok",
            ),
            DashboardMetric(
                label="Failed executions",
                value=self.failed_execution_count,
                status="blocked" if self.failed_execution_count else "ok",
            ),
            DashboardMetric(
                label="System health issues",
                value=self.system_health_issue_count,
                status="attention" if self.system_health_issue_count else "ok",
            ),
        )


class EvidenceArtifactSource(Protocol):
    def artifact_records(self) -> tuple[ArtifactRecord, ...]:
        ...

    def evidence_records(self) -> tuple[EvidenceRecord, ...]:
        ...

    def provenance_edges(self) -> tuple[ProvenanceEdge, ...]:
        ...


@dataclass(frozen=True)
class StaticEvidenceArtifactSource(EvidenceArtifactSource):
    artifacts: tuple[ArtifactRecord, ...]
    evidence: tuple[EvidenceRecord, ...]
    edges: tuple[ProvenanceEdge, ...]

    def artifact_records(self) -> tuple[ArtifactRecord, ...]:
        return _sanitize_artifacts(self.artifacts)

    def evidence_records(self) -> tuple[EvidenceRecord, ...]:
        return _sanitize_evidence(self.evidence)

    def provenance_edges(self) -> tuple[ProvenanceEdge, ...]:
        return tuple(
            sorted(
                self.edges,
                key=lambda edge: (edge.source_id, edge.target_id, edge.relationship),
            )
        )


@dataclass(frozen=True)
class ProductHandoffEvidenceArtifactSource(EvidenceArtifactSource):
    artifacts_loader: ArtifactsLoader = load_product_runtime_handoff_artifacts

    def artifact_records(self) -> tuple[ArtifactRecord, ...]:
        artifacts = self.artifacts_loader()
        records = [
            _handoff_artifact(
                "prd-productization-plan",
                "Productization plan",
                "handoff://product-plan",
                artifacts.product_plan,
            ),
            _handoff_artifact(
                "prd-product-plan-lock",
                "Product plan lock",
                "handoff://product-plan-lock",
                artifacts.lock,
            ),
            _handoff_artifact(
                "prd-product-dry-run",
                "Product dry-run evidence",
                "handoff://product-dry-run",
                artifacts.pdf,
            ),
            _handoff_artifact(
                "prd-product-gate-acceptance",
                "Product gate acceptance evidence",
                "handoff://product-gate-acceptance",
                artifacts.ppg_acceptance,
            ),
        ]
        records.extend(
            _handoff_artifact(
                f"prd-task-contract:{task_id}",
                f"{task_id} task contract",
                f"handoff://task-contracts/{task_id}",
                task,
            )
            for task in artifacts.import_preview
            if (task_id := str(task.get("task_id", "")))
        )
        return _sanitize_artifacts(tuple(records))

    def evidence_records(self) -> tuple[EvidenceRecord, ...]:
        artifacts = self.artifacts_loader()
        task_artifact_ids = tuple(
            f"prd-task-contract:{task_id}"
            for task in artifacts.import_preview
            if (task_id := str(task.get("task_id", "")))
        )
        human_gate_ids = tuple(
            sorted(
                str(gate.get("gate_id", ""))
                for gate in _mapping_sequence(artifacts.lock.get("human_gate_definitions"))
                if gate.get("gate_id")
            )
        )
        return _sanitize_evidence(
            (
                EvidenceRecord(
                    evidence_id="PRD-EVID-PRODUCT-HANDOFF",
                    title="Product runtime handoff evidence",
                    summary=(
                        "Frozen product handoff records projected for browser provenance; "
                        "not a control-plane authority source."
                    ),
                    source_refs=("PRD-TASK-010", "PRD-TASK-015", "PRD-TASK-017"),
                    artifact_ids=(
                        "prd-productization-plan",
                        "prd-product-plan-lock",
                        "prd-product-dry-run",
                        "prd-product-gate-acceptance",
                        *task_artifact_ids,
                    ),
                    human_gate_ids=human_gate_ids,
                    metadata={
                        "plan_id": artifacts.lock.get("product_plan_id"),
                        "plan_version": artifacts.lock.get("plan_version"),
                    },
                ),
            )
        )

    def provenance_edges(self) -> tuple[ProvenanceEdge, ...]:
        return tuple(
            ProvenanceEdge(
                source_id="PRD-EVID-PRODUCT-HANDOFF",
                target_id=artifact.artifact_id,
                relationship="supports_metadata_for",
            )
            for artifact in self.artifact_records()
        )


@dataclass(frozen=True)
class ProductEvidenceBrowserService:
    source: EvidenceArtifactSource = field(default_factory=ProductHandoffEvidenceArtifactSource)

    def authority(self) -> ProductUiAuthoritySnapshot:
        return ProductUiAuthoritySnapshot()

    def artifacts(
        self,
        *,
        query: str | None = None,
        selected_artifact_id: str | None = None,
    ) -> ArtifactBrowserView:
        artifacts = self.source.artifact_records()
        visible = _filter_artifacts(artifacts, query)
        selected = next(
            (artifact for artifact in artifacts if artifact.artifact_id == selected_artifact_id),
            None,
        )
        return ArtifactBrowserView(
            query=query,
            total_artifacts=len(visible),
            artifacts=visible,
            selected_artifact=selected,
            authority=self.authority(),
        )

    def evidence(self) -> EvidenceBrowserView:
        records = self.source.evidence_records()
        return EvidenceBrowserView(
            total_evidence_records=len(records),
            evidence_records=records,
            authority=self.authority(),
        )

    def provenance_path(
        self,
        *,
        artifact_id: str | None = None,
        evidence_id: str | None = None,
    ) -> ProvenancePathView:
        artifacts = {artifact.artifact_id: artifact for artifact in self.source.artifact_records()}
        evidence = {record.evidence_id: record for record in self.source.evidence_records()}
        selected_edges = _select_edges(
            self.source.provenance_edges(),
            artifact_id=artifact_id,
            evidence_id=evidence_id,
        )
        selected_evidence_ids = {
            edge.source_id for edge in selected_edges if edge.source_id in evidence
        }
        selected_artifact_ids = {
            edge.target_id for edge in selected_edges if edge.target_id in artifacts
        }
        if evidence_id is not None and evidence_id in evidence:
            selected_evidence_ids.add(evidence_id)
        if artifact_id is not None and artifact_id in artifacts:
            selected_artifact_ids.add(artifact_id)

        evidence_records = tuple(evidence[item] for item in sorted(selected_evidence_ids))
        nodes = tuple(
            sorted(
                (
                    *(
                        ProvenanceNode(
                            node_id=record.evidence_id,
                            node_type="evidence",
                            label=record.title,
                            status=record.evidence_status,
                        )
                        for record in evidence_records
                    ),
                    *(
                        ProvenanceNode(
                            node_id=artifacts[item].artifact_id,
                            node_type="artifact",
                            label=artifacts[item].title,
                            status=artifacts[item].authority_state,
                        )
                        for item in selected_artifact_ids
                    ),
                ),
                key=lambda node: (node.node_type, node.node_id),
            )
        )
        return ProvenancePathView(
            artifact_id=artifact_id,
            evidence_id=evidence_id,
            nodes=nodes,
            edges=selected_edges,
            evidence_records=evidence_records,
            authority=self.authority(),
        )


@dataclass
class ProductUiServices:
    api_services: ProductApiServices
    operator_directory: OperatorDirectory
    session_store: OperatorSessionStore
    dashboard_projection: DashboardProjection
    evidence_browser: ProductEvidenceBrowserService = field(
        default_factory=ProductEvidenceBrowserService
    )

    @classmethod
    def defaults(cls) -> ProductUiServices:
        return cls(
            api_services=ProductApiServices.defaults(),
            operator_directory=OperatorDirectory.empty(),
            session_store=OperatorSessionStore.empty(),
            dashboard_projection=DashboardProjection(),
        )

    def health(self) -> UiHealthStatus:
        return UiHealthStatus()

    def shell(self) -> UiShell:
        return UiShell(
            primary_routes=(
                UiNavigationItem(label="Dashboard", route=f"{PRODUCT_UI_PREFIX}/dashboard"),
                UiNavigationItem(label="Projects", route=f"{PRODUCT_UI_PREFIX}/projects"),
                UiNavigationItem(label="Human Approvals", route=f"{PRODUCT_UI_PREFIX}/gates"),
                UiNavigationItem(label="Evidence", route=f"{PRODUCT_UI_PREFIX}/evidence"),
                UiNavigationItem(label="System Status", route=f"{PRODUCT_UI_PREFIX}/system"),
            ),
            forbidden_implicit_actions=(
                "human gate approval",
                "task completion",
                "verification approval",
                "policy weakening",
                "unrestricted executor invocation",
            ),
        )

    def login_view(self) -> LoginView:
        return LoginView()

    def login(self, request: LoginRequest) -> LoginSession:
        account = self.operator_directory.authenticate(request)
        record = self.session_store.create(account)
        return LoginSession(session_id=record.session_id, operator=record.operator)

    def dashboard(self, request: DashboardRequest) -> DashboardView:
        session = self.session_store.require(request.session_id)
        boundary = self.api_services.boundary()
        return DashboardView(
            operator=session.operator,
            metrics=self.dashboard_projection.metrics(),
            gates=DashboardGateSummary(pending=self.dashboard_projection.pending_gate_count),
            verification=DashboardVerificationSummary(
                independent_verification_required=boundary.independent_verification_required,
                mandatory_verification_commands=boundary.mandatory_verification_commands,
                verifier_bypass_authority=boundary.verifier_bypass_authority,
            ),
            authority=DashboardAuthoritySummary(
                control_plane_authority=boundary.control_plane_authority,
                authority_mode=boundary.authority_mode,
                grants_control_plane_authority=boundary.grants_control_plane_authority,
                gate_approval_authority=boundary.gate_approval_authority,
                scheduling_authority=boundary.scheduling_authority,
                runtime_execution_authority=boundary.runtime_execution_authority,
                policy_weakening_authority=boundary.policy_weakening_authority,
                denied_operations=boundary.denied_operations,
            ),
            decision_dependencies=boundary.decision_dependencies,
        )

    def artifact_authority(self) -> ProductUiAuthoritySnapshot:
        return self.evidence_browser.authority()

    def artifacts(
        self,
        *,
        query: str | None = None,
        selected_artifact_id: str | None = None,
    ) -> ArtifactBrowserView:
        return self.evidence_browser.artifacts(
            query=query,
            selected_artifact_id=selected_artifact_id,
        )

    def evidence(self) -> EvidenceBrowserView:
        return self.evidence_browser.evidence()

    def provenance_path(
        self,
        *,
        artifact_id: str | None = None,
        evidence_id: str | None = None,
    ) -> ProvenancePathView:
        return self.evidence_browser.provenance_path(
            artifact_id=artifact_id,
            evidence_id=evidence_id,
        )


def hash_password(password: str) -> str:
    return hashlib.sha256(f"{PASSWORD_HASH_CONTEXT}:{password}".encode()).hexdigest()


def _session_id(operator: AuthenticatedOperator) -> str:
    material = "|".join((operator.username, operator.display_name, ",".join(operator.roles)))
    return hashlib.sha256(f"{SESSION_CONTEXT}:{material}".encode()).hexdigest()


def _sanitize_artifacts(artifacts: Iterable[ArtifactRecord]) -> tuple[ArtifactRecord, ...]:
    return tuple(
        sorted(
            (
                artifact.model_copy(
                    update={
                        "evidence_status": "NON_AUTHORITATIVE",
                        "metadata": redact_secret_details(artifact.metadata),
                    }
                )
                for artifact in artifacts
            ),
            key=lambda artifact: artifact.artifact_id,
        )
    )


def _sanitize_evidence(evidence: Iterable[EvidenceRecord]) -> tuple[EvidenceRecord, ...]:
    return tuple(
        sorted(
            (
                record.model_copy(
                    update={
                        "authority_state": "NON_AUTHORITATIVE",
                        "evidence_status": "NON_AUTHORITATIVE",
                        "independent_verification_required": True,
                        "metadata": redact_secret_details(record.metadata),
                    }
                )
                for record in evidence
            ),
            key=lambda record: record.evidence_id,
        )
    )


def _filter_artifacts(
    artifacts: tuple[ArtifactRecord, ...],
    query: str | None,
) -> tuple[ArtifactRecord, ...]:
    if not query:
        return artifacts
    normalized = query.casefold()
    return tuple(
        artifact
        for artifact in artifacts
        if normalized in _artifact_search_text(artifact).casefold()
    )


def _artifact_search_text(artifact: ArtifactRecord) -> str:
    return (
        f"{artifact.artifact_id} {artifact.artifact_type} {artifact.kind} "
        f"{artifact.title} {artifact.relative_path} {artifact.producer}"
    )


def _select_edges(
    edges: tuple[ProvenanceEdge, ...],
    *,
    artifact_id: str | None,
    evidence_id: str | None,
) -> tuple[ProvenanceEdge, ...]:
    selected = [
        edge
        for edge in edges
        if (artifact_id is None or edge.target_id == artifact_id)
        and (evidence_id is None or edge.source_id == evidence_id)
    ]
    return tuple(
        sorted(
            selected,
            key=lambda edge: (edge.source_id, edge.target_id, edge.relationship),
        )
    )


def _handoff_artifact(
    artifact_id: str,
    title: str,
    relative_path: str,
    payload: Mapping[str, Any],
) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id,
        artifact_type="product_handoff_metadata",
        kind="evidence",
        title=title,
        relative_path=relative_path,
        content_hash=_hash_payload(payload),
        producer="product_runtime_handoff",
        authority_state="NON_AUTHORITATIVE",
        metadata={
            "payload_keys": tuple(sorted(str(key) for key in payload)),
            "raw_content_available": False,
        },
    )


def _hash_payload(payload: Mapping[str, Any]) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _mapping_sequence(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))
