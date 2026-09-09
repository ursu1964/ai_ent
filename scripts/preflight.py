from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str]) -> tuple[bool, str]:
    executable = shutil.which(command[0])
    if executable is None:
        return False, f"{command[0]}: missing"
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        return False, f"{command[0]}: {exc}"
    first_line = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else "ok"
    return completed.returncode == 0, first_line


def check() -> int:
    checks: list[tuple[str, bool, str]] = []

    checks.append(("root", ROOT.exists(), str(ROOT)))
    checks.append(("git", *_run(["git", "--version"])))
    checks.append(("python", True, sys.version.split()[0]))
    checks.append(("env-example", (ROOT / ".env.example").exists(), ".env.example"))
    checks.append(("env-private", (ROOT / ".env").exists(), ".env"))
    checks.append(("venv", (ROOT / "aient" / "bin" / "python").exists(), "aient/bin/python"))
    checks.append(("docker", *_run(["docker", "--version"])))
    checks.append(("docker-compose", *_run(["docker", "compose", "version"])))
    checks.append(("psql", *_run(["psql", "--version"])))

    ollama_path = shutil.which("ollama")
    if ollama_path:
        checks.append(("ollama", *_run(["ollama", "--version"])))
    else:
        checks.append(("ollama", True, "optional: missing"))

    failed_required = False
    for name, ok, detail in checks:
        status = "PASS" if ok else "WARN" if name in {"docker", "docker-compose", "psql"} else "FAIL"
        print(f"{status} {name}: {detail}")
        if not ok and status == "FAIL":
            failed_required = True

    if os.environ.get("VIRTUAL_ENV"):
        print(f"PASS active-venv: {os.environ['VIRTUAL_ENV']}")
    else:
        print("WARN active-venv: not activated")

    return 1 if failed_required else 0


if __name__ == "__main__":
    raise SystemExit(check())
