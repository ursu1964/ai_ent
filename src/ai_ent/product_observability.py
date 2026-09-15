from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ai_ent.external_project import MANDATORY_VERIFICATION_COMMANDS, SECRET_FIELD_MARKERS
from ai_ent.persistence.models import Checkpoint, Task, utc_now
from ai_ent.persistence.repositories.errors import (
    DuplicateProductOperationalEventError,
    MissingProductOperationalEventError,
    MissingTaskError,
    RepositoryError,
)
from ai_ent.project_manifest import GENERATED_MARKER, canonical_bytes
from ai_ent.runtime_handoff import DEFAULT_RUNTIME_PROJECT_ID

PRODUCT_OBSERVABILITY_CONTRACT_VERSION = "prd-task-021.1"
PRODUCT_OBSERVABILITY_SCHEMA_VERSION = "product-observability-event-v0.1"
PRODUCT_OBSERVABILITY_CHECKPOINT_PREFIX = "prod-obs-"
PRODUCT_OBSERVABILITY_RECORD_TYPE = "product_operational_event"
REDACTED_VALUE = "***REDACTED***"

ProductOperationalEventType = Literal["AUDIT", "STRUCTURED_LOG", "METRIC", "OPERATIONAL"]
ProductEventSeverity = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
ProductRetentionClassification = Literal[
    "AUDIT_LONG_TERM",
    "METRIC_13_MONTHS",
    "OPERATIONAL_90_DAYS",
    "DIAGNOSTIC_30_DAYS",
]

RETENTION_CLASSIFICATIONS: tuple[ProductRetentionClassification, ...] = (
    "AUDIT_LONG_TERM",
    "METRIC_13_MONTHS",
    "OPERATIONAL_90_DAYS",
    "DIAGNOSTIC_30_DAYS",
)


@dataclass(frozen=True)
class ProductOperationalEvent:
    id: str
    project_id: str
    task_id: str
    event_type: ProductOperationalEventType
    event_name: str
    retention_classification: ProductRetentionClassification
    severity: ProductEventSeverity
    occurred_at: datetime
    actor_id: str | None
    execution_id: str | None
    gate_id: str | None
    message: str
    payload: dict[str, Any]
    payload_hash: str
    grants_control_plane_authority: bool = False
    applies_human_gate_decision: bool = False
    independent_verification_required: bool = True
    secret_values_exposed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "applies_human_gate_decision": self.applies_human_gate_decision,
            "contract_version": PRODUCT_OBSERVABILITY_CONTRACT_VERSION,
            "event_name": self.event_name,
            "event_type": self.event_type,
            "execution_id": self.execution_id,
            "gate_id": self.gate_id,
            "generated": GENERATED_MARKER,
            "grants_control_plane_authority": self.grants_control_plane_authority,
            "id": self.id,
            "independent_verification_required": self.independent_verification_required,
            "mandatory_verification_commands": list(MANDATORY_VERIFICATION_COMMANDS),
            "message": self.message,
            "occurred_at": _isoformat(self.occurred_at),
            "payload": self.payload,
            "payload_hash": self.payload_hash,
            "project_id": self.project_id,
            "retention_classification": self.retention_classification,
            "schema_version": PRODUCT_OBSERVABILITY_SCHEMA_VERSION,
            "secret_values_exposed": self.secret_values_exposed,
            "severity": self.severity,
            "task_id": self.task_id,
        }


@dataclass(frozen=True)
class ProductMetricsSnapshot:
    project_id: str
    total_events: int
    event_type_counts: dict[str, int]
    severity_counts: dict[str, int]
    retention_counts: dict[str, int]
    control_plane_authority_events: int
    human_gate_decision_events: int
    independent_verification_required_events: int
    secret_values_exposed_events: int
    latest_event_at: str | None

    @property
    def snapshot_hash(self) -> str:
        return hashlib.sha256(canonical_bytes(self.as_dict())).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": PRODUCT_OBSERVABILITY_CONTRACT_VERSION,
            "control_plane_authority_events": self.control_plane_authority_events,
            "event_type_counts": self.event_type_counts,
            "generated": GENERATED_MARKER,
            "human_gate_decision_events": self.human_gate_decision_events,
            "independent_verification_required_events": self.independent_verification_required_events,
            "latest_event_at": self.latest_event_at,
            "mandatory_verification_commands": list(MANDATORY_VERIFICATION_COMMANDS),
            "project_id": self.project_id,
            "retention_counts": self.retention_counts,
            "schema_version": "product-observability-metrics-v0.1",
            "secret_values_exposed_events": self.secret_values_exposed_events,
            "severity_counts": self.severity_counts,
            "total_events": self.total_events,
        }


class ProductOperationalEventRepository:
    """Durable product event stream backed by runtime checkpoints."""

    def record(
        self,
        session: Session,
        *,
        project_id: str,
        task_id: str,
        event_type: ProductOperationalEventType,
        event_name: str,
        message: str,
        severity: ProductEventSeverity = "INFO",
        payload: dict[str, Any] | None = None,
        actor_id: str | None = None,
        execution_id: str | None = None,
        gate_id: str | None = None,
        occurred_at: datetime | None = None,
        retention_classification: ProductRetentionClassification | None = None,
        event_id: str | None = None,
        grants_control_plane_authority: bool = False,
        applies_human_gate_decision: bool = False,
        independent_verification_required: bool = True,
        secret_values_exposed: bool = False,
    ) -> ProductOperationalEvent:
        if secret_values_exposed:
            raise RepositoryError("product operational event rejected: secret exposure flag is true")
        self._require_project_task(session, project_id, task_id)
        sanitized_payload = sanitize_observability_payload(payload or {})
        payload_hash = hashlib.sha256(canonical_bytes(sanitized_payload)).hexdigest()
        occurred = occurred_at or utc_now()
        event = ProductOperationalEvent(
            id=event_id
            or _event_id(
                project_id=project_id,
                task_id=task_id,
                event_type=event_type,
                event_name=event_name,
                payload_hash=payload_hash,
            ),
            project_id=project_id,
            task_id=task_id,
            event_type=event_type,
            event_name=event_name,
            retention_classification=retention_classification
            or classify_retention(event_type=event_type, event_name=event_name, severity=severity),
            severity=severity,
            occurred_at=occurred,
            actor_id=actor_id,
            execution_id=execution_id,
            gate_id=gate_id,
            message=message,
            payload=sanitized_payload,
            payload_hash=payload_hash,
            grants_control_plane_authority=grants_control_plane_authority,
            applies_human_gate_decision=applies_human_gate_decision,
            independent_verification_required=independent_verification_required,
            secret_values_exposed=False,
        )
        state = _checkpoint_state(event)
        existing = session.get(Checkpoint, event.id)
        if existing is not None:
            if existing.task_id == task_id and existing.state == state:
                return event
            raise DuplicateProductOperationalEventError(f"product operational event already exists: {event.id}")
        checkpoint = Checkpoint(
            id=event.id,
            task_id=task_id,
            execution_id=execution_id,
            checkpoint_type="runtime",
            state=state,
        )
        session.add(checkpoint)
        try:
            session.flush()
        except IntegrityError as exc:
            raise DuplicateProductOperationalEventError(
                f"product operational event already exists or is invalid: {event.id}"
            ) from exc
        except SQLAlchemyError as exc:
            raise RepositoryError("product operational event persistence failed") from exc
        return event

    def get(self, session: Session, event_id: str) -> ProductOperationalEvent | None:
        checkpoint = session.get(Checkpoint, event_id)
        if checkpoint is None:
            return None
        return _event_from_checkpoint(checkpoint)

    def require(self, session: Session, event_id: str) -> ProductOperationalEvent:
        event = self.get(session, event_id)
        if event is None:
            raise MissingProductOperationalEventError(f"product operational event not found: {event_id}")
        return event

    def list_by_project(self, session: Session, project_id: str) -> tuple[ProductOperationalEvent, ...]:
        statement = (
            select(Checkpoint)
            .join(Task, Checkpoint.task_id == Task.id)
            .where(Task.project_id == project_id)
            .order_by(Checkpoint.created_at, Checkpoint.id)
        )
        events = [_event_from_checkpoint(checkpoint) for checkpoint in session.scalars(statement).all()]
        return tuple(event for event in events if event is not None and event.project_id == project_id)

    def _require_project_task(self, session: Session, project_id: str, task_id: str) -> None:
        task = session.get(Task, task_id)
        if task is None or task.project_id != project_id:
            raise MissingTaskError(f"task not found for product event: {task_id}")


class ProductObservabilityService:
    def __init__(self, repository: ProductOperationalEventRepository | None = None) -> None:
        self.repository = repository or ProductOperationalEventRepository()

    def record_audit_event(self, session: Session, **kwargs: Any) -> ProductOperationalEvent:
        return self.repository.record(session, event_type="AUDIT", **kwargs)

    def record_structured_log(self, session: Session, **kwargs: Any) -> ProductOperationalEvent:
        return self.repository.record(session, event_type="STRUCTURED_LOG", **kwargs)

    def record_metric(self, session: Session, **kwargs: Any) -> ProductOperationalEvent:
        return self.repository.record(session, event_type="METRIC", **kwargs)

    def record_operational_event(self, session: Session, **kwargs: Any) -> ProductOperationalEvent:
        return self.repository.record(session, event_type="OPERATIONAL", **kwargs)

    def record_runtime_plan_import(
        self,
        session: Session,
        *,
        project_id: str = DEFAULT_RUNTIME_PROJECT_ID,
        task_id: str = "PRD-TASK-021",
        import_id: str,
        tasks_imported: int,
        dependency_edges_imported: int,
        human_gates_imported: int,
        ready_tasks: tuple[str, ...],
        gated_tasks: tuple[str, ...],
        waiting_tasks: tuple[str, ...],
        postgresql_authority: str,
    ) -> tuple[ProductOperationalEvent, ...]:
        payload = {
            "dependency_edges_imported": dependency_edges_imported,
            "gated_tasks": list(gated_tasks),
            "human_gates_imported": human_gates_imported,
            "import_id": import_id,
            "postgresql_authority": postgresql_authority,
            "ready_tasks": list(ready_tasks),
            "tasks_imported": tasks_imported,
            "waiting_tasks": list(waiting_tasks),
        }
        return (
            self.record_audit_event(
                session,
                project_id=project_id,
                task_id=task_id,
                event_name="product.runtime_plan_imported",
                message="Product runtime plan imported with explicit gates and preserved authority.",
                payload=payload,
                event_id=_named_event_id(import_id, "audit"),
            ),
            self.record_structured_log(
                session,
                project_id=project_id,
                task_id=task_id,
                event_name="product.runtime_plan_import.log",
                message="product runtime import completed",
                payload=payload,
                event_id=_named_event_id(import_id, "structured-log"),
            ),
            self.record_metric(
                session,
                project_id=project_id,
                task_id=task_id,
                event_name="product.runtime_plan_import.metrics",
                message="product runtime import metrics captured",
                payload={
                    "dependency_edges_imported": dependency_edges_imported,
                    "gated_task_count": len(gated_tasks),
                    "human_gates_imported": human_gates_imported,
                    "ready_task_count": len(ready_tasks),
                    "tasks_imported": tasks_imported,
                    "waiting_task_count": len(waiting_tasks),
                },
                event_id=_named_event_id(import_id, "metric"),
            ),
        )

    def events(self, session: Session, project_id: str) -> tuple[ProductOperationalEvent, ...]:
        return self.repository.list_by_project(session, project_id)

    def audit_events(self, session: Session, project_id: str) -> tuple[dict[str, Any], ...]:
        return tuple(event.as_dict() for event in self.events(session, project_id) if event.event_type == "AUDIT")

    def structured_logs(self, session: Session, project_id: str) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "event_id": event.id,
                "event_name": event.event_name,
                "message": event.message,
                "occurred_at": _isoformat(event.occurred_at),
                "payload": event.payload,
                "payload_hash": event.payload_hash,
                "severity": event.severity,
            }
            for event in self.events(session, project_id)
            if event.event_type == "STRUCTURED_LOG"
        )

    def metrics(self, session: Session, project_id: str) -> ProductMetricsSnapshot:
        events = self.events(session, project_id)
        latest = max((_isoformat(event.occurred_at) for event in events), default=None)
        return ProductMetricsSnapshot(
            project_id=project_id,
            total_events=len(events),
            event_type_counts=dict(sorted(Counter(event.event_type for event in events).items())),
            severity_counts=dict(sorted(Counter(event.severity for event in events).items())),
            retention_counts=dict(
                sorted(Counter(event.retention_classification for event in events).items())
            ),
            control_plane_authority_events=sum(
                1 for event in events if event.grants_control_plane_authority
            ),
            human_gate_decision_events=sum(1 for event in events if event.applies_human_gate_decision),
            independent_verification_required_events=sum(
                1 for event in events if event.independent_verification_required
            ),
            secret_values_exposed_events=sum(1 for event in events if event.secret_values_exposed),
            latest_event_at=latest,
        )


def retention_classifications() -> tuple[dict[str, str], ...]:
    return (
        {
            "id": "AUDIT_LONG_TERM",
            "description": "Identity-bearing product audit trail retained for long-term accountability.",
        },
        {
            "id": "METRIC_13_MONTHS",
            "description": "Aggregated product metrics retained across a rolling annual comparison window.",
        },
        {
            "id": "OPERATIONAL_90_DAYS",
            "description": "Operational lifecycle events retained for near-term incident and recovery review.",
        },
        {
            "id": "DIAGNOSTIC_30_DAYS",
            "description": "Structured diagnostic logs retained for short-term troubleshooting.",
        },
    )


def classify_retention(
    *,
    event_type: ProductOperationalEventType,
    event_name: str,
    severity: ProductEventSeverity = "INFO",
) -> ProductRetentionClassification:
    name = event_name.lower()
    if event_type == "AUDIT" or "approval" in name or "human_gate" in name:
        return "AUDIT_LONG_TERM"
    if event_type == "METRIC":
        return "METRIC_13_MONTHS"
    if event_type == "OPERATIONAL" or severity in {"WARNING", "ERROR", "CRITICAL"}:
        return "OPERATIONAL_90_DAYS"
    return "DIAGNOSTIC_30_DAYS"


def sanitize_observability_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in sorted(value.items()):
            string_key = str(key)
            if _secret_key(string_key):
                sanitized[string_key] = REDACTED_VALUE
            else:
                sanitized[string_key] = sanitize_observability_payload(child)
        return sanitized
    if isinstance(value, list | tuple):
        return [sanitize_observability_payload(item) for item in value]
    return value


def _checkpoint_state(event: ProductOperationalEvent) -> str:
    return canonical_bytes(
        {
            "record_type": PRODUCT_OBSERVABILITY_RECORD_TYPE,
            "event": event.as_dict(),
        }
    ).decode("utf-8")


def _event_from_checkpoint(checkpoint: Checkpoint) -> ProductOperationalEvent | None:
    try:
        payload = json.loads(checkpoint.state)
    except json.JSONDecodeError:
        return None
    if payload.get("record_type") != PRODUCT_OBSERVABILITY_RECORD_TYPE:
        return None
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    event_payload = event.get("payload")
    if not isinstance(event_payload, dict):
        return None
    return ProductOperationalEvent(
        id=str(event["id"]),
        project_id=str(event["project_id"]),
        task_id=str(event["task_id"]),
        event_type=event["event_type"],
        event_name=str(event["event_name"]),
        retention_classification=event["retention_classification"],
        severity=event["severity"],
        occurred_at=_parse_datetime(str(event["occurred_at"])),
        actor_id=_optional_str(event.get("actor_id")),
        execution_id=_optional_str(event.get("execution_id")),
        gate_id=_optional_str(event.get("gate_id")),
        message=str(event["message"]),
        payload=event_payload,
        payload_hash=str(event["payload_hash"]),
        grants_control_plane_authority=bool(event.get("grants_control_plane_authority", False)),
        applies_human_gate_decision=bool(event.get("applies_human_gate_decision", False)),
        independent_verification_required=bool(event.get("independent_verification_required", True)),
        secret_values_exposed=bool(event.get("secret_values_exposed", False)),
    )


def _event_id(
    *,
    project_id: str,
    task_id: str,
    event_type: str,
    event_name: str,
    payload_hash: str,
) -> str:
    material = {
        "event_name": event_name,
        "event_type": event_type,
        "payload_hash": payload_hash,
        "project_id": project_id,
        "task_id": task_id,
    }
    return f"{PRODUCT_OBSERVABILITY_CHECKPOINT_PREFIX}{hashlib.sha256(canonical_bytes(material)).hexdigest()[:32]}"


def _named_event_id(import_id: str, name: str) -> str:
    digest = hashlib.sha256(f"{import_id}:{name}".encode()).hexdigest()[:32]
    return f"{PRODUCT_OBSERVABILITY_CHECKPOINT_PREFIX}{digest}"


def _secret_key(key: str) -> bool:
    normalized = key.lower()
    return any(marker in normalized for marker in SECRET_FIELD_MARKERS)


def _isoformat(value: datetime) -> str:
    normalized = value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)
