from __future__ import annotations

import os
import shlex
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ai_ent.bootstrap.git import PROHIBITED_PATHS, changed_files
from ai_ent.bootstrap.models import BootstrapTask, ExecutionPackage, ExecutionResult
from ai_ent.bootstrap.paths import ROOT, VENV_PYTHON

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class CodexConfig:
    command: tuple[str, ...] = ()
    default_timeout_seconds: int = 900

    @classmethod
    def from_env(cls) -> CodexConfig:
        raw_command = os.environ.get("AIENT_CODEX_COMMAND", "")
        timeout = int(os.environ.get("AIENT_CODEX_TIMEOUT_SECONDS", "900"))
        command = tuple(shlex.split(raw_command)) if raw_command else ()
        return cls(command=command, default_timeout_seconds=timeout)


def build_execution_package(
    task: BootstrapTask,
    *,
    repository_path: Path = ROOT,
    worktree_path: Path | None = None,
    execution_id: str | None = None,
    timeout_seconds: int = 900,
) -> ExecutionPackage:
    resolved_worktree = worktree_path or repository_path
    instructions = "\n".join(
        part
        for part in (
            f"Task: {task.id}",
            f"Title: {task.title}",
            f"Objective: {task.objective}" if task.objective else "",
            "Allowed paths:",
            *(f"- {path}" for path in task.allowed_paths),
            "Required output:",
            *(f"- {path}" for path in task.outputs),
            "Verification commands:",
            *(f"- {command}" for command in task.verification.commands),
            "Complete only this task's objective and required outputs.",
            "Do not verify, commit, push, or schedule another task.",
            "Do not modify files outside allowed paths, .env, virtualenvs, or Git metadata.",
        )
        if part
    )
    return ExecutionPackage(
        repository_path=repository_path,
        worktree_path=resolved_worktree,
        task_id=task.id,
        execution_id=execution_id or str(uuid.uuid4()),
        instructions=instructions,
        allowed_paths=task.allowed_paths,
        prohibited_paths=PROHIBITED_PATHS,
        python_path=VENV_PYTHON,
        timeout_seconds=timeout_seconds,
    )


class CodexExecutor:
    def __init__(self, config: CodexConfig | None = None, runner: Runner = subprocess.run) -> None:
        self.config = config or CodexConfig.from_env()
        self.runner = runner

    def execute(self, task: BootstrapTask) -> ExecutionResult:
        package = build_execution_package(
            task,
            repository_path=ROOT,
            worktree_path=ROOT,
            timeout_seconds=self.config.default_timeout_seconds,
        )
        return self.execute_package(package)

    def execute_package(self, package: ExecutionPackage) -> ExecutionResult:
        if not self.config.command:
            return ExecutionResult(
                ok=False,
                message="CodexExecutor is NOT_CONFIGURED: set AIENT_CODEX_COMMAND",
                execution_id=package.execution_id,
                terminal_state="not_configured",
            )
        if not package.worktree_path.exists() or not package.worktree_path.is_dir():
            return ExecutionResult(
                ok=False,
                message=f"Invalid worktree: {package.worktree_path}",
                execution_id=package.execution_id,
                terminal_state="failure",
            )

        try:
            completed = self.runner(
                list(self.config.command),
                cwd=package.worktree_path,
                check=False,
                text=True,
                input=package.instructions,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=package.timeout_seconds,
                env=self._env(package),
            )
        except subprocess.TimeoutExpired as exc:
            return ExecutionResult(
                ok=False,
                message=f"CodexExecutor timed out after {package.timeout_seconds}s",
                execution_id=package.execution_id,
                stdout=_text(exc.stdout),
                stderr=_text(exc.stderr),
                terminal_state="timeout",
            )

        return ExecutionResult(
            ok=completed.returncode == 0,
            message="CodexExecutor completed" if completed.returncode == 0 else "CodexExecutor failed",
            changed_files=changed_files(package.worktree_path),
            execution_id=package.execution_id,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
            terminal_state="success" if completed.returncode == 0 else "failure",
        )

    def _env(self, package: ExecutionPackage) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "AIENT_TASK_ID": package.task_id,
                "AIENT_EXECUTION_ID": package.execution_id,
                "AIENT_REPOSITORY_PATH": str(package.repository_path),
                "AIENT_WORKTREE_PATH": str(package.worktree_path),
                "AIENT_PYTHON": str(package.python_path),
                "AIENT_ALLOWED_PATHS": os.pathsep.join(package.allowed_paths),
                "AIENT_PROHIBITED_PATHS": os.pathsep.join(package.prohibited_paths),
            }
        )
        return env


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
