from __future__ import annotations

from typing import Protocol, runtime_checkable

from ai_ent.bootstrap.codex import CodexExecutor
from ai_ent.bootstrap.models import BootstrapTask, ExecutionResult


@runtime_checkable
class Executor(Protocol):
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
        return CodexExecutor()
    raise ValueError(f"Unknown executor: {name}")
