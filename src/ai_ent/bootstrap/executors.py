from __future__ import annotations

from ai_ent.bootstrap.models import BootstrapTask, ExecutionResult


class Executor:
    def execute(self, task: BootstrapTask) -> ExecutionResult:
        raise NotImplementedError


class FakeExecutor(Executor):
    def execute(self, task: BootstrapTask) -> ExecutionResult:
        if task.simulation_result == "success":
            return ExecutionResult(True, f"{task.id} simulated successfully")
        return ExecutionResult(False, f"{task.id} simulated {task.simulation_result}")


def get_executor(name: str) -> Executor:
    if name == "fake":
        return FakeExecutor()
    if name == "codex":
        raise NotImplementedError("CodexExecutor is intentionally not enabled yet")
    raise ValueError(f"Unknown executor: {name}")

