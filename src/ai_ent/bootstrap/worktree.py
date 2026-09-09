from __future__ import annotations

from pathlib import Path

from ai_ent.bootstrap.git import require_git
from ai_ent.bootstrap.paths import ROOT

WORKTREE_ROOT = ROOT.parent / ".ai_ent-worktrees"


def task_worktree_path(task_id: str) -> Path:
    return WORKTREE_ROOT / task_id


def worktree_supported() -> bool:
    return WORKTREE_ROOT.exists()


def create_task_worktree(task_id: str, base_ref: str = "HEAD") -> Path:
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    path = task_worktree_path(task_id)
    if path.exists():
        raise FileExistsError(f"worktree already exists: {path}")
    branch = f"task/{task_id}"
    require_git(["worktree", "add", "-b", branch, str(path), base_ref], cwd=ROOT)
    return path


def remove_task_worktree(task_id: str) -> None:
    path = task_worktree_path(task_id)
    if path.exists():
        require_git(["worktree", "remove", "--force", str(path)], cwd=ROOT)
