from __future__ import annotations

import subprocess
from pathlib import Path

from ai_ent.bootstrap.git import (
    capture_candidate,
    commit_eligibility,
    commit_verified_candidate,
    validate_scope,
)
from ai_ent.bootstrap.models import BootstrapTask, CommandResult, VerificationResult


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


def make_task(allowed_paths: list[str] | None = None) -> BootstrapTask:
    return BootstrapTask.from_raw(
        {
            "id": "TASK-9999",
            "stage": "B99",
            "title": "Synthetic task",
            "executor": "fake",
            "depends_on": [],
            "allowed_paths": allowed_paths or ["src/**"],
        }
    )


def verification(ok: bool) -> VerificationResult:
    return VerificationResult(
        ok=ok,
        commands=(CommandResult(command="synthetic", returncode=0 if ok else 1, output=""),),
    )


def assert_runtime_error(message: str, callback: object) -> None:
    try:
        assert callable(callback)
        callback()
    except RuntimeError as exc:
        assert message in str(exc)
    else:
        raise AssertionError("RuntimeError was not raised")


def test_verified_candidate_commit_succeeds(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = make_task()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")

    candidate = capture_candidate(task, cwd=repo)
    commit_id = commit_verified_candidate(task, candidate, verification(True), "Synthetic commit", repo)

    assert commit_id
    assert git(repo, "log", "-1", "--pretty=%B").startswith("Synthetic commit")


def test_unverified_candidate_commit_denied(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = make_task()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")

    candidate = capture_candidate(task, cwd=repo)
    assert_runtime_error(
        "verification failed",
        lambda: commit_verified_candidate(task, candidate, verification(False), "Synthetic commit", repo),
    )


def test_failed_verification_denies_eligibility(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = make_task()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")

    candidate = capture_candidate(task, cwd=repo)
    result = commit_eligibility(task, candidate, verification(False), repo)

    assert not result.ok
    assert result.reason == "verification failed"


def test_candidate_modified_after_pass_is_denied(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = make_task()
    (repo / "src").mkdir()
    target = repo / "src" / "app.py"
    target.write_text("print('ok')\n", encoding="utf-8")

    candidate = capture_candidate(task, cwd=repo)
    target.write_text("print('changed')\n", encoding="utf-8")
    result = commit_eligibility(task, candidate, verification(True), repo)

    assert not result.ok
    assert result.reason == "verification is stale: candidate tree changed"


def test_unrelated_file_present_is_denied(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = make_task()
    (repo / "outside.txt").write_text("not allowed\n", encoding="utf-8")

    candidate = capture_candidate(task, cwd=repo)
    scope = validate_scope(task, candidate)

    assert not scope.ok
    assert scope.findings == ("path outside task scope: outside.txt",)


def test_prohibited_file_present_is_denied(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = make_task(allowed_paths=["**"])
    (repo / ".env").write_text("SECRET=value\n", encoding="utf-8")

    candidate = capture_candidate(task, cwd=repo)
    scope = validate_scope(task, candidate)

    assert not scope.ok
    assert scope.findings == ("prohibited path changed: .env",)


def test_no_op_candidate_is_deterministic(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    task = make_task()

    candidate = capture_candidate(task, cwd=repo)
    result = commit_eligibility(task, candidate, verification(True), repo)
    commit_id = commit_verified_candidate(task, candidate, verification(True), "No-op", repo)

    assert result.ok
    assert result.reason == "no-op candidate"
    assert commit_id is None


def test_git_command_failure_has_no_false_success(tmp_path: Path) -> None:
    task = make_task()
    assert_runtime_error("", lambda: capture_candidate(task, cwd=tmp_path))
