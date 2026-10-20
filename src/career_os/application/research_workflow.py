"""Strict, candidate-only research annotation import contracts."""

from __future__ import annotations

import json
from datetime import date
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from career_os.domain.research import (
    ResearchClaimDraft,
    ResearchDataPeriod,
    ResearchDateState,
    ResearchEvidenceKind,
    ResearchLineageKind,
    ResearchMediaType,
    ResearchMissingReason,
    ResearchQuestion,
    ResearchTextLocation,
)

_SCHEMA_VERSION = "career-os.research-claim-drafts.v1"
_MAX_DRAFTS = 500
_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024


class ResearchWorkflowPayloadError(ValueError):
    """An annotation payload is malformed or does not match the strict schema."""


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _LocationInput(_StrictInput):
    media_type: ResearchMediaType
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    section: str | None
    page_number: int | None

    @field_validator("section")
    @classmethod
    def nonblank_section(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("section must be non-blank when reported")
        return value


class _DataPeriodInput(_StrictInput):
    start: date | None
    end: date | None
    state: ResearchDateState


class _ClaimInput(_StrictInput):
    document_version_id: UUID
    question: ResearchQuestion
    statement: str
    exact_quote: str
    location: _LocationInput
    evidence_kind: ResearchEvidenceKind
    metric_definition: str | None
    metric_value: str | None
    denominator: str | None
    geography: str | None
    sample_description: str | None
    sample_size: int | None = Field(default=None, ge=1)
    data_period: _DataPeriodInput
    limitations: str | None
    missing_reasons: dict[
        Literal[
            "metric_definition",
            "metric_value",
            "denominator",
            "geography",
            "sample_description",
            "sample_size",
            "limitations",
        ],
        ResearchMissingReason,
    ]
    lineage_kind: ResearchLineageKind
    upstream_dataset_or_document: str | None

    @field_validator("statement", "exact_quote")
    @classmethod
    def required_text_must_be_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must be non-blank")
        return value

    @field_validator(
        "metric_definition",
        "metric_value",
        "denominator",
        "geography",
        "sample_description",
        "limitations",
        "upstream_dataset_or_document",
    )
    @classmethod
    def reported_text_must_be_nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("reported text must be non-blank")
        return value

    def to_domain(self) -> ResearchClaimDraft:
        location = ResearchTextLocation(
            media_type=self.location.media_type,
            start_offset=self.location.start_offset,
            end_offset=self.location.end_offset,
            section=self.location.section,
            page_number=self.location.page_number,
        )
        period = ResearchDataPeriod(
            start=self.data_period.start,
            end=self.data_period.end,
            state=self.data_period.state,
        )
        return ResearchClaimDraft(
            document_version_id=self.document_version_id,
            question=self.question,
            statement=self.statement,
            exact_quote=self.exact_quote,
            location=location,
            evidence_kind=self.evidence_kind,
            metric_definition=self.metric_definition,
            metric_value=self.metric_value,
            denominator=self.denominator,
            geography=self.geography,
            sample_description=self.sample_description,
            sample_size=self.sample_size,
            data_period=period,
            limitations=self.limitations,
            missing_reasons=cast(dict[str, ResearchMissingReason], self.missing_reasons),
            lineage_kind=self.lineage_kind,
            upstream_dataset_or_document=self.upstream_dataset_or_document,
        )


class _ClaimDraftEnvelope(_StrictInput):
    schema_version: Literal["career-os.research-claim-drafts.v1"]
    claims: list[_ClaimInput] = Field(min_length=1, max_length=_MAX_DRAFTS)


class _HypothesisReviewInput(_StrictInput):
    id: Literal[
        "developer_task_mix_change",
        "seniority_differentiation",
        "production_ai_skills_shift",
    ]
    status: Literal["supported", "mixed", "contradicted", "insufficient"]
    rationale: str
    accepted_claim_ids: list[UUID]
    alternative_explanations: list[str] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)

    @field_validator("rationale")
    @classmethod
    def rationale_must_be_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rationale must be non-blank")
        return value

    @field_validator("alternative_explanations", "limitations")
    @classmethod
    def list_items_must_be_nonblank(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("review notes must be non-blank")
        return value


class _MonthlyReviewInput(_StrictInput):
    period_id: str
    reviewer: str
    hypotheses: list[_HypothesisReviewInput] = Field(min_length=3, max_length=3)

    @field_validator("period_id")
    @classmethod
    def validate_period_id(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", value):
            raise ValueError("period_id must use YYYY-MM")
        return value

    @field_validator("reviewer")
    @classmethod
    def reviewer_must_be_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reviewer must be non-blank")
        return value

    @field_validator("hypotheses")
    @classmethod
    def hypothesis_ids_must_be_unique(
        cls, value: list[_HypothesisReviewInput]
    ) -> list[_HypothesisReviewInput]:
        if len({item.id for item in value}) != len(value):
            raise ValueError("monthly hypothesis ids must be unique")
        return value


def parse_claim_drafts(payload: bytes) -> tuple[ResearchClaimDraft, ...]:
    """Validate a strict versioned JSON payload and construct candidate drafts.

    Duplicate object keys are rejected so a later value cannot silently replace
    a reported date, quote, or locator. Payload contents remain inert text.
    """
    if len(payload) > _MAX_PAYLOAD_BYTES:
        raise ResearchWorkflowPayloadError("claim draft payload exceeds the 8 MiB limit")
    try:
        source = payload.decode("utf-8")
        json.loads(source, object_pairs_hook=_reject_duplicate_keys)
        envelope = _ClaimDraftEnvelope.model_validate_json(source, strict=True)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ResearchWorkflowPayloadError("claim draft payload is not valid UTF-8 JSON") from error
    except ValidationError as error:
        paths = [
            ".".join(str(part) for part in item["loc"])
            for item in error.errors(include_input=False)
        ]
        summary = ", ".join(paths[:12]) or "schema"
        raise ResearchWorkflowPayloadError(
            f"claim draft schema validation failed at {summary}"
        ) from error
    except ValueError as error:
        raise ResearchWorkflowPayloadError(str(error)) from error

    if envelope.schema_version != _SCHEMA_VERSION:
        raise ResearchWorkflowPayloadError("unsupported claim draft schema version")
    try:
        return tuple(claim.to_domain() for claim in envelope.claims)
    except (TypeError, ValueError) as error:
        raise ResearchWorkflowPayloadError(
            f"claim draft domain validation failed: {error}"
        ) from error


def parse_monthly_review(payload: bytes) -> dict[str, object]:
    """Validate a strict explicit monthly review before the monitor checks evidence."""
    if len(payload) > 1024 * 1024:
        raise ResearchWorkflowPayloadError("monthly review payload exceeds the 1 MiB limit")
    try:
        source = payload.decode("utf-8")
        json.loads(source, object_pairs_hook=_reject_duplicate_keys)
        review = _MonthlyReviewInput.model_validate_json(source, strict=True)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ResearchWorkflowPayloadError("monthly review is not valid UTF-8 JSON") from error
    except ValidationError as error:
        paths = [
            ".".join(str(part) for part in item["loc"])
            for item in error.errors(include_input=False)
        ]
        summary = ", ".join(paths[:12]) or "schema"
        raise ResearchWorkflowPayloadError(
            f"monthly review schema validation failed at {summary}"
        ) from error
    except ResearchWorkflowPayloadError:
        raise
    return review.model_dump(mode="json")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ResearchWorkflowPayloadError("claim draft JSON contains a duplicate object key")
        result[key] = value
    return result
