from __future__ import annotations

from ai_ent.bootstrap.models import ExecutionResult
from ai_ent.bootstrap.proof import ProofResult


def test_proof_result_can_represent_not_configured() -> None:
    result = ProofResult(
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

    assert not result.execution.ok
    assert result.execution.terminal_state == "not_configured"
