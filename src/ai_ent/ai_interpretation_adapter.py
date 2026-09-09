from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from ai_ent.canonical_project_model import CanonicalProjectModel
from ai_ent.deterministic_validator import (
    DeterministicValidationReport,
    DeterministicValidatorService,
    GeneratedArtifact,
)
from ai_ent.project_manifest import canonical_bytes

AI_INTERPRETATION_CONTRACT_VERSION = "c04.1"
AI_INTERPRETATION_SCHEMA_VERSION = "ai-interpretation-response-v0.1"
AI_INTERPRETATION_PROMPT_VERSION = "ai-interpretation-prompt-v0.1"

CandidateObjectType = Literal[
    "requirement",
    "capability",
    "objective",
    "stakeholder",
    "constraint",
]
CandidateTruthStatus = Literal["inferred"]
CandidateApprovalStatus = Literal["pending"]
InterpretationStatus = Literal["VALIDATED", "REJECTED"]
HumanBoundary = Literal["approval", "ambiguity", "policy", "reconciliation"]

_BOUNDARIES_REQUIRING_HUMAN_APPROVAL = {"ambiguity", "policy", "reconciliation"}
_CANDIDATE_TYPES = {"requirement", "capability", "objective", "stakeholder", "constraint"}
_IDENTIFIER_RE = re.compile(r"^[A-Z][A-Z0-9-]{1,63}$")


@dataclass(frozen=True)
class SourceSegment:
    segment_id: str
    ordinal: int
    text: str
    content_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "ordinal": self.ordinal,
            "text": self.text,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class AIInterpretationRequest:
    operation_id: str
    contract_version: str
    prompt_version: str
    response_schema_version: str
    source_id: str
    source_hash: str
    source_segments: tuple[SourceSegment, ...]
    model_parameters: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "contract_version": self.contract_version,
            "prompt_version": self.prompt_version,
            "response_schema_version": self.response_schema_version,
            "source_id": self.source_id,
            "source_hash": self.source_hash,
            "source_segments": [segment.as_dict() for segment in self.source_segments],
            "model_parameters": dict(self.model_parameters),
        }


@dataclass(frozen=True)
class AIModelResponse:
    model: str
    model_version: str
    output: Mapping[str, Any]
    usage: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "model_version": self.model_version,
            "output": dict(self.output),
            "usage": dict(self.usage),
        }


class AIModelAdapter(Protocol):
    """Explicit model boundary for controlled interpretation operations."""

    def interpret(self, request: AIInterpretationRequest) -> AIModelResponse:
        """Return schema-bound candidate project knowledge for the request."""
        raise NotImplementedError


@dataclass(frozen=True)
class CandidateKnowledge:
    candidate_id: str
    candidate_type: CandidateObjectType
    title: str
    description: str
    source_segment_ids: tuple[str, ...]
    confidence: float
    truth_status: CandidateTruthStatus
    approval_status: CandidateApprovalStatus
    provenance: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_type": self.candidate_type,
            "title": self.title,
            "description": self.description,
            "source_segment_ids": list(self.source_segment_ids),
            "confidence": self.confidence,
            "truth_status": self.truth_status,
            "approval_status": self.approval_status,
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True)
class CandidateRelationship:
    relationship_id: str
    relationship_type: str
    source_candidate_id: str
    target_candidate_id: str
    source_segment_ids: tuple[str, ...]
    provenance: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "relationship_id": self.relationship_id,
            "relationship_type": self.relationship_type,
            "source_candidate_id": self.source_candidate_id,
            "target_candidate_id": self.target_candidate_id,
            "source_segment_ids": list(self.source_segment_ids),
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True)
class InterpretationFinding:
    finding_id: str
    boundary: HumanBoundary
    message: str
    source_segment_ids: tuple[str, ...]
    requires_human_approval: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "boundary": self.boundary,
            "message": self.message,
            "source_segment_ids": list(self.source_segment_ids),
            "requires_human_approval": self.requires_human_approval,
        }


@dataclass(frozen=True)
class ClarificationQuestion:
    question_id: str
    question: str
    source_segment_ids: tuple[str, ...]
    boundary: HumanBoundary

    def as_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "question": self.question,
            "source_segment_ids": list(self.source_segment_ids),
            "boundary": self.boundary,
        }


@dataclass(frozen=True)
class AIInterpretationResult:
    operation_id: str
    source_id: str
    source_hash: str
    contract_version: str
    prompt_version: str
    response_schema_version: str
    status: InterpretationStatus
    validation_errors: tuple[str, ...]
    candidates: tuple[CandidateKnowledge, ...]
    relationships: tuple[CandidateRelationship, ...]
    findings: tuple[InterpretationFinding, ...]
    clarification_questions: tuple[ClarificationQuestion, ...]
    deterministic_validation: DeterministicValidationReport | None
    human_action_required: bool
    human_approval_reasons: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.status == "VALIDATED" and not self.validation_errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "source_id": self.source_id,
            "source_hash": self.source_hash,
            "contract_version": self.contract_version,
            "prompt_version": self.prompt_version,
            "response_schema_version": self.response_schema_version,
            "status": self.status,
            "ok": self.ok,
            "validation_errors": list(self.validation_errors),
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "relationships": [relationship.as_dict() for relationship in self.relationships],
            "findings": [finding.as_dict() for finding in self.findings],
            "clarification_questions": [
                question.as_dict() for question in self.clarification_questions
            ],
            "deterministic_validation": (
                self.deterministic_validation.as_dict()
                if self.deterministic_validation is not None
                else None
            ),
            "human_action_required": self.human_action_required,
            "human_approval_reasons": list(self.human_approval_reasons),
        }


class AIInterpretationAdapterService:
    """Service boundary for the C04 AI Interpretation Adapter contract."""

    def __init__(
        self,
        model_adapter: AIModelAdapter,
        *,
        validator: DeterministicValidatorService | None = None,
        contract_version: str = AI_INTERPRETATION_CONTRACT_VERSION,
        prompt_version: str = AI_INTERPRETATION_PROMPT_VERSION,
        response_schema_version: str = AI_INTERPRETATION_SCHEMA_VERSION,
    ) -> None:
        self.model_adapter = model_adapter
        self.validator = validator or DeterministicValidatorService()
        self.contract_version = contract_version
        self.prompt_version = prompt_version
        self.response_schema_version = response_schema_version

    def interpret_text(
        self,
        model: CanonicalProjectModel,
        *,
        source_id: str,
        text: str,
        model_parameters: Mapping[str, Any] | None = None,
    ) -> AIInterpretationResult:
        source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        segments = normalize_source_text(source_id, text)
        parameters = dict(model_parameters or {})
        request = AIInterpretationRequest(
            operation_id=_operation_id(
                source_id,
                source_hash,
                self.contract_version,
                self.prompt_version,
                self.response_schema_version,
                parameters,
            ),
            contract_version=self.contract_version,
            prompt_version=self.prompt_version,
            response_schema_version=self.response_schema_version,
            source_id=source_id,
            source_hash=source_hash,
            source_segments=segments,
            model_parameters=parameters,
        )
        response = self.model_adapter.interpret(request)
        validation_errors = _validate_response_schema(
            response.output,
            segments,
            self.response_schema_version,
        )
        if validation_errors:
            return self._rejected_result(request, validation_errors)

        candidates = _candidate_objects(request, response)
        relationships = _candidate_relationships(request, response, candidates)
        findings = _findings(response)
        questions = _clarification_questions(response)
        artifact = _interpretation_artifact(
            model,
            request,
            response,
            candidates,
            relationships,
            findings,
            questions,
        )
        deterministic_validation = self.validator.validate_model(model, artifacts=(artifact,))
        reasons = _human_approval_reasons(candidates, findings, questions)
        if not deterministic_validation.ok:
            reasons = tuple(sorted((*reasons, "deterministic_validation_failed")))

        return AIInterpretationResult(
            operation_id=request.operation_id,
            source_id=request.source_id,
            source_hash=request.source_hash,
            contract_version=request.contract_version,
            prompt_version=request.prompt_version,
            response_schema_version=request.response_schema_version,
            status="VALIDATED" if deterministic_validation.ok else "REJECTED",
            validation_errors=(),
            candidates=candidates if deterministic_validation.ok else (),
            relationships=relationships if deterministic_validation.ok else (),
            findings=findings,
            clarification_questions=questions,
            deterministic_validation=deterministic_validation,
            human_action_required=bool(reasons),
            human_approval_reasons=reasons,
        )

    def _rejected_result(
        self,
        request: AIInterpretationRequest,
        validation_errors: Sequence[str],
    ) -> AIInterpretationResult:
        return AIInterpretationResult(
            operation_id=request.operation_id,
            source_id=request.source_id,
            source_hash=request.source_hash,
            contract_version=request.contract_version,
            prompt_version=request.prompt_version,
            response_schema_version=request.response_schema_version,
            status="REJECTED",
            validation_errors=tuple(sorted(validation_errors)),
            candidates=(),
            relationships=(),
            findings=(),
            clarification_questions=(),
            deterministic_validation=None,
            human_action_required=True,
            human_approval_reasons=("schema_validation_failed",),
        )


def normalize_source_text(source_id: str, text: str) -> tuple[SourceSegment, ...]:
    chunks = tuple(chunk.strip() for chunk in re.split(r"\n\s*\n+", text) if chunk.strip())
    if not chunks and text.strip():
        chunks = (text.strip(),)
    return tuple(
        SourceSegment(
            segment_id=f"{source_id}#S{index:03d}",
            ordinal=index,
            text=chunk,
            content_hash=hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
        )
        for index, chunk in enumerate(chunks, start=1)
    )


def _operation_id(
    source_id: str,
    source_hash: str,
    contract_version: str,
    prompt_version: str,
    response_schema_version: str,
    model_parameters: Mapping[str, Any],
) -> str:
    payload = {
        "source_id": source_id,
        "source_hash": source_hash,
        "contract_version": contract_version,
        "prompt_version": prompt_version,
        "response_schema_version": response_schema_version,
        "model_parameters": dict(model_parameters),
    }
    return f"AIINT-{hashlib.sha256(canonical_bytes(payload)).hexdigest()[:16].upper()}"


def _validate_response_schema(
    output: Mapping[str, Any],
    segments: tuple[SourceSegment, ...],
    expected_schema_version: str,
) -> tuple[str, ...]:
    errors: list[str] = []
    if output.get("schema_version") != expected_schema_version:
        errors.append(f"schema_version must be {expected_schema_version}")
    for key in (
        "candidate_objects",
        "candidate_relationships",
        "findings",
        "clarification_questions",
    ):
        if not isinstance(output.get(key), list):
            errors.append(f"{key} must be a list")
    if errors:
        return tuple(errors)

    segment_ids = {segment.segment_id for segment in segments}
    candidate_ids: set[str] = set()
    for index, raw_candidate in enumerate(_list(output["candidate_objects"]), start=1):
        if not isinstance(raw_candidate, Mapping):
            errors.append(f"candidate_objects[{index}] must be a mapping")
            continue
        candidate_id = _string(raw_candidate.get("id"))
        if not _IDENTIFIER_RE.match(candidate_id):
            errors.append(f"candidate_objects[{index}].id is invalid")
        if candidate_id in candidate_ids:
            errors.append(f"duplicate candidate id:{candidate_id}")
        candidate_ids.add(candidate_id)
        if _string(raw_candidate.get("type")) not in _CANDIDATE_TYPES:
            errors.append(f"candidate_objects[{index}].type is unsupported")
        if not _string(raw_candidate.get("title")):
            errors.append(f"candidate_objects[{index}].title is required")
        if not _string(raw_candidate.get("description")):
            errors.append(f"candidate_objects[{index}].description is required")
        _validate_segment_refs(
            errors,
            f"candidate_objects[{index}].source_segment_ids",
            raw_candidate.get("source_segment_ids"),
            segment_ids,
        )

    for index, raw_relationship in enumerate(
        _list(output["candidate_relationships"]),
        start=1,
    ):
        if not isinstance(raw_relationship, Mapping):
            errors.append(f"candidate_relationships[{index}] must be a mapping")
            continue
        relationship_id = _string(raw_relationship.get("id"))
        if not _IDENTIFIER_RE.match(relationship_id):
            errors.append(f"candidate_relationships[{index}].id is invalid")
        if _string(raw_relationship.get("source_candidate_id")) not in candidate_ids:
            errors.append(f"candidate_relationships[{index}].source_candidate_id is unknown")
        if _string(raw_relationship.get("target_candidate_id")) not in candidate_ids:
            errors.append(f"candidate_relationships[{index}].target_candidate_id is unknown")
        if not _string(raw_relationship.get("type")):
            errors.append(f"candidate_relationships[{index}].type is required")
        _validate_segment_refs(
            errors,
            f"candidate_relationships[{index}].source_segment_ids",
            raw_relationship.get("source_segment_ids"),
            segment_ids,
        )

    for collection_name in ("findings", "clarification_questions"):
        for index, raw_item in enumerate(_list(output[collection_name]), start=1):
            if not isinstance(raw_item, Mapping):
                errors.append(f"{collection_name}[{index}] must be a mapping")
                continue
            if not _IDENTIFIER_RE.match(_string(raw_item.get("id"))):
                errors.append(f"{collection_name}[{index}].id is invalid")
            text_field = "message" if collection_name == "findings" else "question"
            if not _string(raw_item.get(text_field)):
                errors.append(f"{collection_name}[{index}] text is required")
            if _boundary(raw_item.get("boundary")) is None:
                errors.append(f"{collection_name}[{index}].boundary is invalid")
            _validate_segment_refs(
                errors,
                f"{collection_name}[{index}].source_segment_ids",
                raw_item.get("source_segment_ids"),
                segment_ids,
            )
    return tuple(errors)


def _validate_segment_refs(
    errors: list[str],
    field_name: str,
    raw_refs: object,
    segment_ids: set[str],
) -> None:
    refs = _string_tuple(raw_refs)
    if not refs:
        errors.append(f"{field_name} must reference at least one source segment")
        return
    for ref in refs:
        if ref not in segment_ids:
            errors.append(f"{field_name} references unknown source segment {ref}")


def _candidate_objects(
    request: AIInterpretationRequest,
    response: AIModelResponse,
) -> tuple[CandidateKnowledge, ...]:
    return tuple(
        CandidateKnowledge(
            candidate_id=_string(raw["id"]),
            candidate_type=cast(CandidateObjectType, _string(raw["type"])),
            title=_string(raw["title"]),
            description=_string(raw["description"]),
            source_segment_ids=_string_tuple(raw["source_segment_ids"]),
            confidence=_confidence(raw.get("confidence")),
            truth_status="inferred",
            approval_status="pending",
            provenance=_provenance(request, response),
        )
        for raw in _sorted_mappings(response.output["candidate_objects"])
    )


def _candidate_relationships(
    request: AIInterpretationRequest,
    response: AIModelResponse,
    candidates: tuple[CandidateKnowledge, ...],
) -> tuple[CandidateRelationship, ...]:
    candidate_ids = {candidate.candidate_id for candidate in candidates}
    return tuple(
        CandidateRelationship(
            relationship_id=_string(raw["id"]),
            relationship_type=_string(raw["type"]),
            source_candidate_id=_string(raw["source_candidate_id"]),
            target_candidate_id=_string(raw["target_candidate_id"]),
            source_segment_ids=_string_tuple(raw["source_segment_ids"]),
            provenance=_provenance(request, response),
        )
        for raw in _sorted_mappings(response.output["candidate_relationships"])
        if _string(raw["source_candidate_id"]) in candidate_ids
        and _string(raw["target_candidate_id"]) in candidate_ids
    )


def _findings(response: AIModelResponse) -> tuple[InterpretationFinding, ...]:
    return tuple(
        InterpretationFinding(
            finding_id=_string(raw["id"]),
            boundary=_boundary(raw["boundary"]) or "ambiguity",
            message=_string(raw["message"]),
            source_segment_ids=_string_tuple(raw["source_segment_ids"]),
            requires_human_approval=_string(raw["boundary"])
            in _BOUNDARIES_REQUIRING_HUMAN_APPROVAL,
        )
        for raw in _sorted_mappings(response.output["findings"])
    )


def _clarification_questions(response: AIModelResponse) -> tuple[ClarificationQuestion, ...]:
    return tuple(
        ClarificationQuestion(
            question_id=_string(raw["id"]),
            question=_string(raw["question"]),
            source_segment_ids=_string_tuple(raw["source_segment_ids"]),
            boundary=_boundary(raw["boundary"]) or "ambiguity",
        )
        for raw in _sorted_mappings(response.output["clarification_questions"])
    )


def _interpretation_artifact(
    model: CanonicalProjectModel,
    request: AIInterpretationRequest,
    response: AIModelResponse,
    candidates: tuple[CandidateKnowledge, ...],
    relationships: tuple[CandidateRelationship, ...],
    findings: tuple[InterpretationFinding, ...],
    questions: tuple[ClarificationQuestion, ...],
) -> GeneratedArtifact:
    payload = {
        "operation_id": request.operation_id,
        "contract_version": request.contract_version,
        "prompt_version": request.prompt_version,
        "response_schema_version": request.response_schema_version,
        "model_parameters": dict(request.model_parameters),
        "source_id": request.source_id,
        "source_hash": request.source_hash,
        "source_segments": [segment.as_dict() for segment in request.source_segments],
        "model": response.model,
        "model_version": response.model_version,
        "usage": dict(response.usage),
        "candidates": [candidate.as_dict() for candidate in candidates],
        "relationships": [relationship.as_dict() for relationship in relationships],
        "findings": [finding.as_dict() for finding in findings],
        "clarification_questions": [question.as_dict() for question in questions],
    }
    return GeneratedArtifact(
        artifact_id=f"{request.operation_id}-candidate-knowledge",
        artifact_type="ai-interpretation-candidate-knowledge",
        source_model_hash=model.model_hash,
        payload=payload,
        producer="ai-interpretation-adapter",
        claimed_payload_hash=hashlib.sha256(canonical_bytes(payload)).hexdigest(),
        claimed_authoritative=False,
    )


def _human_approval_reasons(
    candidates: tuple[CandidateKnowledge, ...],
    findings: tuple[InterpretationFinding, ...],
    questions: tuple[ClarificationQuestion, ...],
) -> tuple[str, ...]:
    reasons: set[str] = set()
    if candidates:
        reasons.add("pending_candidate_review")
    for finding in findings:
        if finding.requires_human_approval:
            reasons.add(f"{finding.boundary}_boundary:{finding.finding_id}")
    for question in questions:
        if question.boundary in _BOUNDARIES_REQUIRING_HUMAN_APPROVAL:
            reasons.add(f"{question.boundary}_question:{question.question_id}")
    return tuple(sorted(reasons))


def _provenance(
    request: AIInterpretationRequest,
    response: AIModelResponse,
) -> dict[str, Any]:
    return {
        "operation_id": request.operation_id,
        "source_id": request.source_id,
        "source_hash": request.source_hash,
        "contract_version": request.contract_version,
        "prompt_version": request.prompt_version,
        "response_schema_version": request.response_schema_version,
        "model_parameters": dict(request.model_parameters),
        "model": response.model,
        "model_version": response.model_version,
    }


def _sorted_mappings(raw_items: object) -> tuple[Mapping[str, Any], ...]:
    mappings = tuple(item for item in _list(raw_items) if isinstance(item, Mapping))
    return tuple(sorted(mappings, key=lambda item: _string(item.get("id"))))


def _list(raw: object) -> list[object]:
    return list(raw) if isinstance(raw, list) else []


def _string(raw: object) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def _string_tuple(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(item.strip() for item in raw if isinstance(item, str) and item.strip())


def _confidence(raw: object) -> float:
    if isinstance(raw, int | float):
        return max(0.0, min(float(raw), 1.0))
    return 0.0


def _boundary(raw: object) -> HumanBoundary | None:
    value = _string(raw)
    if value in {"approval", "ambiguity", "policy", "reconciliation"}:
        return cast(HumanBoundary, value)
    return None
