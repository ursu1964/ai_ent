from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ai_ent.product_plan_acceptance import EXPECTED_PRODUCTIZATION_PLAN_HASH
from ai_ent.project_manifest import GENERATED_MARKER, canonical_bytes

PRODUCT_DECISION_RECORDER_VERSION = "product-decision.1"
EXPECTED_PPA_ACCEPTANCE_HASH = "cf97940d5848f8c651232e23b733325f4dd274aa7a45078abab0039d80e638e9"
EXPECTED_PFE_001_HASH = "25cbfa87927d3486e096364c76c532459f75081d2819b0dd2961dfdcbc4855f9"

DecisionResult = Literal["RECORDED", "ALREADY_RECORDED", "BLOCKED", "CONFLICT"]


@dataclass(frozen=True)
class ProductDecisionRecord:
    result: DecisionResult
    recommendation: str
    decision_id: str
    decision_status: str
    decided_at: str
    decided_by: str
    accepted_stack: dict[str, str]
    accepted_product_plan_lineage: dict[str, str]
    prd_dec_002_state: str
    prd_dec_003_state: str
    architectural_conditions: tuple[str, ...]
    non_authorizations: tuple[str, ...]
    implementation_tasks_created: int
    implementation_executions: int
    validation_findings: tuple[dict[str, Any], ...]
    recorder_version: str = PRODUCT_DECISION_RECORDER_VERSION

    @property
    def decision_hash(self) -> str:
        material = self.material_dict()
        material.pop("recommendation")
        material.pop("result")
        return hashlib.sha256(canonical_bytes(material)).hexdigest()

    @property
    def ok(self) -> bool:
        return self.result in {"RECORDED", "ALREADY_RECORDED"}

    def material_dict(self) -> dict[str, Any]:
        return {
            "accepted_product_plan_lineage": self.accepted_product_plan_lineage,
            "accepted_stack": self.accepted_stack,
            "architectural_conditions": list(self.architectural_conditions),
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "decision_id": self.decision_id,
            "decision_status": self.decision_status,
            "implementation_executions": self.implementation_executions,
            "implementation_tasks_created": self.implementation_tasks_created,
            "non_authorizations": list(self.non_authorizations),
            "prd_dec_002_state": self.prd_dec_002_state,
            "prd_dec_003_state": self.prd_dec_003_state,
            "recommendation": self.recommendation,
            "recorder_version": self.recorder_version,
            "result": self.result,
            "validation_findings": list(self.validation_findings),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "phase": "PRD-DECISION",
            **self.material_dict(),
            "decision_hash": self.decision_hash,
        }


def record_prd_dec_001(
    *,
    plan_path: Path = Path(".build/compiled/productization-plan.json"),
    ppa_path: Path = Path("artifacts/ppa-001/PPA-001.json"),
    pfe_path: Path = Path("artifacts/pfe-001/PFE-001.json"),
    artifacts_dir: Path = Path("artifacts"),
    output_dir: Path = Path(".build/compiled"),
    repository_root: Path = Path("."),
    decided_by: str = "operator",
    decided_at: str | None = None,
    expected_plan_hash: str = EXPECTED_PRODUCTIZATION_PLAN_HASH,
    expected_ppa_acceptance_hash: str = EXPECTED_PPA_ACCEPTANCE_HASH,
    expected_pfe_hash: str = EXPECTED_PFE_001_HASH,
) -> ProductDecisionRecord:
    plan = _read_json(plan_path)
    ppa = _read_json(ppa_path)
    pfe = _read_json(pfe_path)
    current_head = _git_rev(repository_root, "HEAD")
    decision_path = artifacts_dir / "product-decisions" / "PRD-DEC-001.json"

    findings = _validation_findings(
        plan=plan,
        ppa=ppa,
        pfe=pfe,
        expected_plan_hash=expected_plan_hash,
        expected_ppa_acceptance_hash=expected_ppa_acceptance_hash,
        expected_pfe_hash=expected_pfe_hash,
    )
    if any(finding["severity"] == "ERROR" for finding in findings):
        return _record(
            result="BLOCKED",
            decided_at=decided_at,
            decided_by=decided_by,
            current_head=current_head,
            ppa=ppa,
            pfe=pfe,
            findings=findings,
        )

    candidate = _record(
        result="RECORDED",
        decided_at=decided_at,
        decided_by=decided_by,
        current_head=current_head,
        ppa=ppa,
        pfe=pfe,
        findings=findings,
    )
    existing = _existing_record(decision_path)
    if existing is not None:
        if _same_decision(existing, candidate):
            return ProductDecisionRecord(
                result="ALREADY_RECORDED",
                recommendation=str(existing["recommendation"]),
                decision_id=str(existing["decision_id"]),
                decision_status=str(existing["decision_status"]),
                decided_at=str(existing["decided_at"]),
                decided_by=str(existing["decided_by"]),
                accepted_stack=dict(existing["accepted_stack"]),
                accepted_product_plan_lineage=dict(existing["accepted_product_plan_lineage"]),
                prd_dec_002_state=str(existing["prd_dec_002_state"]),
                prd_dec_003_state=str(existing["prd_dec_003_state"]),
                architectural_conditions=tuple(existing["architectural_conditions"]),
                non_authorizations=tuple(existing["non_authorizations"]),
                implementation_tasks_created=int(existing["implementation_tasks_created"]),
                implementation_executions=int(existing["implementation_executions"]),
                validation_findings=tuple(existing["validation_findings"]),
                recorder_version=str(existing["recorder_version"]),
            )
        return _record(
            result="CONFLICT",
            decided_at=decided_at,
            decided_by=decided_by,
            current_head=current_head,
            ppa=ppa,
            pfe=pfe,
            findings=(
                *findings,
                {
                    "id": "PRD-DEC-001-CONFLICT",
                    "message": "existing PRD-DEC-001 decision evidence differs from requested approval",
                    "severity": "ERROR",
                },
            ),
        )

    _write_outputs(candidate, artifacts_dir=artifacts_dir, output_dir=output_dir)
    return candidate


def _record(
    *,
    result: DecisionResult,
    decided_at: str | None,
    decided_by: str,
    current_head: str,
    ppa: dict[str, Any],
    pfe: dict[str, Any],
    findings: tuple[dict[str, Any], ...],
) -> ProductDecisionRecord:
    recommendation = (
        "READY_FOR_PDF_001"
        if result in {"RECORDED", "ALREADY_RECORDED"}
        else "REMEDIATION_REQUIRED"
    )
    return ProductDecisionRecord(
        result=result,
        recommendation=recommendation,
        decision_id="PRD-DEC-001",
        decision_status="ACCEPTED"
        if result in {"RECORDED", "ALREADY_RECORDED"}
        else "NOT_RECORDED",
        decided_at=decided_at or datetime.now(UTC).replace(microsecond=0).isoformat(),
        decided_by=decided_by,
        accepted_stack={
            "backend_api": "FastAPI + Pydantic",
            "frontend": "React + TypeScript + Vite",
            "realtime": "SSE-first",
        },
        accepted_product_plan_lineage={
            "current_head": current_head,
            "pfe_001_baseline": str(pfe.get("baseline", "")),
            "pfe_001_hash": str(pfe.get("product_feasibility_hash", "")),
            "ppa_001_acceptance_hash": str(ppa.get("acceptance_hash", "")),
            "ppa_001_baseline": str(ppa.get("baseline", "")),
            "productization_plan_hash": str(pfe.get("productization_plan_hash", "")),
        },
        prd_dec_002_state="UNRESOLVED_MUST_RESOLVE_BEFORE_AFFECTED_TASK",
        prd_dec_003_state="UNRESOLVED_MUST_RESOLVE_BEFORE_AFFECTED_TASK",
        architectural_conditions=(
            "FastAPI/Product API remains an authority-preserving facade over existing application/control-plane services.",
            "React UI receives no direct control-plane authority.",
            "SSE remains observation-only.",
            "Gate approval remains explicit and approver-authorized.",
            "Verification PASS remains independently authoritative.",
            "Git commit/push authority remains behind the existing verified commit boundary.",
            "Artifact access remains authenticated, project-scoped, containment-checked, and redacted.",
            "Generated applications remain isolated from AI-Enterprise source, secrets, PostgreSQL authority, and unrelated workspaces.",
            "Public internet exposure is not authorized by this decision.",
        ),
        non_authorizations=(
            "No productization human gate is approved.",
            "No productization implementation is authorized.",
            "PRD-DEC-002 is not resolved.",
            "PRD-DEC-003 is not resolved.",
            "No LAN or public ingress is authorized.",
            "No production-grade secrets backend decision is made.",
        ),
        implementation_tasks_created=0,
        implementation_executions=0,
        validation_findings=findings,
    )


def _validation_findings(
    *,
    plan: dict[str, Any],
    ppa: dict[str, Any],
    pfe: dict[str, Any],
    expected_plan_hash: str,
    expected_ppa_acceptance_hash: str,
    expected_pfe_hash: str,
) -> tuple[dict[str, Any], ...]:
    findings: list[dict[str, Any]] = []
    if plan.get("plan_hash") != expected_plan_hash:
        findings.append(
            {
                "id": "PRD-DEC-001-PLAN-HASH",
                "message": "accepted product plan hash mismatch",
                "severity": "ERROR",
            }
        )
    if ppa.get("acceptance_hash") != expected_ppa_acceptance_hash:
        findings.append(
            {
                "id": "PRD-DEC-001-PPA-HASH",
                "message": "PPA acceptance hash mismatch",
                "severity": "ERROR",
            }
        )
    if pfe.get("product_feasibility_hash") != expected_pfe_hash:
        findings.append(
            {
                "id": "PRD-DEC-001-PFE-HASH",
                "message": "PFE feasibility hash mismatch",
                "severity": "ERROR",
            }
        )
    if pfe.get("result") != "READY_WITH_DECISIONS_REQUIRED":
        findings.append(
            {
                "id": "PRD-DEC-001-PFE-RESULT",
                "message": "PFE result is not at the expected decision boundary",
                "severity": "ERROR",
            }
        )
    decision = pfe.get("decision_plan", {}).get("PRD-DEC-001", {})
    if decision.get("pfe_recommendation") != "ACCEPT":
        findings.append(
            {
                "id": "PRD-DEC-001-PFE-RECOMMENDATION",
                "message": "PFE did not recommend accepting PRD-DEC-001",
                "severity": "ERROR",
            }
        )
    if pfe.get("summary", {}).get("technical_blockers") != 0:
        findings.append(
            {
                "id": "PRD-DEC-001-TECHNICAL-BLOCKERS",
                "message": "PFE has technical blockers",
                "severity": "ERROR",
            }
        )
    return tuple(sorted(findings, key=lambda item: item["id"]))


def _same_decision(existing: dict[str, Any], candidate: ProductDecisionRecord) -> bool:
    existing_lineage = dict(existing["accepted_product_plan_lineage"])
    candidate_lineage = dict(candidate.as_dict()["accepted_product_plan_lineage"])
    existing_lineage.pop("current_head", None)
    candidate_lineage.pop("current_head", None)
    existing_material = {
        key: existing[key]
        for key in (
            "accepted_stack",
            "architectural_conditions",
            "decision_id",
            "decision_status",
            "implementation_executions",
            "implementation_tasks_created",
            "non_authorizations",
            "prd_dec_002_state",
            "prd_dec_003_state",
        )
    }
    existing_material["accepted_product_plan_lineage"] = existing_lineage
    candidate_material = {
        key: candidate.as_dict()[key]
        for key in (
            "accepted_stack",
            "architectural_conditions",
            "decision_id",
            "decision_status",
            "implementation_executions",
            "implementation_tasks_created",
            "non_authorizations",
            "prd_dec_002_state",
            "prd_dec_003_state",
        )
    }
    candidate_material["accepted_product_plan_lineage"] = candidate_lineage
    return canonical_bytes(existing_material) == canonical_bytes(candidate_material)


def _existing_record(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return _read_json(path)


def _write_outputs(
    decision: ProductDecisionRecord,
    *,
    artifacts_dir: Path,
    output_dir: Path,
) -> None:
    decision_dir = artifacts_dir / "product-decisions"
    decision_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(decision_dir / "PRD-DEC-001.json", decision.as_dict())
    (decision_dir / "PRD-DEC-001.md").write_text(_markdown(decision), encoding="utf-8")
    _write_json(output_dir / "product-decision-PRD-DEC-001.json", decision.as_dict())
    _write_json(output_dir / "product-decision-records.json", {"decisions": [decision.as_dict()]})


def _markdown(decision: ProductDecisionRecord) -> str:
    lines = [
        "# PRD-DEC-001 Product Stack Decision",
        "",
        f"Result: {decision.result}",
        f"Decision status: {decision.decision_status}",
        f"Recommendation: {decision.recommendation}",
        f"Decided at: {decision.decided_at}",
        f"Decision hash: {decision.decision_hash}",
        "",
        "## Accepted Stack",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(decision.accepted_stack.items()))
    lines.extend(
        [
            "",
            "## Lineage",
            f"- Product plan hash: {decision.accepted_product_plan_lineage['productization_plan_hash']}",
            f"- PFE-001 hash: {decision.accepted_product_plan_lineage['pfe_001_hash']}",
            f"- PPA-001 hash: {decision.accepted_product_plan_lineage['ppa_001_acceptance_hash']}",
            "",
            "## Remaining Decisions",
            f"- PRD-DEC-002: {decision.prd_dec_002_state}",
            f"- PRD-DEC-003: {decision.prd_dec_003_state}",
            "",
        ]
    )
    return "\n".join(lines)


def _read_json(path: Path) -> dict[str, Any]:
    value = __import__("json").loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(value) + b"\n")


def _git_rev(cwd: Path, rev: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", rev],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"
