from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ai_ent.bootstrap.models import BootstrapTask
from ai_ent.bootstrap.paths import BOOTSTRAP, TASK_DIR


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise TypeError(f"{path} must contain a YAML mapping")
    return loaded


def load_bootstrap() -> dict[str, Any]:
    return load_yaml(BOOTSTRAP)


def load_tasks() -> dict[str, BootstrapTask]:
    tasks: dict[str, BootstrapTask] = {}
    for path in sorted(TASK_DIR.glob("TASK-*.yaml")):
        task = BootstrapTask.from_raw(load_yaml(path))
        tasks[task.id] = task
    validate_task_graph(tasks)
    return tasks


def validate_task_graph(tasks: dict[str, BootstrapTask]) -> None:
    for task in tasks.values():
        missing = [dependency for dependency in task.depends_on if dependency not in tasks]
        if missing:
            raise ValueError(f"{task.id} has unknown dependencies: {', '.join(missing)}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            raise ValueError(f"task graph contains a cycle at {task_id}")
        visiting.add(task_id)
        for dependency in tasks[task_id].depends_on:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks:
        visit(task_id)


def task_status(task: BootstrapTask, completed: set[str], blocked: set[str]) -> str:
    if task.id in completed:
        return "done"
    if task.id in blocked:
        return "blocked"
    if any(dependency not in completed for dependency in task.depends_on):
        return "waiting"
    return "ready"


def ready_tasks(tasks: dict[str, BootstrapTask], completed: set[str], blocked: set[str]) -> list[str]:
    return [
        task.id
        for task in tasks.values()
        if task_status(task, completed=completed, blocked=blocked) == "ready"
    ]

