from __future__ import annotations

from ai_ent.bootstrap.manifest import load_tasks, ready_tasks, task_status


def test_non_schedulable_simulation_task_is_not_ready() -> None:
    tasks = load_tasks()
    task = tasks["TASK-0004"]

    assert task.execution_class == "simulation"
    assert not task.schedulable
    assert task_status(task, completed={"TASK-0003"}, blocked=set()) == "simulation"
    assert "TASK-0004" not in ready_tasks(tasks, completed={"TASK-0003"}, blocked=set())


def test_task_0013_enters_normal_dag_after_task_0012() -> None:
    tasks = load_tasks()
    completed = {
        "TASK-0001",
        "TASK-0002",
        "TASK-0003",
        "TASK-0005",
        "TASK-0006",
        "TASK-0007",
        "TASK-0008",
        "TASK-0009",
        "TASK-0010",
        "TASK-0011",
        "TASK-0012",
    }

    ready = ready_tasks(tasks, completed=completed, blocked=set())

    assert "TASK-0013" in ready
    assert "TASK-0004" not in ready
