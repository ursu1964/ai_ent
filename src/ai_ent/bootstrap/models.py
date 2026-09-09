from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ExecutorName = Literal["fake", "codex"]
SimulationResult = Literal["success", "failure", "timeout", "invalid_output", "scope_violation"]
TerminalState = Literal["success", "failure", "timeout", "cancelled", "not_configured"]
ExecutionClass = Literal["implementation", "simulation"]


@dataclass(frozen=True)
class VerificationSpec:
    commands: tuple[str, ...] = ()

    @classmethod
    def from_raw(cls, raw: object) -> VerificationSpec:
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise TypeError("verification must be a mapping")
        commands = raw.get("commands", [])
        if not isinstance(commands, list) or not all(isinstance(item, str) for item in commands):
            raise TypeError("verification.commands must be a list of strings")
        return cls(commands=tuple(commands))


@dataclass(frozen=True)
class BootstrapTask:
    id: str
    stage: str
    title: str
    executor: ExecutorName
    depends_on: tuple[str, ...] = ()
    objective: str | None = None
    allowed_paths: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    execution_class: ExecutionClass = "implementation"
    schedulable: bool = True
    simulation_result: SimulationResult = "success"
    verification: VerificationSpec = field(default_factory=VerificationSpec)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> BootstrapTask:
        depends_on = raw.get("depends_on", [])
        allowed_paths = raw.get("allowed_paths", [])
        outputs = raw.get("outputs", [])
        simulation = raw.get("simulation") or {}
        objective = raw.get("objective")
        execution_class = raw.get("execution_class", "implementation")
        schedulable = raw.get("schedulable", True)

        if not isinstance(raw.get("id"), str):
            raise TypeError("task id must be a string")
        if not isinstance(raw.get("stage"), str):
            raise TypeError("task stage must be a string")
        if not isinstance(raw.get("title"), str):
            raise TypeError("task title must be a string")
        if raw.get("executor") not in {"fake", "codex"}:
            raise ValueError("task executor must be fake or codex")
        if not isinstance(depends_on, list) or not all(isinstance(item, str) for item in depends_on):
            raise TypeError("depends_on must be a list of strings")
        if not isinstance(allowed_paths, list) or not all(
            isinstance(item, str) for item in allowed_paths
        ):
            raise TypeError("allowed_paths must be a list of strings")
        if not isinstance(outputs, list) or not all(isinstance(item, str) for item in outputs):
            raise TypeError("outputs must be a list of strings")
        if objective is not None and not isinstance(objective, str):
            raise TypeError("objective must be a string")
        if execution_class not in {"implementation", "simulation"}:
            raise ValueError("execution_class must be implementation or simulation")
        if not isinstance(schedulable, bool):
            raise TypeError("schedulable must be a boolean")
        if not isinstance(simulation, dict):
            raise TypeError("simulation must be a mapping")

        simulation_result = simulation.get("result", "success")
        if simulation_result not in {
            "success",
            "failure",
            "timeout",
            "invalid_output",
            "scope_violation",
        }:
            raise ValueError("simulation.result is not supported")

        return cls(
            id=raw["id"],
            stage=raw["stage"],
            title=raw["title"],
            executor=raw["executor"],
            depends_on=tuple(depends_on),
            objective=objective,
            allowed_paths=tuple(allowed_paths),
            outputs=tuple(outputs),
            execution_class=execution_class,
            schedulable=schedulable,
            simulation_result=simulation_result,
            verification=VerificationSpec.from_raw(raw.get("verification")),
        )


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    message: str
    changed_files: tuple[str, ...] = ()
    execution_id: str | None = None
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    terminal_state: TerminalState = "success"


@dataclass(frozen=True)
class ExecutionPackage:
    repository_path: Path
    worktree_path: Path
    task_id: str
    execution_id: str
    instructions: str
    allowed_paths: tuple[str, ...]
    prohibited_paths: tuple[str, ...]
    python_path: Path
    timeout_seconds: int


@dataclass(frozen=True)
class CommandResult:
    command: str
    returncode: int
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    commands: tuple[CommandResult, ...]


@dataclass(frozen=True)
class Candidate:
    task_id: str
    tree_hash: str
    changed_files: tuple[str, ...]


@dataclass(frozen=True)
class ScopeResult:
    ok: bool
    findings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommitEligibility:
    ok: bool
    reason: str
