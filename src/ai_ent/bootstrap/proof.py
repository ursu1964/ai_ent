from __future__ import annotations

import os
from dataclasses import dataclass

from ai_ent.bootstrap.codex import CodexConfig, CodexExecutor, build_execution_package
from ai_ent.bootstrap.git import capture_candidate, commit_verified_candidate, require_git
from ai_ent.bootstrap.manifest import load_tasks
from ai_ent.bootstrap.models import Candidate, ExecutionResult, VerificationResult
from ai_ent.bootstrap.paths import ROOT
from ai_ent.bootstrap.verifier import verify_task
from ai_ent.bootstrap.worktree import create_task_worktree, remove_task_worktree


@dataclass(frozen=True)
class ProofResult:
    execution: ExecutionResult
    candidate: Candidate | None
    verification: VerificationResult | None
    commit_id: str | None
    committed_tree_hash: str | None
    worktree_path: str | None


def run_codex_proof() -> ProofResult:
    tasks = load_tasks()
    task = tasks["TASK-0013"]
    config = CodexConfig.for_scheduler_from_env()
    if not config.command:
        return ProofResult(
            execution=ExecutionResult(
                ok=False,
                message="CODEX_NOT_CONFIGURED: set AIENT_CODEX_COMMAND",
                terminal_state="not_configured",
            ),
            candidate=None,
            verification=None,
            commit_id=None,
            committed_tree_hash=None,
            worktree_path=None,
        )

    worktree = create_task_worktree(task.id)
    try:
        package = build_execution_package(
            task,
            repository_path=ROOT,
            worktree_path=worktree,
            timeout_seconds=config.default_timeout_seconds,
        )
        executor = CodexExecutor(config=config)
        execution = executor.execute_package(package)
        if not execution.ok:
            return ProofResult(execution, None, None, None, None, str(worktree))

        candidate = capture_candidate(task, cwd=worktree)
        verification = verify_task(task, cwd=worktree)
        if not verification.ok:
            return ProofResult(execution, candidate, verification, None, None, str(worktree))

        commit_id = commit_verified_candidate(
            task,
            candidate,
            verification,
            "Add Codex isolated execution proof fixture",
            cwd=worktree,
        )
        committed_tree_hash = require_git(["rev-parse", "HEAD^{tree}"], cwd=worktree)
        return ProofResult(execution, candidate, verification, commit_id, committed_tree_hash, str(worktree))
    finally:
        if os.environ.get("AIENT_KEEP_PROOF_WORKTREE") != "1":
            remove_task_worktree(task.id)
