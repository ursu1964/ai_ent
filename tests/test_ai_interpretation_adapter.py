from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ai_ent.ai_interpretation_adapter import (
    AI_INTERPRETATION_CONTRACT_VERSION,
    AI_INTERPRETATION_PROMPT_VERSION,
    AI_INTERPRETATION_SCHEMA_VERSION,
    AIInterpretationAdapterService,
    AIInterpretationRequest,
    AIModelResponse,
)
from ai_ent.canonical_project_model import compile_canonical_project_model

MANIFEST_ROOT = Path("manifest/project/ai-ent")


@dataclass
class FakeModelAdapter:
    output: dict[str, object]
    request: AIInterpretationRequest | None = None

    def interpret(self, request: AIInterpretationRequest) -> AIModelResponse:
        self.request = request
        return AIModelResponse(
            model="fake-model",
            model_version="2026-09-09",
            output=self.output,
            usage={"input_tokens": 42, "output_tokens": 17},
        )


def test_int_002_invokes_model_adapter_through_versioned_contract() -> None:
    model = compile_canonical_project_model(MANIFEST_ROOT)
    fake = FakeModelAdapter(
        {
            "schema_version": AI_INTERPRETATION_SCHEMA_VERSION,
            "candidate_objects": [
                {
                    "id": "REQ-CRM",
                    "type": "requirement",
                    "title": "Track customer follow up",
                    "description": "Sales users need follow-up reminders for active opportunities.",
                    "source_segment_ids": ["CLIENT-BRIEF#S001"],
                    "confidence": 0.82,
                }
            ],
            "candidate_relationships": [],
            "findings": [],
            "clarification_questions": [],
        }
    )

    result = AIInterpretationAdapterService(fake).interpret_text(
        model,
        source_id="CLIENT-BRIEF",
        text="Sales users need follow-up reminders for active opportunities.",
        model_parameters={"temperature": 0},
    )

    assert fake.request is not None
    assert fake.request.contract_version == AI_INTERPRETATION_CONTRACT_VERSION
    assert fake.request.prompt_version == AI_INTERPRETATION_PROMPT_VERSION
    assert fake.request.response_schema_version == AI_INTERPRETATION_SCHEMA_VERSION
    assert fake.request.source_segments[0].segment_id == "CLIENT-BRIEF#S001"
    assert result.ok
    assert result.candidates[0].provenance["operation_id"] == result.operation_id
    assert result.candidates[0].provenance["model"] == "fake-model"
    validation = result.deterministic_validation
    assert validation is not None
    assert validation.ok


def test_sec_001_ai_candidates_remain_pending_and_non_authoritative() -> None:
    model = compile_canonical_project_model(MANIFEST_ROOT)
    fake = FakeModelAdapter(
        {
            "schema_version": AI_INTERPRETATION_SCHEMA_VERSION,
            "candidate_objects": [
                {
                    "id": "CAP-AUTH",
                    "type": "capability",
                    "title": "Approved by model",
                    "description": "The model is trying to imply authority.",
                    "source_segment_ids": ["CLIENT-AUTH#S001"],
                    "confidence": 1.0,
                    "truth_status": "fact",
                    "approval_status": "approved",
                    "claimed_authoritative": True,
                }
            ],
            "candidate_relationships": [],
            "findings": [],
            "clarification_questions": [],
        }
    )

    result = AIInterpretationAdapterService(fake).interpret_text(
        model,
        source_id="CLIENT-AUTH",
        text="The model is trying to imply authority.",
    )

    assert result.ok
    assert result.candidates[0].truth_status == "inferred"
    assert result.candidates[0].approval_status == "pending"
    assert result.human_action_required
    assert result.human_approval_reasons == ("pending_candidate_review",)
    validation = result.deterministic_validation
    assert validation is not None
    generated_artifact = validation.generated_artifacts[0]
    assert generated_artifact["claimed_authoritative"] is False


def test_sec_003_policy_ambiguity_and_reconciliation_boundaries_require_human_review() -> None:
    model = compile_canonical_project_model(MANIFEST_ROOT)
    fake = FakeModelAdapter(
        {
            "schema_version": AI_INTERPRETATION_SCHEMA_VERSION,
            "candidate_objects": [],
            "candidate_relationships": [],
            "findings": [
                {
                    "id": "AMB-001",
                    "boundary": "ambiguity",
                    "message": "The source uses customer and account interchangeably.",
                    "source_segment_ids": ["CLIENT-GAPS#S001"],
                },
                {
                    "id": "POL-001",
                    "boundary": "policy",
                    "message": "Retention policy is mentioned without an approved rule.",
                    "source_segment_ids": ["CLIENT-GAPS#S002"],
                },
            ],
            "clarification_questions": [
                {
                    "id": "REC-001",
                    "boundary": "reconciliation",
                    "question": "Should customer records reconcile against the existing CRM?",
                    "source_segment_ids": ["CLIENT-GAPS#S002"],
                }
            ],
        }
    )

    result = AIInterpretationAdapterService(fake).interpret_text(
        model,
        source_id="CLIENT-GAPS",
        text=(
            "The source uses customer and account interchangeably.\n\n"
            "Retention policy is mentioned without an approved rule."
        ),
    )

    assert result.ok
    assert result.human_action_required
    assert result.human_approval_reasons == (
        "ambiguity_boundary:AMB-001",
        "policy_boundary:POL-001",
        "reconciliation_question:REC-001",
    )
    assert all(finding.requires_human_approval for finding in result.findings)


def test_malformed_ai_output_is_rejected_before_deterministic_validation() -> None:
    model = compile_canonical_project_model(MANIFEST_ROOT)
    fake = FakeModelAdapter(
        {
            "schema_version": "freeform",
            "message": "I think this project needs a CRM.",
        }
    )

    result = AIInterpretationAdapterService(fake).interpret_text(
        model,
        source_id="CLIENT-FREEFORM",
        text="I think this project needs a CRM.",
    )

    assert not result.ok
    assert result.status == "REJECTED"
    assert result.candidates == ()
    assert result.deterministic_validation is None
    assert result.human_action_required
    assert "schema_validation_failed" in result.human_approval_reasons
