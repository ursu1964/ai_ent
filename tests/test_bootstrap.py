from __future__ import annotations

from ai_ent.bootstrap.executors import FakeExecutor
from ai_ent.bootstrap.manifest import load_tasks, ready_tasks
from ai_ent.bootstrap.models import BootstrapTask
from ai_ent.bootstrap.verifier import run_command


def test_load_tasks_has_initial_task() -> None:
    tasks = load_tasks()
    assert "TASK-0001" in tasks


def test_task_graph_exposes_ready_tasks() -> None:
    tasks = load_tasks()
    ready = ready_tasks(tasks, completed=set(), blocked=set())
    assert "TASK-0001" in ready
    assert "TASK-0002" not in ready
    assert "TASK-0004" not in ready


def test_simulation_task_is_not_normal_ready_work() -> None:
    tasks = load_tasks()
    completed = {"TASK-0003"}
    ready = ready_tasks(tasks, completed=completed, blocked=set())
    assert tasks["TASK-0004"].execution_class == "simulation"
    assert not tasks["TASK-0004"].schedulable
    assert "TASK-0004" not in ready


def test_fake_executor_failure_is_not_success() -> None:
    task = BootstrapTask.from_raw(
        {
            "id": "TASK-9999",
            "stage": "B99",
            "title": "Synthetic failure",
            "executor": "fake",
            "depends_on": [],
            "simulation": {"result": "failure"},
        }
    )
    result = FakeExecutor().execute(task)
    assert not result.ok


def test_independent_verifier_runs_command() -> None:
    result = run_command("python -c 'print(42)'")
    assert result.ok
    assert result.output == "42"
