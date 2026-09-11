from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from ai_ent.bootstrap.codex import (
    CODEX_NON_INTERACTIVE_ARGS,
    CodexConfig,
    CodexExecutor,
    build_execution_package,
)
from ai_ent.bootstrap.executors import Executor, get_executor
from ai_ent.bootstrap.models import BootstrapTask


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert completed.returncode == 0, completed.stdout
    return completed.stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.local")
    git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-m", "seed")
    return tmp_path


def make_task() -> BootstrapTask:
    return BootstrapTask.from_raw(
        {
            "id": "TASK-0012",
            "stage": "B03",
            "title": "Prepare Codex executor adapter contract",
            "objective": "Create a disabled configurable Codex adapter.",
            "executor": "codex",
            "depends_on": [],
            "allowed_paths": ["src/**", "tests/**"],
            "outputs": ["tests/fixtures/adapter_contract.txt"],
            "verification": {
                "commands": ["test -f tests/fixtures/adapter_contract.txt"],
            },
        }
    )


def test_codex_adapter_satisfies_executor_interface() -> None:
    executor = get_executor("codex")
    assert isinstance(executor, Executor)


def test_execution_package_maps_task_to_invocation_contract(tmp_path: Path) -> None:
    task = make_task()
    package = build_execution_package(
        task,
        repository_path=tmp_path,
        worktree_path=tmp_path / "worktree",
        execution_id="exec-1",
        timeout_seconds=30,
    )

    assert package.task_id == "TASK-0012"
    assert package.execution_id == "exec-1"
    assert package.repository_path == tmp_path
    assert package.worktree_path == tmp_path / "worktree"
    assert package.allowed_paths == ("src/**", "tests/**")
    assert ".env" in package.prohibited_paths
    assert package.timeout_seconds == 30
    assert "Do not verify, commit, push, or schedule another task." in package.instructions
    assert "tests/fixtures/adapter_contract.txt" in package.instructions
    assert "test -f tests/fixtures/adapter_contract.txt" in package.instructions


def test_missing_codex_configuration_is_deterministic() -> None:
    task = make_task()
    result = CodexExecutor(config=CodexConfig(command=())).execute(task)

    assert not result.ok
    assert result.terminal_state == "not_configured"
    assert "codex_not_configured:set AIENT_CODEX_COMMAND" in result.message


def test_scheduler_env_bare_codex_resolves_to_non_interactive_argv(monkeypatch) -> None:
    monkeypatch.setenv("AIENT_CODEX_COMMAND", "codex")

    config = CodexConfig.for_scheduler_from_env()

    assert config.command == ("codex", *CODEX_NON_INTERACTIVE_ARGS)


def test_scheduler_env_preserves_executable_path_with_spaces(tmp_path: Path, monkeypatch) -> None:
    tool_dir = tmp_path / "tool dir"
    tool_dir.mkdir()
    codex = tool_dir / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex.chmod(0o755)
    monkeypatch.setenv("AIENT_CODEX_COMMAND", f"'{codex}'")

    config = CodexConfig.for_scheduler_from_env()

    assert config.command == (str(codex), *CODEX_NON_INTERACTIVE_ARGS)


def test_scheduler_codex_exec_command_adds_required_noninteractive_arguments(monkeypatch) -> None:
    monkeypatch.setenv("AIENT_CODEX_COMMAND", "codex exec")

    config = CodexConfig.for_scheduler_from_env()

    assert config.command == ("codex", "exec", "--sandbox", "workspace-write", "--ignore-rules", "-")


def test_scheduler_codex_exec_keeps_options_before_stdin_marker(monkeypatch) -> None:
    monkeypatch.setenv("AIENT_CODEX_COMMAND", "codex exec --sandbox workspace-write -")

    config = CodexConfig.for_scheduler_from_env()

    assert config.command == ("codex", "exec", "--sandbox", "workspace-write", "--ignore-rules", "-")


def test_invalid_codex_scheduler_modes_are_rejected(tmp_path: Path) -> None:
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex.chmod(0o755)
    config = CodexConfig(command=(str(codex), "resume"))

    assert config.scheduler_configuration_error() == f"codex_unsupported_scheduler_mode:{config.command[1]}"


def test_codex_executor_rejects_bare_codex_without_subprocess(tmp_path: Path) -> None:
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex.chmod(0o755)
    task = make_task()
    package = build_execution_package(task, repository_path=tmp_path, worktree_path=tmp_path)

    def forbidden_runner(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("interactive codex command should not launch")

    result = CodexExecutor(config=CodexConfig(command=(str(codex),)), runner=forbidden_runner).execute_package(package)

    assert not result.ok
    assert result.terminal_state == "failure"
    assert "codex_interactive_command:use codex exec" in result.message


def test_codex_executor_passes_argv_stdin_cwd_and_timeout(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex.chmod(0o755)
    task = make_task()
    package = build_execution_package(task, repository_path=repo, worktree_path=repo, timeout_seconds=42)
    captured: dict[str, Any] = {}
    command = (str(codex), "exec", "--sandbox", "workspace-write", "--ignore-rules", "-")

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    result = CodexExecutor(config=CodexConfig(command=command), runner=runner).execute_package(package)

    assert result.ok
    assert captured["args"] == list(command)
    assert captured["cwd"] == repo
    assert captured["input"] == package.instructions
    assert captured["timeout"] == 42
    assert captured["stdout"] == subprocess.PIPE
    assert captured["stderr"] == subprocess.PIPE


def test_process_launch_failure_is_reported(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = make_task()
    package = build_execution_package(task, repository_path=repo, worktree_path=repo)
    command = (sys.executable, "-c", "raise SystemExit(0)")

    def runner(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("permission denied")

    result = CodexExecutor(config=CodexConfig(command=command), runner=runner).execute_package(package)

    assert not result.ok
    assert result.terminal_state == "failure"
    assert "permission denied" in result.stderr


def test_invalid_worktree_rejected(tmp_path: Path) -> None:
    task = make_task()
    package = build_execution_package(task, repository_path=tmp_path, worktree_path=tmp_path / "missing")
    executor = CodexExecutor(config=CodexConfig(command=(sys.executable, "-c", "print('no')")))

    result = executor.execute_package(package)

    assert not result.ok
    assert result.terminal_state == "failure"
    assert "Invalid worktree" in result.message


def test_timeout_represented_correctly(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = make_task()
    package = build_execution_package(task, repository_path=repo, worktree_path=repo, timeout_seconds=1)
    executor = CodexExecutor(
        config=CodexConfig(command=(sys.executable, "-c", "import time; time.sleep(5)"))
    )

    result = executor.execute_package(package)

    assert not result.ok
    assert result.terminal_state == "timeout"
    assert result.exit_code is None


def test_non_zero_exit_and_streams_are_captured(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = make_task()
    package = build_execution_package(task, repository_path=repo, worktree_path=repo)
    command = (
        sys.executable,
        "-c",
        "import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(7)",
    )
    executor = CodexExecutor(config=CodexConfig(command=command))

    result = executor.execute_package(package)

    assert not result.ok
    assert result.terminal_state == "failure"
    assert result.exit_code == 7
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


def test_success_reports_changed_files_without_commit(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = make_task()
    package = build_execution_package(task, repository_path=repo, worktree_path=repo)
    command = (sys.executable, "-c", "from pathlib import Path; Path('src').mkdir(); Path('src/x.py').write_text('x=1\\n')")
    executor = CodexExecutor(config=CodexConfig(command=command))

    before_head = git(repo, "rev-parse", "HEAD")
    result = executor.execute_package(package)
    after_head = git(repo, "rev-parse", "HEAD")

    assert result.ok
    assert result.changed_files == ("src/x.py",)
    assert before_head == after_head


def test_executor_does_not_invoke_verifier(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = make_task()
    package = build_execution_package(task, repository_path=repo, worktree_path=repo)

    executor = CodexExecutor(config=CodexConfig(command=(sys.executable, "-c", "print('ok')")))
    with patch("ai_ent.bootstrap.verifier.verify_task", side_effect=AssertionError):
        result = executor.execute_package(package)

    assert result.ok
