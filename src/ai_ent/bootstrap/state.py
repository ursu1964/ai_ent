from __future__ import annotations

import json
from typing import Any

from ai_ent.bootstrap.paths import STATE_FILE


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"completed_tasks": [], "blocked_tasks": {}}
    with STATE_FILE.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    state.setdefault("completed_tasks", [])
    state.setdefault("blocked_tasks", {})
    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")


def mark_completed(task_id: str, message: str) -> None:
    state = load_state()
    completed = list(dict.fromkeys([*state.get("completed_tasks", []), task_id]))
    blocked = dict(state.get("blocked_tasks", {}))
    blocked.pop(task_id, None)
    state["completed_tasks"] = completed
    state["blocked_tasks"] = blocked
    state["last_completed_task"] = task_id
    state["last_result"] = message
    save_state(state)


def mark_blocked(task_id: str, reason: str) -> None:
    state = load_state()
    blocked = dict(state.get("blocked_tasks", {}))
    blocked[task_id] = reason
    state["blocked_tasks"] = blocked
    state["last_blocked_task"] = task_id
    state["last_result"] = reason
    save_state(state)

