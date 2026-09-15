from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from ai_ent.external_project import MANDATORY_VERIFICATION_COMMANDS
from ai_ent.persistence.models import Checkpoint
from ai_ent.persistence.repositories.errors import DuplicateProductOperationalEventError
from ai_ent.product_observability import (
    ProductObservabilityService,
    ProductOperationalEventRepository,
    classify_retention,
    retention_classifications,
)
from ai_ent.product_runtime_handoff import ProductRuntimePlanImporter
from ai_ent.runtime_handoff import DEFAULT_RUNTIME_PROJECT_ID
from tests.test_product_runtime_handoff import (
    artifacts,
    seed_prior_and_residual_complete,
    session_factory,
)


def test_product_event_repository_redacts_and_hashes_deterministically() -> None:
    factory = session_factory()
    repository = ProductOperationalEventRepository()
    occurred_at = datetime(2026, 9, 12, 10, 30, tzinfo=UTC)

    with factory() as session:
        seed_prior_and_residual_complete(session)
        session.flush()
        event = repository.record(
            session,
            project_id=DEFAULT_RUNTIME_PROJECT_ID,
            task_id="RES-TEST-000",
            event_type="AUDIT",
            event_name="product.human_gate.reviewed",
            message="review recorded",
            actor_id="operator-1",
            occurred_at=occurred_at,
            payload={
                "decision": "review_only",
                "nested": {"api_token": "super-secret-token"},
                "password": "secret-password",
            },
        )
        duplicate = repository.record(
            session,
            project_id=DEFAULT_RUNTIME_PROJECT_ID,
            task_id="RES-TEST-000",
            event_type="AUDIT",
            event_name="product.human_gate.reviewed",
            message="review recorded",
            actor_id="operator-1",
            occurred_at=occurred_at,
            payload={
                "decision": "review_only",
                "nested": {"api_token": "super-secret-token"},
                "password": "secret-password",
            },
        )
        checkpoint = session.get(Checkpoint, event.id)

    assert event == duplicate
    assert event.retention_classification == "AUDIT_LONG_TERM"
    assert event.grants_control_plane_authority is False
    assert event.applies_human_gate_decision is False
    assert event.independent_verification_required is True
    assert event.secret_values_exposed is False
    assert event.as_dict()["mandatory_verification_commands"] == list(MANDATORY_VERIFICATION_COMMANDS)
    assert event.payload["password"] == "***REDACTED***"
    assert event.payload["nested"]["api_token"] == "***REDACTED***"
    assert checkpoint is not None
    assert "super-secret-token" not in checkpoint.state
    assert "secret-password" not in checkpoint.state


def test_product_event_repository_rejects_conflicting_event_ids() -> None:
    factory = session_factory()
    repository = ProductOperationalEventRepository()

    with factory() as session:
        seed_prior_and_residual_complete(session)
        repository.record(
            session,
            project_id=DEFAULT_RUNTIME_PROJECT_ID,
            task_id="RES-TEST-000",
            event_type="STRUCTURED_LOG",
            event_name="product.log",
            message="first",
            payload={"value": 1},
            event_id="prod-obs-conflict",
        )
        try:
            repository.record(
                session,
                project_id=DEFAULT_RUNTIME_PROJECT_ID,
                task_id="RES-TEST-000",
                event_type="STRUCTURED_LOG",
                event_name="product.log",
                message="second",
                payload={"value": 2},
                event_id="prod-obs-conflict",
            )
        except DuplicateProductOperationalEventError:
            session.rollback()
        else:
            raise AssertionError("conflicting product operational event was not rejected")


def test_product_observability_metrics_and_log_surfaces_are_deterministic() -> None:
    factory = session_factory()
    service = ProductObservabilityService()

    with factory() as session:
        seed_prior_and_residual_complete(session)
        service.record_structured_log(
            session,
            project_id=DEFAULT_RUNTIME_PROJECT_ID,
            task_id="RES-TEST-000",
            event_name="product.request.completed",
            message="request complete",
            payload={"path": "/api/product/runtime/state"},
        )
        service.record_metric(
            session,
            project_id=DEFAULT_RUNTIME_PROJECT_ID,
            task_id="RES-TEST-001",
            event_name="product.runtime.ready_tasks",
            message="ready task count",
            payload={"ready_tasks": 2},
        )
        first = service.metrics(session, DEFAULT_RUNTIME_PROJECT_ID)
        second = service.metrics(session, DEFAULT_RUNTIME_PROJECT_ID)
        logs = service.structured_logs(session, DEFAULT_RUNTIME_PROJECT_ID)

    assert first.as_dict() == second.as_dict()
    assert first.snapshot_hash == second.snapshot_hash
    assert first.event_type_counts == {"METRIC": 1, "STRUCTURED_LOG": 1}
    assert first.retention_counts == {"DIAGNOSTIC_30_DAYS": 1, "METRIC_13_MONTHS": 1}
    assert first.control_plane_authority_events == 0
    assert first.human_gate_decision_events == 0
    assert first.independent_verification_required_events == 2
    assert first.secret_values_exposed_events == 0
    assert logs[0]["event_name"] == "product.request.completed"


def test_retention_classifications_are_explicit() -> None:
    assert [item["id"] for item in retention_classifications()] == [
        "AUDIT_LONG_TERM",
        "METRIC_13_MONTHS",
        "OPERATIONAL_90_DAYS",
        "DIAGNOSTIC_30_DAYS",
    ]
    assert classify_retention(event_type="AUDIT", event_name="product.approval.reviewed") == "AUDIT_LONG_TERM"
    assert classify_retention(event_type="METRIC", event_name="product.requests") == "METRIC_13_MONTHS"
    assert (
        classify_retention(
            event_type="STRUCTURED_LOG",
            event_name="product.request.failed",
            severity="ERROR",
        )
        == "OPERATIONAL_90_DAYS"
    )


def test_product_runtime_import_emits_observability_events_without_authority() -> None:
    factory = session_factory()
    service = ProductObservabilityService()

    with factory() as session:
        seed_prior_and_residual_complete(session)
        result = ProductRuntimePlanImporter().import_product_plan(
            session,
            artifacts(),
            require_clean_git=False,
        )
        events = service.events(session, DEFAULT_RUNTIME_PROJECT_ID)
        metrics = service.metrics(session, DEFAULT_RUNTIME_PROJECT_ID)
        checkpoints = session.scalars(
            select(Checkpoint).where(Checkpoint.id.in_([event.id for event in events]))
        ).all()

    assert result.ok
    assert {event.event_name for event in events} == {
        "product.runtime_plan_import.log",
        "product.runtime_plan_import.metrics",
        "product.runtime_plan_imported",
    }
    assert metrics.total_events == 3
    assert metrics.control_plane_authority_events == 0
    assert metrics.human_gate_decision_events == 0
    assert metrics.independent_verification_required_events == 3
    assert metrics.secret_values_exposed_events == 0
    assert all("AIENT_DB_PASSWORD" not in checkpoint.state for checkpoint in checkpoints)
