from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from ai_ent.bootstrap.codex import CodexConfig, CodexExecutor, build_execution_package
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
    assert "AIENT_CODEX_PROOF=TASK-0013" in package.instructions


def test_missing_codex_configuration_is_deterministic() -> None:
    task = make_task()
    result = CodexExecutor(config=CodexConfig(command=())).execute(task)

    assert not result.ok
    assert result.terminal_state == "not_configured"
    assert "NOT_CONFIGURED" in result.message


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
