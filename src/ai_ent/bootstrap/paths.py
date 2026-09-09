from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP = ROOT / "manifest" / "bootstrap" / "bootstrap.yaml"
TASK_DIR = ROOT / "manifest" / "bootstrap" / "tasks"
STATE_FILE = ROOT / ".bootstrap" / "state.json"
VENV_PYTHON = ROOT / "aient" / "bin" / "python"

