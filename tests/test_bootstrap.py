from __future__ import annotations

from scripts.bootstrap import load_tasks


def test_load_tasks_has_initial_task() -> None:
    tasks = load_tasks()
    assert "TASK-0001" in tasks
