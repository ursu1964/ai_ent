from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from ai_ent.bootstrap.codex import CodexConfig
from ai_ent.bootstrap.executors import get_executor
from ai_ent.bootstrap.manifest import (
    load_bootstrap,
    load_tasks,
    ready_tasks,
    task_status,
)
from ai_ent.bootstrap.paths import ROOT
from ai_ent.bootstrap.proof import run_codex_proof
from ai_ent.bootstrap.state import load_state, mark_blocked, mark_completed
from ai_ent.bootstrap.state_reconciliation import BootstrapStateReconciler
from ai_ent.bootstrap.verifier import verify_task
from ai_ent.persistence.config import DatabaseConfigError, load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.runtime_handoff import DEFAULT_RUNTIME_PROJECT_ID, build_runtime_manifest_tasks
from ai_ent.scheduler.bounded import BoundedRunLimits, BoundedSchedulerRunner
from ai_ent.scheduler.execution import ClaimedExecutionRunner
from ai_ent.scheduler.finalization import ExecutionFinalizer
from ai_ent.scheduler.guarded import GuardedAutonomousRunner, GuardedRunConfig, GuardedRunResult
from ai_ent.scheduler.iteration import (
    ExecutionPackageFactory,
    ExecutionTimeoutPolicy,
    SchedulerIterationService,
)
from ai_ent.scheduler.repair import RepairExecutionService, RepairPlanner, RepairPolicy


def preflight(_: argparse.Namespace) -> int:
    return subprocess.call([sys.executable, str(ROOT / "scripts" / "preflight.py")], cwd=ROOT)


def status(_: argparse.Namespace) -> int:
    bootstrap = load_bootstrap()
    tasks = load_tasks()
    state = load_state()
    completed = set(state.get("completed_tasks", []))
    blocked = set(state.get("blocked_tasks", {}))
    print(f"project: {bootstrap.get('project')}")
    print(f"tasks: {len(tasks)}")
    print(f"completed: {len(completed)}")
    print(f"ready: {', '.join(ready_tasks(tasks, completed, blocked)) or 'none'}")
    for task_id in sorted(tasks):
        task = tasks[task_id]
        marker = task_status(task, completed=completed, blocked=blocked)
        print(f"{task_id}: {marker} - {task.title}")
    return 0


def run(args: argparse.Namespace) -> int:
    tasks = load_tasks()
    task = tasks.get(args.task)
    if task is None:
        print(f"Unknown task: {args.task}", file=sys.stderr)
        return 2

    if not task.schedulable and not args.allow_simulation:
        print(
            f"{task.id} is a non-schedulable {task.execution_class} task; "
            "use test-task for explicit simulation",
            file=sys.stderr,
        )
        return 2

    state = load_state()
    completed = set(state.get("completed_tasks", []))
    if not args.force:
        missing = [dependency for dependency in task.depends_on if dependency not in completed]
        if missing:
            reason = f"{task.id} is waiting for dependencies: {', '.join(missing)}"
            print(reason, file=sys.stderr)
            mark_blocked(task.id, reason)
            return 1

    executor_name = args.executor or task.executor
    try:
        executor = get_executor(executor_name)
    except (NotImplementedError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    result = executor.execute(task)
    print(result.message)
    if not result.ok:
        mark_blocked(task.id, result.message)
        return 1

    verification = verify_task(task)
    for command in verification.commands:
        status_code = "PASS" if command.ok else "FAIL"
        print(f"{status_code} verify: {command.command}")
        if not command.ok and command.output:
            print(command.output)
    if not verification.ok:
        reason = f"{task.id} verification failed"
        mark_blocked(task.id, reason)
        return 1

    mark_completed(task.id, result.message)
    return 0


def init(_: argparse.Namespace) -> int:
    required_dirs = [
        "src/ai_ent",
        "tests",
        "schemas",
        "manifest",
        "policies",
        "config",
        "templates",
        "migrations",
        "scripts",
        "docs",
        "infrastructure",
        "benchmarks",
        "examples",
        ".github",
    ]
    for relative in required_dirs:
        path = ROOT / relative
        path.mkdir(parents=True, exist_ok=True)
        keep = path / ".gitkeep"
        if relative not in {"src/ai_ent", "scripts", "schemas", "manifest"} and not keep.exists():
            keep.write_text("", encoding="utf-8")
    print("repository skeleton ready")
    return 0


def codex_proof(_: argparse.Namespace) -> int:
    result = run_codex_proof()
    print(result.execution.message)
    print(f"terminal_state: {result.execution.terminal_state}")
    if result.execution.exit_code is not None:
        print(f"exit_code: {result.execution.exit_code}")
    if result.worktree_path:
        print(f"worktree: {result.worktree_path}")
    if result.candidate:
        print(f"candidate_tree: {result.candidate.tree_hash}")
        print(f"changed_files: {', '.join(result.candidate.changed_files) or 'none'}")
    if result.verification:
        print(f"verification: {'PASS' if result.verification.ok else 'FAIL'}")
    if result.commit_id:
        print(f"commit: {result.commit_id}")
    if result.committed_tree_hash:
        print(f"committed_tree: {result.committed_tree_hash}")
    if result.execution.ok and result.verification and result.verification.ok:
        mark_completed("TASK-0013", result.execution.message)
        return 0
    return 1


def state_inspect(_: argparse.Namespace) -> int:
    result = BootstrapStateReconciler().inspect()
    print(f"outcome: {result.outcome}")
    if result.differences:
        print(f"differences: {', '.join(result.differences)}")
    return 0 if result.outcome != "CONFLICT" else 1


def state_migrate(args: argparse.Namespace) -> int:
    result = BootstrapStateReconciler().migrate(confirm_cutover=args.confirm_cutover)
    print(f"outcome: {result.outcome}")
    if result.backup_path:
        print(f"backup: {result.backup_path}")
    if result.differences:
        print(f"differences: {', '.join(result.differences)}")
    return 0 if result.outcome == "IN_SYNC" else 1


def state_reconcile(_: argparse.Namespace) -> int:
    result = BootstrapStateReconciler().reconcile()
    print(f"outcome: {result.outcome}")
    if result.differences:
        print(f"differences: {', '.join(result.differences)}")
    return 0 if result.outcome in {"IN_SYNC", "LOCAL_ONLY", "DATABASE_ONLY"} else 1


def guarded_run(args: argparse.Namespace) -> int:
    try:
        database = Database(load_database_settings(Path(".env")))
    except DatabaseConfigError as exc:
        print(f"guarded_run: database configuration blocked: {exc}", file=sys.stderr)
        return 2

    health = database.health()
    if not health.ok:
        print(f"guarded_run: postgresql unavailable: {health.detail}", file=sys.stderr)
        database.dispose()
        return 2

    try:
        with database.session() as session:
            config = GuardedRunConfig(
                project_id=args.project,
                max_tasks_per_run=args.max_tasks,
                max_wall_clock_seconds=args.max_seconds,
                max_failures_per_run=args.max_failures,
                max_repairs_per_task=args.max_repairs,
                dry_run=args.dry_run,
            )
            result = _build_guarded_runner(config, session=session).run(session, config)
            _print_guarded_result(result)
            return 0 if result.stop_reason in {"NO_READY_TASK", "COMPLETED_BOUND", "TASK_LIMIT"} else 1
    finally:
        database.dispose()


def _build_guarded_runner(config: GuardedRunConfig, *, session=None) -> GuardedAutonomousRunner:
    codex_config = CodexConfig.for_scheduler_from_env()
    timeout_policy = ExecutionTimeoutPolicy.for_codex_default(codex_config.default_timeout_seconds)
    if session is not None and config.project_id == DEFAULT_RUNTIME_PROJECT_ID:
        manifest_tasks = build_runtime_manifest_tasks(session, config.project_id)
    else:
        manifest_tasks = load_tasks()
    repair_policy = RepairPolicy(max_autonomous_repair_attempts=config.max_repairs_per_task)
    execution_runner = ClaimedExecutionRunner(config=codex_config)
    finalizer = ExecutionFinalizer(manifest_tasks=manifest_tasks)
    scheduler = SchedulerIterationService(
        package_factory=ExecutionPackageFactory(
            manifest_tasks=manifest_tasks,
            timeout_policy=timeout_policy,
        ),
        owner_id="guarded-run",
        lease_duration=timedelta(seconds=max(timeout_policy.maximum_timeout_seconds * 2, 60)),
    )
    repair_runner = RepairExecutionService(
        planner=RepairPlanner(manifest_tasks=manifest_tasks, policy=repair_policy),
        runner=execution_runner,
        finalizer=finalizer,
    )
    bounded = BoundedSchedulerRunner(
        scheduler=scheduler,
        execution_runner=execution_runner,
        finalizer=finalizer,
        repair_runner=repair_runner,
        limits=BoundedRunLimits(
            max_tasks_per_run=config.max_tasks_per_run,
            max_wall_clock_seconds=config.max_wall_clock_seconds,
            max_failures_per_run=config.max_failures_per_run,
            max_repairs_per_task=config.max_repairs_per_task,
            repair_policy=repair_policy,
        ),
    )
    return GuardedAutonomousRunner(bounded_runner=bounded, codex_config=codex_config)


def _print_guarded_result(result: GuardedRunResult) -> None:
    print(f"run_id: {result.run_id}")
    print(f"project: {result.project_id}")
    print(f"dry_run: {result.dry_run}")
    print(f"stop_reason: {result.stop_reason}")
    print(f"tasks_attempted: {result.tasks_attempted}")
    print(f"tasks_completed: {result.tasks_completed}")
    print(f"tasks_failed: {result.tasks_failed}")
    print(f"repairs_attempted: {result.repairs_attempted}")
    print(f"commits_created: {', '.join(result.commits_created) or 'none'}")
    print(f"remaining_ready_tasks: {', '.join(result.remaining_ready_tasks) or 'none'}")
    print(f"likely_next_task: {result.likely_next_task_id or 'none'}")
    print(f"human_action_required: {result.human_action_required}")
    print(f"reconciliation_required: {result.reconciliation_required}")
    if result.recovered_state is not None:
        print(f"recovery_action: {result.recovered_state.action}")
        print(f"recovery_stage: {result.recovered_state.detected_stage}")
    if result.blockers:
        print(f"blockers: {', '.join(result.blockers)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI-Enterprise bootstrap runner")
    subparsers = parser.add_subparsers(required=True)

    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.set_defaults(func=preflight)

    init_parser = subparsers.add_parser("init")
    init_parser.set_defaults(func=init)

    status_parser = subparsers.add_parser("status")
    status_parser.set_defaults(func=status)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--task", required=True)
    run_parser.add_argument("--executor", choices=["fake"])
    run_parser.add_argument("--force", action="store_true")
    run_parser.add_argument("--allow-simulation", action="store_true")
    run_parser.set_defaults(func=run)

    test_task_parser = subparsers.add_parser("test-task")
    test_task_parser.add_argument("task")
    test_task_parser.set_defaults(
        func=lambda args: run(
            argparse.Namespace(
                task=args.task,
                executor="fake",
                force=True,
                allow_simulation=True,
            )
        )
    )

    resume_parser = subparsers.add_parser("resume")
    resume_parser.set_defaults(func=status)

    plan_parser = subparsers.add_parser("plan")
    plan_parser.set_defaults(func=status)

    codex_proof_parser = subparsers.add_parser("codex-proof")
    codex_proof_parser.set_defaults(func=codex_proof)

    state_parser = subparsers.add_parser("state")
    state_subparsers = state_parser.add_subparsers(required=True)
    inspect_parser = state_subparsers.add_parser("inspect")
    inspect_parser.set_defaults(func=state_inspect)
    migrate_parser = state_subparsers.add_parser("migrate")
    migrate_parser.add_argument("--no-cutover", action="store_false", dest="confirm_cutover")
    migrate_parser.set_defaults(func=state_migrate, confirm_cutover=True)
    reconcile_parser = state_subparsers.add_parser("reconcile")
    reconcile_parser.set_defaults(func=state_reconcile)

    guarded_parser = subparsers.add_parser("guarded-run")
    guarded_parser.add_argument("--project", default="ai-ent")
    guarded_parser.add_argument("--max-tasks", type=int, default=1)
    guarded_parser.add_argument("--max-seconds", type=float, default=900.0)
    guarded_parser.add_argument("--max-failures", type=int, default=1)
    guarded_parser.add_argument("--max-repairs", type=int, default=2)
    guarded_parser.add_argument("--dry-run", action="store_true")
    guarded_parser.set_defaults(func=guarded_run)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
