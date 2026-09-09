from __future__ import annotations

from pathlib import Path

from ai_ent.bootstrap.paths import ROOT

WORKTREE_ROOT = ROOT.parent / ".ai_ent-worktrees"


def task_worktree_path(task_id: str) -> Path:
    return WORKTREE_ROOT / task_id


def worktree_supported() -> bool:
    return WORKTREE_ROOT.exists()

