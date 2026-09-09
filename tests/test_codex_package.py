from __future__ import annotations

from ai_ent.bootstrap.codex import build_execution_package
from ai_ent.bootstrap.models import BootstrapTask


def test_execution_package_respects_allowed_source_paths() -> None:
    task = BootstrapTask(
        id="IMPL-TEST",
        stage="runtime-plan",
        title="Runtime task",
        executor="codex",
        allowed_paths=("src/ai_ent/**", "tests/**"),
        objective="Implement a runtime task.",
    )

    package = build_execution_package(task)

    assert "- src/ai_ent/**" in package.instructions
    assert "Do not modify source code, manifests, scripts" not in package.instructions
    assert "Do not modify files outside allowed paths" in package.instructions
