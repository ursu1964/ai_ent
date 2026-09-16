from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.persistence.models import (
    Base,
    Checkpoint,
    Execution,
    RuntimeBaselineIntegration,
    RuntimePlanImport,
    RuntimeTaskPlanBinding,
    Task,
    TaskLease,
)
from ai_ent.persistence.repositories import ProjectRepository, TaskRepository
from ai_ent.scheduler.claiming import TaskClaimingService
from ai_ent.scheduler.hardening_baseline import HardeningBaselineService
from ai_ent.scheduler.iteration import ExecutionPackageFactory
from ai_ent.scheduler.readiness import TaskReadinessService

PLAN_ID = "PUBLIC-CLOUD-HARDENING-PLAN-877cc1468dc3"
PLAN_VERSION = "1"
BASELINE_COMMIT = "e504ef38c9d47b8bcfad61ec4550dfafd49eec97"
BASELINE_TREE = "2897a5bd38a30cc34acd9bc655c5c17c6f7f6e59"


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert completed.returncode == 0, completed.stdout
    return completed.stdout.strip()


def make_repo(tmp_path: Path) -> tuple[Path, str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.local")
    git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "README.md").write_text("baseline\n", encoding="utf-8")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-m", "baseline")
    commit = git(tmp_path, "rev-parse", "HEAD")
    tree = git(tmp_path, "rev-parse", "HEAD^{tree}")
    return tmp_path, commit, tree


def commit_file(repo: Path, path: str, content: str, message: str) -> tuple[str, str]:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git(repo, "add", path)
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD^{tree}")


def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def seed_import(session: Session) -> RuntimePlanImport:
    ProjectRepository().create(session, project_id="PRJ-AI-ENT", name="AI Enterprise")
    plan_import = RuntimePlanImport(
        id="pchi-public-cloud-hardening-plan-877cc1468dc3-v1",
        project_id="PRJ-AI-ENT",
        plan_project_id="PRJ-AI-ENT",
        plan_id=PLAN_ID,
        plan_version=PLAN_VERSION,
        status="imported",
        compiled_project_hash="0" * 64,
        capability_resolution_hash="1" * 64,
        trace_validation_hash="2" * 64,
        implementation_plan_hash="877cc1468dc3a7b6efd93fae98d0d381553ac0bfecb7ea68613009fb745e9c2a",
        feasibility_hash="3" * 64,
        dry_run_hash="4" * 64,
        task_fingerprint_hash="5" * 64,
        dependency_graph_hash="6" * 64,
        importer_version="test",
        task_count=3,
        dependency_count=2,
        human_gate_count=0,
        effective_concurrency=1,
    )
    session.add(plan_import)
    session.flush()
    return plan_import


def seed_pch_task(
    session: Session,
    plan_import: RuntimePlanImport,
    task_id: str,
    *,
    status: str = "pending",
    baseline_commit: str = BASELINE_COMMIT,
    baseline_tree: str = BASELINE_TREE,
) -> Task:
    task = TaskRepository().create(
        session,
        task_id=task_id,
        project_id="PRJ-AI-ENT",
        title=f"Task {task_id}",
        objective=f"{task_id}: objective",
        status=status,  # type: ignore[arg-type]
        fingerprint=f"fingerprint-{task_id}",
    )
    session.add(
        RuntimeTaskPlanBinding(
            task_id=task.id,
            import_id=plan_import.id,
            plan_id=plan_import.plan_id,
            plan_version=plan_import.plan_version,
            fingerprint=task.fingerprint or "",
            risk_level="MEDIUM",
            agent_role="AGT-PCH",
            model_profile="MDL-PCH",
            executor="governed_scheduler_isolated_worktree",
            verification_profile="FULL_REGRESSION",
            feasibility_status="FEASIBLE",
            policy_decision="GUARDED_ALLOWED",
            implements_json=json.dumps({"requirements": ["PCH-RELEASE-001"]}),
            write_scope_json=json.dumps({"allowed": ["src/ai_ent/**"], "prohibited": [".env"]}),
            acceptance_json=json.dumps(
                {
                    "baseline_commit": baseline_commit,
                    "baseline_tree": baseline_tree,
                    "cumulative_baseline_policy": {
                        "owner_task": "PCH-TASK-001",
                        "on_violation": "STOP_BEFORE_CLAIM",
                        "rule": "downstream task base tree must contain all passed dependency trees before claim",
                    },
                    "acceptance_criteria": [
                        "Verified commit is cumulatively integrated into the authoritative hardening baseline before dependent tasks execute"
                    ],
                },
                sort_keys=True,
            ),
        )
    )
    session.flush()
    return task


def seed_pch_graph(session: Session) -> None:
    plan_import = seed_import(session)
    seed_pch_task(session, plan_import, "PCH-TASK-001", status="passed")
    seed_pch_task(session, plan_import, "PCH-TASK-002")
    seed_pch_task(session, plan_import, "PCH-TASK-013")
    tasks = TaskRepository()
    tasks.add_dependency(session, task_id="PCH-TASK-002", depends_on_task_id="PCH-TASK-001")
    tasks.add_dependency(session, task_id="PCH-TASK-013", depends_on_task_id="PCH-TASK-001")


def seed_pch_graph_for_baseline(
    session: Session,
    *,
    baseline_commit: str,
    baseline_tree: str,
    task_001_status: str = "passed",
) -> None:
    plan_import = seed_import(session)
    seed_pch_task(
        session,
        plan_import,
        "PCH-TASK-001",
        status=task_001_status,
        baseline_commit=baseline_commit,
        baseline_tree=baseline_tree,
    )
    seed_pch_task(
        session,
        plan_import,
        "PCH-TASK-002",
        baseline_commit=baseline_commit,
        baseline_tree=baseline_tree,
    )
    seed_pch_task(
        session,
        plan_import,
        "PCH-TASK-013",
        status="passed",
        baseline_commit=baseline_commit,
        baseline_tree=baseline_tree,
    )
    tasks = TaskRepository()
    tasks.add_dependency(session, task_id="PCH-TASK-002", depends_on_task_id="PCH-TASK-001")
    tasks.add_dependency(session, task_id="PCH-TASK-013", depends_on_task_id="PCH-TASK-001")


def seed_verified_execution(
    session: Session,
    *,
    task_id: str,
    execution_id: str,
    source_commit: str,
    source_tree: str,
    baseline_commit: str,
    baseline_tree: str,
    baseline_generation: int = 0,
    status: str = "succeeded",
    terminal_state: str | None = "success",
    checkpoint_status: str = "completed",
    verification_ok: bool = True,
    committed_tree: str | None = None,
) -> Execution:
    now = datetime.now(UTC)
    execution = Execution(
        id=execution_id,
        task_id=task_id,
        executor_type="codex",
        status=status,  # type: ignore[arg-type]
        attempt=1,
        started_at=now,
        finished_at=now,
        terminal_state=terminal_state,
        candidate_tree_hash=source_tree,
        commit_hash=source_commit,
        baseline_commit=baseline_commit,
        baseline_tree=baseline_tree,
        baseline_generation=baseline_generation,
    )
    session.add(execution)
    session.flush()
    session.add(
        Checkpoint(
            id=f"checkpoint-{execution_id}",
            task_id=task_id,
            execution_id=execution_id,
            checkpoint_type="execution",
            state=json.dumps(
                {
                    "status": checkpoint_status,
                    "candidate_tree_hash": source_tree,
                    "committed_tree_hash": committed_tree or source_tree,
                    "commit_id": source_commit,
                    "verification": [{"command": "pytest", "ok": verification_ok, "returncode": 0, "output": ""}],
                },
                sort_keys=True,
            ),
            commit_hash=source_commit,
            tree_hash=source_tree,
        )
    )
    session.flush()
    return execution


def seed_integration(
    session: Session,
    *,
    integration_id: str,
    task_id: str,
    execution_id: str,
    source_commit: str,
    source_tree: str,
    prior_commit: str,
    prior_tree: str,
    integrated_commit: str,
    integrated_tree: str,
    generation: int = 0,
    status: str = "verified",
    plan_id: str = PLAN_ID,
    plan_version: str = PLAN_VERSION,
) -> RuntimeBaselineIntegration:
    row = RuntimeBaselineIntegration(
        id=integration_id,
        import_id="pchi-public-cloud-hardening-plan-877cc1468dc3-v1",
        project_id="PRJ-AI-ENT",
        plan_id=plan_id,
        plan_version=plan_version,
        task_id=task_id,
        source_execution_id=execution_id,
        source_commit=source_commit,
        source_tree=source_tree,
        prior_baseline_generation=generation,
        prior_baseline_commit=prior_commit,
        prior_baseline_tree=prior_tree,
        integrated_commit=integrated_commit,
        integrated_tree=integrated_tree,
        integration_strategy="fast_forward" if source_commit == integrated_commit else "semantic_integration",
        status=status,  # type: ignore[arg-type]
        verification_evidence_json=json.dumps({"verified": status == "verified"}, sort_keys=True),
    )
    session.add(row)
    session.flush()
    return row


def advance_task_001(
    baseline: HardeningBaselineService,
    session: Session,
    *,
    source_commit: str,
    source_tree: str,
    integrated_commit: str,
    integrated_tree: str,
    integration_id: str,
    expected_generation: int = 0,
):
    return baseline.advance(
        session,
        plan_id=PLAN_ID,
        plan_version=PLAN_VERSION,
        task_id="PCH-TASK-001",
        task_commit=source_commit,
        task_tree=source_tree,
        resulting_baseline_commit=integrated_commit,
        resulting_baseline_tree=integrated_tree,
        expected_generation=expected_generation,
        integration_id=integration_id,
    )


def test_passed_dependency_without_baseline_representation_blocks_readiness_and_claim() -> None:
    factory = session_factory()
    readiness = TaskReadinessService(task_id_prefix="PCH-TASK-")
    claiming = TaskClaimingService()

    with factory() as session:
        seed_pch_graph(session)

        task_002 = readiness.evaluate_task(session, "PCH-TASK-002")
        task_013 = readiness.evaluate_task(session, "PCH-TASK-013")
        ready = readiness.list_ready_tasks(session, project_id="PRJ-AI-ENT")
        claim = claiming.claim_task(
            session,
            task_id="PCH-TASK-002",
            owner_id="worker",
            lease_duration=timedelta(minutes=5),
        )

        assert task_002.status == "NOT_SCHEDULABLE"
        assert task_002.blocking_dependencies == ("PCH-TASK-001",)
        assert task_002.reasons == ("dependency_not_integrated_into_baseline:PCH-TASK-001",)
        assert task_013.status == "NOT_SCHEDULABLE"
        assert task_013.reasons == ("dependency_not_integrated_into_baseline:PCH-TASK-001",)
        assert ready == []
        assert claim.status == "NOT_CLAIMABLE"
        assert claim.reason == "dependency_not_integrated_into_baseline:PCH-TASK-001"
        assert session.scalars(select(Execution)).all() == []
        assert session.scalars(select(TaskLease)).all() == []


def test_baseline_advancement_requires_verified_execution_and_integration(tmp_path: Path) -> None:
    repo, baseline_commit, baseline_tree = make_repo(tmp_path)
    source_commit, source_tree = commit_file(repo, "pch-task-001.txt", "task 001\n", "task 001")
    source_013_commit, source_013_tree = commit_file(repo, "pch-task-013.txt", "task 013\n", "task 013")
    factory = session_factory()
    baseline = HardeningBaselineService(repository_path=repo)
    readiness = TaskReadinessService(task_id_prefix="PCH-TASK-")
    claiming = TaskClaimingService()

    with factory() as session:
        seed_pch_graph_for_baseline(session, baseline_commit=baseline_commit, baseline_tree=baseline_tree)
        seed_verified_execution(
            session,
            task_id="PCH-TASK-001",
            execution_id="execution-pch-001",
            source_commit=source_commit,
            source_tree=source_tree,
            baseline_commit=baseline_commit,
            baseline_tree=baseline_tree,
        )
        seed_integration(
            session,
            integration_id="integration-pch-001",
            task_id="PCH-TASK-001",
            execution_id="execution-pch-001",
            source_commit=source_commit,
            source_tree=source_tree,
            prior_commit=baseline_commit,
            prior_tree=baseline_tree,
            integrated_commit=source_commit,
            integrated_tree=source_tree,
        )
        seed_verified_execution(
            session,
            task_id="PCH-TASK-013",
            execution_id="execution-pch-013",
            source_commit=source_013_commit,
            source_tree=source_013_tree,
            baseline_commit=baseline_commit,
            baseline_tree=baseline_tree,
        )
        seed_integration(
            session,
            integration_id="integration-pch-013",
            task_id="PCH-TASK-013",
            execution_id="execution-pch-013",
            source_commit=source_013_commit,
            source_tree=source_013_tree,
            prior_commit=baseline_commit,
            prior_tree=baseline_tree,
            integrated_commit=source_013_commit,
            integrated_tree=source_013_tree,
        )

        advanced = baseline.advance(
            session,
            plan_id=PLAN_ID,
            plan_version=PLAN_VERSION,
            task_id="PCH-TASK-001",
            task_commit=source_commit,
            task_tree=source_tree,
            resulting_baseline_commit=source_commit,
            resulting_baseline_tree=source_tree,
            expected_generation=0,
            integration_id="integration-pch-001",
        )
        stale = baseline.advance(
            session,
            plan_id=PLAN_ID,
            plan_version=PLAN_VERSION,
            task_id="PCH-TASK-013",
            task_commit=source_013_commit,
            task_tree=source_013_tree,
            resulting_baseline_commit=source_013_commit,
            resulting_baseline_tree=source_013_tree,
            expected_generation=0,
            integration_id="integration-pch-013",
        )

        assert advanced.status == "ADVANCED"
        assert advanced.state is not None
        assert advanced.state.generation == 1
        assert advanced.state.integrated_task_ids == ("PCH-TASK-001",)
        assert advanced.state.task_commits == {"PCH-TASK-001": source_commit}
        assert advanced.state.task_trees == {"PCH-TASK-001": source_tree}
        assert stale.status == "STALE_ADVANCEMENT"
        assert stale.state is not None
        assert stale.state.generation == 1
        assert readiness.evaluate_task(session, "PCH-TASK-002").status == "READY"
        claim = claiming.claim_task(
            session,
            task_id="PCH-TASK-002",
            owner_id="worker",
            lease_duration=timedelta(minutes=5),
        )
        assert claim.status == "CLAIMED"
        assert session.scalar(select(Checkpoint).where(Checkpoint.checkpoint_type == "runtime")) is not None


def test_baseline_advancement_accepts_verified_semantic_integration(tmp_path: Path) -> None:
    repo, baseline_commit, baseline_tree = make_repo(tmp_path)
    source_commit, source_tree = commit_file(repo, "pch-task-001.txt", "task 001\n", "task 001")
    integrated_commit, integrated_tree = commit_file(repo, "integration.txt", "semantic union\n", "semantic integration")
    factory = session_factory()

    with factory() as session:
        seed_pch_graph_for_baseline(session, baseline_commit=baseline_commit, baseline_tree=baseline_tree)
        seed_verified_execution(
            session,
            task_id="PCH-TASK-001",
            execution_id="execution-pch-001",
            source_commit=source_commit,
            source_tree=source_tree,
            baseline_commit=baseline_commit,
            baseline_tree=baseline_tree,
        )
        seed_integration(
            session,
            integration_id="integration-pch-001",
            task_id="PCH-TASK-001",
            execution_id="execution-pch-001",
            source_commit=source_commit,
            source_tree=source_tree,
            prior_commit=baseline_commit,
            prior_tree=baseline_tree,
            integrated_commit=integrated_commit,
            integrated_tree=integrated_tree,
        )

        advanced = advance_task_001(
            HardeningBaselineService(repository_path=repo),
            session,
            source_commit=source_commit,
            source_tree=source_tree,
            integrated_commit=integrated_commit,
            integrated_tree=integrated_tree,
            integration_id="integration-pch-001",
        )

        assert advanced.status == "ADVANCED"
        assert advanced.state is not None
        assert advanced.state.baseline_commit == integrated_commit
        assert advanced.state.baseline_tree == integrated_tree
        assert advanced.state.task_commits == {"PCH-TASK-001": source_commit}


def test_baseline_advancement_is_idempotent_for_exact_verified_integration(tmp_path: Path) -> None:
    repo, baseline_commit, baseline_tree = make_repo(tmp_path)
    source_commit, source_tree = commit_file(repo, "pch-task-001.txt", "task 001\n", "task 001")
    factory = session_factory()
    baseline = HardeningBaselineService(repository_path=repo)

    with factory() as session:
        seed_pch_graph_for_baseline(session, baseline_commit=baseline_commit, baseline_tree=baseline_tree)
        seed_verified_execution(
            session,
            task_id="PCH-TASK-001",
            execution_id="execution-pch-001",
            source_commit=source_commit,
            source_tree=source_tree,
            baseline_commit=baseline_commit,
            baseline_tree=baseline_tree,
        )
        seed_integration(
            session,
            integration_id="integration-pch-001",
            task_id="PCH-TASK-001",
            execution_id="execution-pch-001",
            source_commit=source_commit,
            source_tree=source_tree,
            prior_commit=baseline_commit,
            prior_tree=baseline_tree,
            integrated_commit=source_commit,
            integrated_tree=source_tree,
        )

        first = advance_task_001(
            baseline,
            session,
            source_commit=source_commit,
            source_tree=source_tree,
            integrated_commit=source_commit,
            integrated_tree=source_tree,
            integration_id="integration-pch-001",
        )
        second = advance_task_001(
            baseline,
            session,
            source_commit=source_commit,
            source_tree=source_tree,
            integrated_commit=source_commit,
            integrated_tree=source_tree,
            integration_id="integration-pch-001",
        )

        assert first.status == "ADVANCED"
        assert second.status == "ALREADY_REPRESENTED"
        assert baseline.current_for_task(session, "PCH-TASK-001").generation == 1  # type: ignore[union-attr]


def test_baseline_advancement_rejects_insufficient_execution_authority(tmp_path: Path) -> None:
    repo, baseline_commit, baseline_tree = make_repo(tmp_path)
    source_commit, source_tree = commit_file(repo, "pch-task-001.txt", "task 001\n", "task 001")
    factory = session_factory()
    cases: tuple[tuple[str, dict[str, object], str], ...] = (
        ("failed-execution", {"status": "failed", "terminal_state": "failure"}, "EXECUTION_NOT_VERIFIED"),
        ("running-execution", {"status": "running", "terminal_state": None}, "EXECUTION_NOT_VERIFIED"),
        ("missing-commit", {"source_commit": ""}, "EXECUTION_NOT_VERIFIED"),
        ("missing-tree", {"source_tree": ""}, "EXECUTION_NOT_VERIFIED"),
        ("verification-failed", {"verification_ok": False}, "EXECUTION_NOT_VERIFIED"),
        ("tree-mismatch", {"committed_tree": baseline_tree}, "PROVENANCE_MISMATCH"),
        ("stale-baseline", {"baseline_generation": 99}, "PROVENANCE_MISMATCH"),
    )

    for name, overrides, expected in cases:
        with factory() as session:
            seed_pch_graph_for_baseline(session, baseline_commit=baseline_commit, baseline_tree=baseline_tree)
            execution_source_commit = cast(str, overrides.get("source_commit", source_commit))
            execution_source_tree = cast(str, overrides.get("source_tree", source_tree))
            execution_generation = cast(int, overrides.get("baseline_generation", 0))
            execution_status = cast(str, overrides.get("status", "succeeded"))
            execution_terminal_state = cast(str | None, overrides.get("terminal_state", "success"))
            verification_ok = cast(bool, overrides.get("verification_ok", True))
            committed_tree = cast(str | None, overrides.get("committed_tree"))
            seed_verified_execution(
                session,
                task_id="PCH-TASK-001",
                execution_id=f"execution-{name}",
                source_commit=execution_source_commit,
                source_tree=execution_source_tree,
                baseline_commit=baseline_commit,
                baseline_tree=baseline_tree,
                baseline_generation=execution_generation,
                status=execution_status,
                terminal_state=execution_terminal_state,
                verification_ok=verification_ok,
                committed_tree=committed_tree,
            )
            seed_integration(
                session,
                integration_id=f"integration-{name}",
                task_id="PCH-TASK-001",
                execution_id=f"execution-{name}",
                source_commit=source_commit,
                source_tree=source_tree,
                prior_commit=baseline_commit,
                prior_tree=baseline_tree,
                integrated_commit=source_commit,
                integrated_tree=source_tree,
            )

            result = advance_task_001(
                HardeningBaselineService(repository_path=repo),
                session,
                source_commit=source_commit,
                source_tree=source_tree,
                integrated_commit=source_commit,
                integrated_tree=source_tree,
                integration_id=f"integration-{name}",
            )

            assert result.status == expected
            assert HardeningBaselineService(repository_path=repo).current_for_task(session, "PCH-TASK-001").generation == 0  # type: ignore[union-attr]


def test_baseline_advancement_rejects_git_and_integration_provenance_failures(tmp_path: Path) -> None:
    repo, baseline_commit, baseline_tree = make_repo(tmp_path)
    source_commit, source_tree = commit_file(repo, "pch-task-001.txt", "task 001\n", "task 001")
    blob = git(repo, "hash-object", "-w", "pch-task-001.txt")
    other_repo, _, _ = make_repo(tmp_path / "other")
    wrong_repo_commit, wrong_repo_tree = commit_file(other_repo, "other.txt", "other\n", "other")
    factory = session_factory()
    cases = (
        ("no-integration", None, {}, "INTEGRATION_NOT_FOUND"),
        ("integration-pending", "pending", {}, "INTEGRATION_NOT_VERIFIED"),
        ("wrong-plan", "verified", {"plan_id": "OTHER-PLAN"}, "PROVENANCE_MISMATCH"),
        ("wrong-execution", "verified", {"execution_id": "execution-other"}, "PROVENANCE_MISMATCH"),
        ("source-mismatch", "verified", {"integration_source_commit": baseline_commit}, "PROVENANCE_MISMATCH"),
        ("caller-mismatch", "verified", {"caller_integrated_commit": baseline_commit, "caller_integrated_tree": baseline_tree}, "PROVENANCE_MISMATCH"),
        ("malformed-commit", "verified", {"integrated_commit": "not-a-commit"}, "GIT_OBJECT_INVALID"),
        ("malformed-tree", "verified", {"integrated_tree": "not-a-tree"}, "GIT_OBJECT_INVALID"),
        ("tree-as-commit", "verified", {"integrated_commit": source_tree}, "GIT_OBJECT_INVALID"),
        ("blob-as-tree", "verified", {"integrated_tree": blob}, "GIT_OBJECT_INVALID"),
        ("commit-tree-mismatch", "verified", {"integrated_tree": baseline_tree}, "GIT_OBJECT_INVALID"),
        ("wrong-repo-object", "verified", {"integrated_commit": wrong_repo_commit, "integrated_tree": wrong_repo_tree}, "GIT_OBJECT_INVALID"),
    )

    for name, status, overrides, expected in cases:
        with factory() as session:
            seed_pch_graph_for_baseline(session, baseline_commit=baseline_commit, baseline_tree=baseline_tree)
            seed_verified_execution(
                session,
                task_id="PCH-TASK-001",
                execution_id="execution-pch-001",
                source_commit=source_commit,
                source_tree=source_tree,
                baseline_commit=baseline_commit,
                baseline_tree=baseline_tree,
            )
            if overrides.get("execution_id") == "execution-other":
                seed_verified_execution(
                    session,
                    task_id="PCH-TASK-013",
                    execution_id="execution-other",
                    source_commit=source_commit,
                    source_tree=source_tree,
                    baseline_commit=baseline_commit,
                    baseline_tree=baseline_tree,
                )
            if status is not None:
                seed_integration(
                    session,
                    integration_id=f"integration-{name}",
                    task_id="PCH-TASK-001",
                    execution_id=overrides.get("execution_id", "execution-pch-001"),
                    source_commit=overrides.get("integration_source_commit", source_commit),
                    source_tree=source_tree,
                    prior_commit=baseline_commit,
                    prior_tree=baseline_tree,
                    integrated_commit=overrides.get("integrated_commit", source_commit),
                    integrated_tree=overrides.get("integrated_tree", source_tree),
                    status=status,
                    plan_id=overrides.get("plan_id", PLAN_ID),
                )

            result = advance_task_001(
                HardeningBaselineService(repository_path=repo),
                session,
                source_commit=source_commit,
                source_tree=source_tree,
                integrated_commit=overrides.get("caller_integrated_commit", overrides.get("integrated_commit", source_commit)),
                integrated_tree=overrides.get("caller_integrated_tree", overrides.get("integrated_tree", source_tree)),
                integration_id=f"integration-{name}",
            )

            assert result.status == expected
            assert HardeningBaselineService(repository_path=repo).current_for_task(session, "PCH-TASK-001").generation == 0  # type: ignore[union-attr]


def test_runtime_package_is_bound_to_authoritative_baseline_generation() -> None:
    factory = session_factory()
    package_factory = ExecutionPackageFactory(repository_path=Path("/repo"), timeout_seconds=30)

    with factory() as session:
        plan_import = seed_import(session)
        task = seed_pch_task(session, plan_import, "PCH-TASK-001")

        package = package_factory.build(
            task,
            execution_id="execution-pch-001",
            session=session,
            project_id="PRJ-AI-ENT",
        )

        assert package.baseline_commit == BASELINE_COMMIT
        assert package.baseline_tree == BASELINE_TREE
        assert package.baseline_generation == 0
        assert f"- commit: {BASELINE_COMMIT}" in package.instructions
        assert f"- tree: {BASELINE_TREE}" in package.instructions
        assert "- generation: 0" in package.instructions
