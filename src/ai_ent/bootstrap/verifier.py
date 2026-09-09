from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ai_ent.bootstrap.models import BootstrapTask, CommandResult, VerificationResult
from ai_ent.bootstrap.paths import ROOT, VENV_PYTHON


def verification_env() -> dict[str, str]:
    env = os.environ.copy()
    venv_bin = str(VENV_PYTHON.parent)
    env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
    return env


def run_command(command: str, cwd: Path = ROOT) -> CommandResult:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=verification_env(),
        shell=True,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return CommandResult(
        command=command,
        returncode=completed.returncode,
        output=completed.stdout.strip(),
    )


def verify_task(task: BootstrapTask, cwd: Path = ROOT) -> VerificationResult:
    results = tuple(run_command(command, cwd=cwd) for command in task.verification.commands)
    return VerificationResult(ok=all(result.ok for result in results), commands=results)

