from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ai_ent.bootstrap.git import PROHIBITED_PATHS, changed_files
from ai_ent.bootstrap.models import BootstrapTask, ExecutionPackage, ExecutionResult
from ai_ent.bootstrap.paths import ROOT, VENV_PYTHON

Runner = Callable[..., subprocess.CompletedProcess[str]]
CODEX_NON_INTERACTIVE_ARGS = ("exec", "--sandbox", "workspace-write", "--ignore-rules", "-")


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

    @classmethod
    def for_scheduler_from_env(cls) -> CodexConfig:
        return cls.from_env().for_scheduler_execution()

    def for_scheduler_execution(self) -> CodexConfig:
        return replace(self, command=_scheduler_command(self.command))

    def scheduler_configuration_error(self) -> str | None:
        if not self.command:
            return "codex_not_configured:set AIENT_CODEX_COMMAND"
        executable = self.command[0]
        if _executable_missing(executable):
            return f"codex_executable_not_found:{executable}"
        if _executable_not_executable(executable):
            return f"codex_executable_not_executable:{executable}"
        if not _is_codex_executable(executable):
            return None
        if len(self.command) == 1:
            return "codex_interactive_command:use codex exec"
        if self.command[1] != "exec":
            return f"codex_unsupported_scheduler_mode:{self.command[1]}"
        sandbox = _sandbox_value(self.command)
        if sandbox != "workspace-write":
            return "codex_invalid_sandbox:use workspace-write"
        if "--ignore-rules" not in self.command:
            return "codex_missing_ignore_rules"
        if "-" not in self.command[2:]:
            return "codex_missing_stdin_prompt:-"
        return None


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
            "Prohibited paths:",
            *(f"- {path}" for path in (task.prohibited_paths or PROHIBITED_PATHS)),
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
        prohibited_paths=task.prohibited_paths or PROHIBITED_PATHS,
        python_path=VENV_PYTHON,
        timeout_seconds=timeout_seconds,
    )


class CodexExecutor:
    def __init__(self, config: CodexConfig | None = None, runner: Runner = subprocess.run) -> None:
        self.config = config or CodexConfig.for_scheduler_from_env()
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
        configuration_error = self.config.scheduler_configuration_error()
        if configuration_error is not None:
            return ExecutionResult(
                ok=False,
                message=f"CodexExecutor configuration invalid: {configuration_error}",
                execution_id=package.execution_id,
                terminal_state="not_configured" if not self.config.command else "failure",
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
        except OSError as exc:
            return ExecutionResult(
                ok=False,
                message=f"CodexExecutor launch failed: {_text(str(exc))}",
                execution_id=package.execution_id,
                stderr=_text(str(exc)),
                terminal_state="failure",
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


def _scheduler_command(command: tuple[str, ...]) -> tuple[str, ...]:
    if not command:
        return ()
    executable = command[0]
    if not _is_codex_executable(executable):
        return command
    if len(command) == 1:
        return (executable, *CODEX_NON_INTERACTIVE_ARGS)
    if command[1] != "exec":
        return command
    args = list(command[2:])
    if _sandbox_value(command) is None:
        args[0:0] = ["--sandbox", "workspace-write"]
    if "--ignore-rules" not in args:
        _insert_before_stdin_marker(args, "--ignore-rules")
    if "-" not in args:
        args.append("-")
    return (executable, "exec", *args)


def _is_codex_executable(executable: str) -> bool:
    return Path(executable).name in {"codex", "codex.exe"}


def _sandbox_value(command: tuple[str, ...]) -> str | None:
    args = command[2:] if len(command) > 1 and command[1] == "exec" else command[1:]
    for index, value in enumerate(args):
        if value == "--sandbox" and index + 1 < len(args):
            return args[index + 1]
        if value.startswith("--sandbox="):
            return value.split("=", 1)[1]
        if value == "-s" and index + 1 < len(args):
            return args[index + 1]
    return None


def _insert_before_stdin_marker(args: list[str], value: str) -> None:
    try:
        index = args.index("-")
    except ValueError:
        args.append(value)
        return
    args.insert(index, value)


def _executable_missing(executable: str) -> bool:
    path = Path(executable)
    if path.parent != Path("."):
        return not path.exists()
    return shutil.which(executable) is None


def _executable_not_executable(executable: str) -> bool:
    path = Path(executable)
    if path.parent != Path("."):
        return path.exists() and not os.access(path, os.X_OK)
    return False


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
