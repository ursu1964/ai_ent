from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.execution_planner import ExecutionPlannerService
from ai_ent.governance_contract import GovernanceEvolutionRequest
from ai_ent.governance_evolution import (
    GOVERNANCE_EVOLUTION_OUTPUT_SCHEMA_VERSION,
    GOVERNANCE_EVOLUTION_SERVICE_VERSION,
    GovernanceEvolutionService,
    evaluate_governance_evolution,
)
from ai_ent.persistence.models import Base, Execution, RuntimeHumanGate, Task, TaskLease
from ai_ent.project_manifest import EnvironmentProfile

MANIFEST_ROOT = Path("manifest/project/ai-ent")


def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def test_c08_service_recommendations_are_deterministic_and_manifest_bounded() -> None:
    request = _valid_request(
        requested_authorities=(
            "simulate",
            "record_evidence",
            "recommend",
            "analyze_impact",
        ),
        target_manifest_paths=(
            "manifest/project/ai-ent/governance/policy.yaml",
            "manifest/project/ai-ent/capabilities.yaml",
        ),
    )

    first = evaluate_governance_evolution(request, manifest_root=MANIFEST_ROOT)
    second = evaluate_governance_evolution(
        _valid_request(
            requested_authorities=(
                "analyze_impact",
                "recommend",
                "record_evidence",
                "simulate",
            ),
            target_manifest_paths=(
                "manifest/project/ai-ent/capabilities.yaml",
                "manifest/project/ai-ent/governance/policy.yaml",
            ),
        ),
        manifest_root=MANIFEST_ROOT,
    )

    assert first.ok
    assert first.as_dict() == second.as_dict()
    assert first.output_hash == second.output_hash
    assert first.contract_version == GOVERNANCE_EVOLUTION_SERVICE_VERSION
    assert first.schema_version == GOVERNANCE_EVOLUTION_OUTPUT_SCHEMA_VERSION
    assert first.accepted_authorities == (
        "analyze_impact",
        "recommend",
        "record_evidence",
        "simulate",
    )
    assert first.denied_authorities == ()
    assert first.manifest_policy.capability_id == "C08"
    assert first.manifest_policy.manifest_root == "manifest/project/ai-ent"
    assert first.manifest_policy.repository_branch_policy == "verified_commits_only"
    assert ".env" in first.manifest_policy.protected_paths
    assert "VG-003" in first.manifest_policy.verification_gate_ids
    assert len(first.manifest_policy.policy_hash) == 64
    assert first.recommendations[0].target_manifest_paths == (
        "manifest/project/ai-ent/capabilities.yaml",
        "manifest/project/ai-ent/governance/policy.yaml",
    )
    assert first.recommendations[0].required_human_approval is True
    assert first.evidence_records[0].output_authority_state == "NON_AUTHORITATIVE"
    assert first.as_dict()["summary"]["recommendations_only"] is True
    assert first.as_dict()["summary"]["evidence_only"] is True
    assert first.as_dict()["summary"]["runtime_completion_authority_granted"] is False


def test_c08_service_rejects_authority_escalation_without_recommendations() -> None:
    result = GovernanceEvolutionService().evaluate(
        _valid_request(
            requested_authorities=(
                "recommend",
                "approve_gate",
                "bypass_verifier",
                "bypass_commit_boundary",
                "schedule_execution",
                "execute_runtime",
                "commit",
                "push",
                "weaken_policy",
            ),
            policy_delta={
                "allowed_authorities": ["recommend", "commit", "schedule_execution"],
                "commit": True,
                "human_approval_required": False,
                "mutate_manifest": True,
                "policy_weakening_allowed": True,
                "push": True,
                "runtime_execution_authority": True,
            },
        ),
        manifest_root=MANIFEST_ROOT,
    )

    assert not result.ok
    assert result.status == "REJECTED"
    assert result.accepted_authorities == ()
    assert result.recommendations == ()
    assert result.evidence_records[0].output_authority_state == "NON_AUTHORITATIVE"
    assert set(result.denied_authorities) >= {
        "approve_gate",
        "bypass_commit_boundary",
        "bypass_verifier",
        "commit",
        "execute_runtime",
        "push",
        "schedule_execution",
        "weaken_policy",
    }
    assert set(result.blockers) >= {
        "prohibited authority requested:approve_gate",
        "prohibited authority requested:bypass_commit_boundary",
        "prohibited authority requested:bypass_verifier",
        "prohibited authority requested:commit",
        "prohibited authority requested:execute_runtime",
        "prohibited authority requested:push",
        "prohibited authority requested:schedule_execution",
        "prohibited authority requested:weaken_policy",
        "policy delta grants prohibited authority:commit",
        "policy delta grants prohibited authority:execute_runtime",
        "policy delta grants prohibited authority:mutate_manifest",
        "policy delta grants prohibited authority:push",
        "policy delta grants prohibited authority:schedule_execution",
        "policy delta weakens human approval boundary",
        "policy delta weakens policy weakening boundary",
    }
    assert result.as_dict()["summary"] == {
        "recommended": False,
        "output_authority_state": "NON_AUTHORITATIVE",
        "recommendations_only": True,
        "evidence_only": True,
        "runtime_completion_authority_granted": False,
        "gate_approval_granted": False,
        "verifier_bypass_granted": False,
        "commit_boundary_bypass_granted": False,
        "self_scheduling_granted": False,
        "runtime_execution_granted": False,
        "policy_weakening_granted": False,
        "human_approval_required": True,
    }
    assert all(not boundary.granted for boundary in result.service_boundaries)


def test_c08_service_boundary_authorities_are_deterministic_and_never_granted() -> None:
    first = GovernanceEvolutionService().evaluate(_valid_request(), manifest_root=MANIFEST_ROOT)
    second = GovernanceEvolutionService().evaluate(_valid_request(), manifest_root=MANIFEST_ROOT)

    assert first.as_dict()["service_boundaries"] == second.as_dict()["service_boundaries"]
    assert first.as_dict()["summary"] == second.as_dict()["summary"]
    assert {
        (boundary.boundary_id, boundary.authority, boundary.granted)
        for boundary in first.service_boundaries
    } == {
        ("human-gate", "approve_gate", False),
        ("deterministic-verifier", "bypass_verifier", False),
        ("verified-commit", "bypass_commit_boundary", False),
        ("runtime-completion", "execute_runtime", False),
        ("runtime-scheduling", "schedule_execution", False),
        ("manifest-policy", "weaken_policy", False),
    }
    assert first.evidence_records[0].output_authority_state == "NON_AUTHORITATIVE"


def test_c08_service_does_not_mutate_runtime_completion_state(tmp_path: Path) -> None:
    planner = ExecutionPlannerService()
    plan = planner.plan(MANIFEST_ROOT, environment=_environment(codex_configured=False))
    factory = session_factory()

    with factory() as session:
        planner.import_plan(
            session,
            plan,
            require_clean_git=False,
            require_codex_command=False,
            repository_root=tmp_path,
        )
        before = _runtime_state(session)

        result = GovernanceEvolutionService().evaluate(
            _valid_request(),
            manifest_root=MANIFEST_ROOT,
        )

        after = _runtime_state(session)

    assert result.ok
    assert before == after
    assert before["execution_count"] == 0
    assert before["lease_count"] == 0
    assert before["passed_task_count"] == 0
    assert before["approved_gate_count"] == 0


def _runtime_state(session: Session) -> dict[str, int]:
    return {
        "execution_count": session.scalar(select(func.count()).select_from(Execution)) or 0,
        "lease_count": session.scalar(select(func.count()).select_from(TaskLease)) or 0,
        "task_count": session.scalar(select(func.count()).select_from(Task)) or 0,
        "passed_task_count": session.scalar(
            select(func.count()).select_from(Task).where(Task.status == "passed")
        )
        or 0,
        "human_gate_count": session.scalar(select(func.count()).select_from(RuntimeHumanGate)) or 0,
        "approved_gate_count": session.scalar(
            select(func.count()).select_from(RuntimeHumanGate).where(RuntimeHumanGate.status == "approved")
        )
        or 0,
    }


def _valid_request(
    *,
    requested_authorities: tuple[str, ...] = ("recommend",),
    policy_delta: dict[str, object] | None = None,
    target_manifest_paths: tuple[str, ...] = ("manifest/project/ai-ent/capabilities.yaml",),
) -> GovernanceEvolutionRequest:
    return GovernanceEvolutionRequest(
        request_id="GOV-REQ-C08-001",
        actor_id="AGT-003",
        domain="evolution",
        evolution_type="technical",
        summary="Recommend a bounded C08 governance evolution.",
        target_manifest_paths=target_manifest_paths,
        requested_authorities=requested_authorities,
        policy_delta=policy_delta or {},
        learning_sources=("approved_manifests", "verified_implementation_outcomes"),
        evidence_refs=("BEAG-001:tests/test_governance_evolution.py",),
    )


def _environment(*, codex_configured: bool = True) -> EnvironmentProfile:
    return EnvironmentProfile(
        profile_id="test",
        repository_path="/repo",
        git_available=True,
        postgresql_available=True,
        alembic_available=True,
        docker_available=True,
        docker_compose_available=True,
        codex_command_configured=codex_configured,
        codex_executable_available=True,
        python_path="aient/bin/python",
        python_available=True,
        ram_mb=4096,
        gpu_available=False,
        external_network="available",
        configured_secret_names=(),
    )
