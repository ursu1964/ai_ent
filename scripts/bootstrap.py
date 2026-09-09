from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "manifest" / "bootstrap" / "bootstrap.yaml"
TASK_DIR = ROOT / "manifest" / "bootstrap" / "tasks"
STATE_FILE = ROOT / ".bootstrap" / "state.json"


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    message: str


class FakeExecutor:
    def execute(self, task: dict[str, Any]) -> ExecutionResult:
        simulation = task.get("simulation") or {}
        result = simulation.get("result", "success")
        if result == "success":
            return ExecutionResult(True, f"{task['id']} simulated successfully")
        return ExecutionResult(False, f"{task['id']} simulated {result}")


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise TypeError(f"{path} must contain a YAML mapping")
    return loaded


def load_tasks() -> dict[str, dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    for path in sorted(TASK_DIR.glob("TASK-*.yaml")):
        task = load_yaml(path)
        task_id = task.get("id")
        if not isinstance(task_id, str):
            raise TypeError(f"{path} is missing a string id")
        tasks[task_id] = task
    return tasks


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"completed_tasks": []}
    with STATE_FILE.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")


def preflight(_: argparse.Namespace) -> int:
    return subprocess.call([sys.executable, str(ROOT / "scripts" / "preflight.py")], cwd=ROOT)


def status(_: argparse.Namespace) -> int:
    bootstrap = load_yaml(BOOTSTRAP)
    tasks = load_tasks()
    state = load_state()
    completed = set(state.get("completed_tasks", []))
    print(f"project: {bootstrap.get('project')}")
    print(f"tasks: {len(tasks)}")
    print(f"completed: {len(completed)}")
    for task_id in sorted(tasks):
        marker = "done" if task_id in completed else "ready"
        print(f"{task_id}: {marker} - {tasks[task_id].get('title')}")
    return 0


def run(args: argparse.Namespace) -> int:
    tasks = load_tasks()
    task = tasks.get(args.task)
    if task is None:
        print(f"Unknown task: {args.task}", file=sys.stderr)
        return 2

    executor_name = args.executor or task.get("executor", "fake")
    if executor_name != "fake":
        print(f"Executor not available yet: {executor_name}", file=sys.stderr)
        return 2

    result = FakeExecutor().execute(task)
    print(result.message)
    if not result.ok:
        return 1

    state = load_state()
    completed = list(dict.fromkeys([*state.get("completed_tasks", []), task["id"]]))
    state["completed_tasks"] = completed
    state["last_completed_task"] = task["id"]
    save_state(state)
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
