from __future__ import annotations

import fnmatch
import os
import subprocess
import tempfile
from pathlib import Path

from ai_ent.bootstrap.models import (
    BootstrapTask,
    Candidate,
    CommitEligibility,
    ScopeResult,
    VerificationResult,
)
from ai_ent.bootstrap.paths import ROOT

PROHIBITED_PATHS = (
    ".env",
    ".env.*",
    ".bootstrap/**",
    "aient/**",
)


def run_git(
    args: list[str],
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def require_git(args: list[str], cwd: Path = ROOT, env: dict[str, str] | None = None) -> str:
    completed = run_git(args, cwd=cwd, env=env)
    if completed.returncode != 0:
        raise RuntimeError(completed.stdout.strip() or f"git {' '.join(args)} failed")
    return completed.stdout.strip()


def changed_files(cwd: Path = ROOT) -> tuple[str, ...]:
    output = require_git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=cwd)
    files: list[str] = []
    for line in output.splitlines():
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        path = parts[1]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path)
    return tuple(sorted(files))


def candidate_tree_hash(cwd: Path = ROOT) -> str:
    with tempfile.TemporaryDirectory(prefix="ai-ent-index-") as temp_dir:
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(Path(temp_dir) / "index")
        require_git(["read-tree", "HEAD"], cwd=cwd, env=env)
        require_git(["add", "-A"], cwd=cwd, env=env)
        return require_git(["write-tree"], cwd=cwd, env=env)


def capture_candidate(task: BootstrapTask, cwd: Path = ROOT) -> Candidate:
    return Candidate(
        task_id=task.id,
        tree_hash=candidate_tree_hash(cwd),
        changed_files=changed_files(cwd),
    )


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def validate_scope(task: BootstrapTask, candidate: Candidate) -> ScopeResult:
    findings: list[str] = []
    if not candidate.changed_files:
        return ScopeResult(True)

    for path in candidate.changed_files:
        if _matches(path, task.prohibited_paths or PROHIBITED_PATHS):
            findings.append(f"prohibited path changed: {path}")
        if task.allowed_paths and not _matches(path, task.allowed_paths):
            findings.append(f"path outside task scope: {path}")

    return ScopeResult(ok=not findings, findings=tuple(findings))


def commit_eligibility(
    task: BootstrapTask,
    candidate: Candidate,
    verification: VerificationResult,
    cwd: Path = ROOT,
) -> CommitEligibility:
    if candidate.task_id != task.id:
        return CommitEligibility(False, "candidate task does not match task")
    if not candidate.changed_files:
        return CommitEligibility(True, "no-op candidate")
    scope = validate_scope(task, candidate)
    if not scope.ok:
        return CommitEligibility(False, "; ".join(scope.findings))
    if not verification.ok:
        return CommitEligibility(False, "verification failed")
    current_hash = candidate_tree_hash(cwd)
    if current_hash != candidate.tree_hash:
        return CommitEligibility(False, "verification is stale: candidate tree changed")
    return CommitEligibility(True, "commit allowed")


def commit_verified_candidate(
    task: BootstrapTask,
    candidate: Candidate,
    verification: VerificationResult,
    message: str,
    cwd: Path = ROOT,
) -> str | None:
    eligibility = commit_eligibility(task, candidate, verification, cwd=cwd)
    if not eligibility.ok:
        raise RuntimeError(eligibility.reason)
    if not candidate.changed_files:
        return None

    require_git(["add", *candidate.changed_files], cwd=cwd)
    current_hash = require_git(["write-tree"], cwd=cwd)
    if current_hash != candidate.tree_hash:
        raise RuntimeError("candidate changed during staging")

    commit_message = (
        f"{message}\n\n"
        f"Task: {task.id}\n"
        f"Candidate-Tree: {candidate.tree_hash}\n"
        "Verification: PASS"
    )
    require_git(["commit", "-m", commit_message], cwd=cwd)
    return require_git(["rev-parse", "--short", "HEAD"], cwd=cwd)
