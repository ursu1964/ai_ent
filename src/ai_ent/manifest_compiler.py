from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_ent.canonical_project_model import (
    CanonicalProjectModel,
    CanonicalProjectModelService,
)
from ai_ent.project_manifest import (
    GENERATED_MARKER,
    PROJECT_MANIFEST_COMPILER_VERSION,
    PROJECT_MANIFEST_ROOT,
    CompilationResult,
    ManifestValidationResult,
    canonical_bytes,
    compile_project_manifest,
    validate_project_manifest,
)

MANIFEST_COMPILER_CONTRACT_VERSION = "c15.1"
MANIFEST_COMPILER_OUTPUT_SCHEMA_VERSION = "manifest-compiler-output-v0.1"


@dataclass(frozen=True)
class ManifestCompilerResult:
    contract_version: str
    schema_version: str
    compilation: CompilationResult
    canonical_model: CanonicalProjectModel

    @property
    def validation_passed(self) -> bool:
        return self.compilation.validation.ok

    @property
    def output_hash(self) -> str:
        return hashlib.sha256(canonical_bytes(self._payload())).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["output_hash"] = self.output_hash
        return payload

    def _payload(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "contract_version": self.contract_version,
            "schema_version": self.schema_version,
            "compiler_version": PROJECT_MANIFEST_COMPILER_VERSION,
            "validation_gate": self.compilation.validation.as_dict(),
            "manifest_lock": self.compilation.lock.as_dict(),
            "compiled_project": self.compilation.compiled.as_dict(),
            "canonical_project_model": self.canonical_model.as_dict(),
            "summary": {
                "validation_passed": self.validation_passed,
                "compiled_project_hash": self.compilation.lock.compiled_hash,
                "canonical_model_hash": self.canonical_model.model_hash,
                "canonical_object_count": len(self.canonical_model.objects),
                "canonical_relationship_count": len(self.canonical_model.relationships),
            },
        }


class ManifestCompilerService:
    """Service boundary for the C15 Manifest Compiler contract."""

    def __init__(
        self,
        *,
        contract_version: str = MANIFEST_COMPILER_CONTRACT_VERSION,
        schema_version: str = MANIFEST_COMPILER_OUTPUT_SCHEMA_VERSION,
    ) -> None:
        self.contract_version = contract_version
        self.schema_version = schema_version

    def validate(self, root: Path = PROJECT_MANIFEST_ROOT) -> ManifestValidationResult:
        return validate_project_manifest(root)

    def compile_project(self, root: Path = PROJECT_MANIFEST_ROOT) -> CompilationResult:
        return compile_project_manifest(root)

    def compile_canonical_model(
        self,
        root: Path = PROJECT_MANIFEST_ROOT,
    ) -> CanonicalProjectModel:
        compilation = self.compile_project(root)
        return CanonicalProjectModelService().from_compilation(compilation)

    def compile(self, root: Path = PROJECT_MANIFEST_ROOT) -> ManifestCompilerResult:
        compilation = self.compile_project(root)
        canonical_model = CanonicalProjectModelService().from_compilation(compilation)
        return ManifestCompilerResult(
            contract_version=self.contract_version,
            schema_version=self.schema_version,
            compilation=compilation,
            canonical_model=canonical_model,
        )

    def compile_for_task_dag(self, root: Path = PROJECT_MANIFEST_ROOT) -> CompilationResult:
        return self.compile_project(root)


def compile_manifest(root: Path = PROJECT_MANIFEST_ROOT) -> ManifestCompilerResult:
    return ManifestCompilerService().compile(root)


def compile_manifest_for_task_dag(root: Path = PROJECT_MANIFEST_ROOT) -> CompilationResult:
    return ManifestCompilerService().compile_for_task_dag(root)
