from __future__ import annotations

import argparse
import subprocess
import sys

from ai_ent.bootstrap.executors import get_executor
from ai_ent.bootstrap.manifest import (
    load_bootstrap,
    load_tasks,
    ready_tasks,
    task_status,
)
from ai_ent.bootstrap.paths import ROOT
from ai_ent.bootstrap.state import load_state, mark_blocked, mark_completed
from ai_ent.bootstrap.verifier import verify_task


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
    run_parser.set_defaults(func=run)

    resume_parser = subparsers.add_parser("resume")
    resume_parser.set_defaults(func=status)

    plan_parser = subparsers.add_parser("plan")
    plan_parser.set_defaults(func=status)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
