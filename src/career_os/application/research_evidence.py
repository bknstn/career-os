"""Read-only projections for the source-grounded research evidence stream."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol
from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select
from sqlalchemy.sql.selectable import Subquery

from career_os.domain.research import ResearchQuestion
from career_os.persistence.models import (
    ResearchAcquisitionAttempt,
    ResearchClaim,
    ResearchClaimReview,
    ResearchCollectionRun,
    ResearchDocument,
    ResearchDocumentVersion,
)

type Row = Mapping[str, object]


class ResearchEvidenceReadStore(Protocol):
    """Small query seam that keeps evidence policy independently testable."""

    def list_documents(self, *, page: int, page_size: int) -> tuple[Sequence[Row], int]: ...

    def get_version(self, document_id: UUID, version_id: UUID) -> Row | None: ...

    def list_claims(
        self,
        *,
        question: ResearchQuestion | None,
        version_id: UUID | None,
        page: int,
        page_size: int,
    ) -> tuple[Sequence[Row], int]: ...

    def list_claim_summaries(self) -> Sequence[Row]: ...

    def review_history(self, claim_ids: Sequence[UUID]) -> Mapping[UUID, Sequence[Row]]: ...

    def latest_text_digests(self, document_ids: Sequence[UUID]) -> Mapping[UUID, str | None]: ...

    def document_counts_by_source(self) -> Sequence[Row]: ...

    def latest_registry_snapshot(self) -> Row | None: ...

    def source_attempt_statuses(self) -> Sequence[Row]: ...

    def acquisition_attempts(self, *, source_key: str, urls: Sequence[str]) -> Sequence[Row]: ...


@dataclass(frozen=True, slots=True)
class ResearchEvidenceContext:
    """Question-specific search pointers; these never join job denominators."""

    key: ResearchQuestion
    label: str
    prompt: str
    job_terms: tuple[str, ...]
    skill_terms: tuple[str, ...]
    reading_note: str


_QUESTION_CONTEXTS = (
    ResearchEvidenceContext(
        key=ResearchQuestion.DEVELOPER_TASKS,
        label="Developer task change",
        prompt="How are developers' tasks changing as AI capabilities and use change?",
        job_terms=("software engineer", "developer", "AI coding", "code review"),
        skill_terms=("software engineering", "AI-assisted development", "automation"),
        reading_note=(
            "Separate task mix and reported use from measured productivity, hiring, or employment. "
            "Adoption alone does not show that task composition changed."
        ),
    ),
    ResearchEvidenceContext(
        key=ResearchQuestion.EXPERIENCE_SENIORITY,
        label="Experience and seniority",
        prompt="How do impacts and task changes differ by experience and seniority?",
        job_terms=("senior engineer", "staff engineer", "years of experience", "entry-level"),
        skill_terms=("technical leadership", "architecture", "mentoring", "review"),
        reading_note=(
            "Keep worker populations, role definitions, and task difficulty visible; do not treat "
            "one experienced-developer experiment as representative of all seniority levels."
        ),
    ),
    ResearchEvidenceContext(
        key=ResearchQuestion.PRODUCTION_AI_SKILLS,
        label="Production AI skills",
        prompt="Which skills are becoming useful for building and operating AI in production?",
        job_terms=("AI engineer", "MLOps", "model evaluation", "AI observability"),
        skill_terms=(
            "evaluation",
            "reliability",
            "integration",
            "data",
            "observability",
            "security",
        ),
        reading_note=(
            "Distinguish production evaluation and operations from prompt familiarity; advertised "
            "job-posting skills remain a separate denominator from research samples."
        ),
    ),
)
_TERMINAL_REVIEW_STATES = frozenset({"superseded", "retracted"})


class ResearchEvidenceService:
    """Build simple read-only API projections from a supplied research read store."""

    def __init__(self, store: ResearchEvidenceReadStore) -> None:
        self._store = store

    def overview(self) -> dict[str, object]:
        latest_registry = self._store.latest_registry_snapshot()
        source_counts = self._source_coverage(latest_registry)
        claim_rows = tuple(self._store.list_claim_summaries())
        claim_states = self._current_claim_states(claim_rows)
        questions = self._question_payloads(claim_rows, claim_states)
        current_count = sum(bool(item["current_evidence"]) for item in claim_states)
        latest_accepted = sum(item["review_state"] == "accepted" for item in claim_states)
        return {
            "schema_version": "career-radar.research.overview.v1",
            "documents": {
                "body_bearing_count": sum(_int(row.get("document_count")) for row in source_counts),
                "extractable_count": sum(
                    _int(row.get("extractable_count")) for row in source_counts
                ),
                "extraction_error_count": sum(
                    _int(row.get("extraction_error_count")) for row in source_counts
                ),
                "by_source": source_counts,
            },
            "source_registry": _registry_summary(latest_registry),
            "claims": {
                "total_count": len(claim_rows),
                "latest_review_accepted_count": latest_accepted,
                "current_evidence_count": current_count,
                "not_current_count": len(claim_rows) - current_count,
            },
            "questions": questions,
            "interpretation_note": (
                "Claim and document counts describe the stored review corpus. No hypothesis status "
                "is inferred from counts or publisher brands."
            ),
        }

    def _source_coverage(self, latest_registry: Row | None) -> list[dict[str, object]]:
        by_key: dict[str, dict[str, object]] = {}
        for source in _enabled_sources_from_latest_registry(latest_registry):
            key = _str(source.get("key"))
            if key is None:
                continue
            by_key[key] = {
                "source_key": key,
                "publisher": _str(source.get("publisher")),
                "enabled": True,
                "document_count": 0,
                "extractable_count": 0,
                "extraction_error_count": 0,
                "latest_attempt": None,
                "last_success_at": None,
            }
        for row in self._store.document_counts_by_source():
            key = _str(row.get("source_key"))
            if key is None:
                continue
            entry = by_key.setdefault(
                key,
                {
                    "source_key": key,
                    "publisher": None,
                    "enabled": None,
                    "document_count": 0,
                    "extractable_count": 0,
                    "extraction_error_count": 0,
                    "latest_attempt": None,
                    "last_success_at": None,
                },
            )
            for count_name in ("document_count", "extractable_count", "extraction_error_count"):
                entry[count_name] = _int(row.get(count_name))
        for row in self._store.source_attempt_statuses():
            key = _str(row.get("source_key"))
            if key is None:
                continue
            entry = by_key.setdefault(
                key,
                {
                    "source_key": key,
                    "publisher": None,
                    "enabled": None,
                    "document_count": 0,
                    "extractable_count": 0,
                    "extraction_error_count": 0,
                    "latest_attempt": None,
                    "last_success_at": None,
                },
            )
            entry["latest_attempt"] = {
                "outcome": _str(row.get("outcome")),
                "occurred_at": _datetime_string(row.get("occurred_at")),
                "http_status": _int_or_none(row.get("http_status")),
                "bytes_received": _int(row.get("bytes_received")),
                "error_code": _str(row.get("error_code")),
                "run_id": _uuid_string(row.get("run_id")),
                "run_status": _str(row.get("run_status")),
                "run_started_at": _datetime_string(row.get("run_started_at")),
                "run_finished_at": _datetime_string(row.get("run_finished_at")),
            }
            entry["last_success_at"] = _datetime_string(row.get("last_success_at"))
        return [by_key[key] for key in sorted(by_key)]

    def questions(self) -> dict[str, object]:
        claim_rows = tuple(self._store.list_claim_summaries())
        claim_states = self._current_claim_states(claim_rows)
        return {
            "schema_version": "career-radar.research.questions.v1",
            "items": self._question_payloads(claim_rows, claim_states),
        }

    def documents(self, *, page: int, page_size: int) -> dict[str, object]:
        rows, total = self._store.list_documents(page=page, page_size=page_size)
        return {
            "schema_version": "career-radar.research.documents.v1",
            "page": page,
            "page_size": page_size,
            "total": total,
            "items": [_document_payload(row) for row in rows],
        }

    def version_evidence(self, document_id: UUID, version_id: UUID) -> dict[str, object] | None:
        version = self._store.get_version(document_id, version_id)
        if version is None:
            return None
        claim_rows, claim_total = self._store.list_claims(
            question=None,
            version_id=version_id,
            page=1,
            page_size=100,
        )
        claim_payloads = self._claim_payloads(claim_rows)
        source_key = _str(version.get("source_key")) or ""
        urls = tuple(
            dict.fromkeys(
                value
                for value in (
                    _str(version.get("canonical_url")),
                    _str(version.get("discovered_url")),
                    _str(version.get("retrieved_url")),
                )
                if value is not None
            )
        )
        attempts = self._store.acquisition_attempts(source_key=source_key, urls=urls)
        text_value = _str(version.get("normalized_text"))
        return {
            "schema_version": "career-radar.research.version-evidence.v1",
            "document": {
                "id": _uuid_string(version.get("document_id")),
                "source_key": source_key,
                "canonical_url": _str(version.get("canonical_url")),
                "created_at": _datetime_string(version.get("document_created_at")),
            },
            "version": _version_payload(version),
            "normalized_text": text_value,
            "escaped_normalized_text": _escape_for_html(text_value),
            "regions": _regions_payload(version.get("extraction_regions")),
            "claims": {
                "items": claim_payloads,
                "total": claim_total,
                "truncated": claim_total > len(claim_payloads),
            },
            "acquisition_attempts": [_attempt_payload(row) for row in attempts],
            "body_policy": (
                "Original response bytes are retained in the database and are not served "
                "by this API."
            ),
        }

    def claims(
        self,
        *,
        question: ResearchQuestion | None,
        page: int,
        page_size: int,
    ) -> dict[str, object]:
        rows, total = self._store.list_claims(
            question=question,
            version_id=None,
            page=page,
            page_size=page_size,
        )
        return {
            "schema_version": "career-radar.research.claims.v1",
            "page": page,
            "page_size": page_size,
            "total": total,
            "items": self._claim_payloads(rows),
            "current_view_rule": (
                "Current evidence requires a latest accepted review, no superseded/retracted "
                "event, and a non-null claim-version normalized digest equal to the latest "
                "document digest."
            ),
        }

    def _claim_payloads(self, rows: Sequence[Row]) -> list[dict[str, object]]:
        claim_ids = tuple(_as_uuid(row.get("claim_id")) for row in rows)
        document_ids = tuple(dict.fromkeys(_as_uuid(row.get("document_id")) for row in rows))
        histories = self._store.review_history(claim_ids)
        latest_digests = self._store.latest_text_digests(document_ids)
        return [
            _claim_payload(
                row,
                histories.get(_as_uuid(row.get("claim_id")), ()),
                latest_digests.get(_as_uuid(row.get("document_id"))),
            )
            for row in rows
        ]

    def _current_claim_states(self, rows: Sequence[Row]) -> list[dict[str, object]]:
        claim_ids = tuple(_as_uuid(row.get("claim_id")) for row in rows)
        document_ids = tuple(dict.fromkeys(_as_uuid(row.get("document_id")) for row in rows))
        histories = self._store.review_history(claim_ids)
        latest_digests = self._store.latest_text_digests(document_ids)
        states: list[dict[str, object]] = []
        for row in rows:
            claim_id = _as_uuid(row.get("claim_id"))
            document_id = _as_uuid(row.get("document_id"))
            version_digest = _str(row.get("claim_text_digest"))
            current, review_state, exclusion = _assess_current_evidence(
                claim_version_digest=version_digest,
                latest_document_digest=latest_digests.get(document_id),
                reviews=histories.get(claim_id, ()),
            )
            states.append(
                {
                    "claim_id": claim_id,
                    "question": _str(row.get("question")) or "",
                    "current_evidence": current,
                    "review_state": review_state,
                    "exclusion_reason": exclusion,
                }
            )
        return states

    def _question_payloads(
        self,
        rows: Sequence[Row],
        states: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        state_by_id = {state["claim_id"]: state for state in states}
        payloads: list[dict[str, object]] = []
        for context in _QUESTION_CONTEXTS:
            question_rows = [row for row in rows if _str(row.get("question")) == context.key.value]
            current_rows = [
                row
                for row in question_rows
                if bool(state_by_id.get(_as_uuid(row.get("claim_id")), {}).get("current_evidence"))
            ]
            payloads.append(_question_payload(context, question_rows, current_rows))
        return payloads


class SqlResearchEvidenceReadStore:
    """Explicit-column PostgreSQL queries that never select original body bytes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_documents(self, *, page: int, page_size: int) -> tuple[Sequence[Row], int]:
        latest = _latest_version_numbers()
        base = (
            select(ResearchDocument.id)
            .join(latest, latest.c.document_id == ResearchDocument.id)
            .join(
                ResearchDocumentVersion,
                (ResearchDocumentVersion.document_id == ResearchDocument.id)
                & (ResearchDocumentVersion.version_number == latest.c.version_number),
            )
        )
        total = int(self._session.scalar(select(func.count()).select_from(base.subquery())) or 0)
        statement = (
            select(
                ResearchDocument.id.label("document_id"),
                ResearchDocument.source_key,
                ResearchDocument.canonical_url,
                ResearchDocument.created_at.label("document_created_at"),
                ResearchDocumentVersion.id.label("version_id"),
                ResearchDocumentVersion.version_number,
                ResearchDocumentVersion.title,
                ResearchDocumentVersion.publication_date,
                ResearchDocumentVersion.publication_date_state,
                ResearchDocumentVersion.retrieved_at,
                ResearchDocumentVersion.media_type,
                ResearchDocumentVersion.raw_sha256,
                ResearchDocumentVersion.normalized_text_sha256,
                ResearchDocumentVersion.extraction_version,
                ResearchDocumentVersion.extraction_error,
                ResearchDocumentVersion.version_relation,
                func.octet_length(ResearchDocumentVersion.raw_body).label("body_bytes"),
            )
            .join(latest, latest.c.document_id == ResearchDocument.id)
            .join(
                ResearchDocumentVersion,
                (ResearchDocumentVersion.document_id == ResearchDocument.id)
                & (ResearchDocumentVersion.version_number == latest.c.version_number),
            )
            .order_by(
                ResearchDocument.source_key, ResearchDocumentVersion.title, ResearchDocument.id
            )
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        return self._rows(statement), total

    def get_version(self, document_id: UUID, version_id: UUID) -> Row | None:
        statement = (
            select(
                ResearchDocument.id.label("document_id"),
                ResearchDocument.source_key,
                ResearchDocument.canonical_url,
                ResearchDocument.created_at.label("document_created_at"),
                ResearchDocumentVersion.id.label("version_id"),
                ResearchDocumentVersion.version_number,
                ResearchDocumentVersion.previous_version_id,
                ResearchDocumentVersion.discovered_url,
                ResearchDocumentVersion.retrieved_url,
                ResearchDocumentVersion.title,
                ResearchDocumentVersion.publication_date,
                ResearchDocumentVersion.publication_date_state,
                ResearchDocumentVersion.data_period_start,
                ResearchDocumentVersion.data_period_end,
                ResearchDocumentVersion.data_period_state,
                ResearchDocumentVersion.retrieved_at,
                ResearchDocumentVersion.response_etag,
                ResearchDocumentVersion.response_last_modified,
                ResearchDocumentVersion.media_type,
                func.octet_length(ResearchDocumentVersion.raw_body).label("body_bytes"),
                ResearchDocumentVersion.raw_sha256,
                ResearchDocumentVersion.normalized_text,
                ResearchDocumentVersion.normalized_text_sha256,
                ResearchDocumentVersion.extraction_version,
                ResearchDocumentVersion.extraction_regions,
                ResearchDocumentVersion.extraction_error,
                ResearchDocumentVersion.version_relation,
            )
            .join(
                ResearchDocumentVersion, ResearchDocumentVersion.document_id == ResearchDocument.id
            )
            .where(
                ResearchDocument.id == document_id,
                ResearchDocumentVersion.id == version_id,
            )
        )
        return self._first(statement)

    def list_claims(
        self,
        *,
        question: ResearchQuestion | None,
        version_id: UUID | None,
        page: int,
        page_size: int,
    ) -> tuple[Sequence[Row], int]:
        base = self._claim_select()
        if question is not None:
            base = base.where(ResearchClaim.question == question.value)
        if version_id is not None:
            base = base.where(ResearchClaim.document_version_id == version_id)
        total = int(
            self._session.scalar(select(func.count()).select_from(base.order_by(None).subquery()))
            or 0
        )
        statement = (
            base.order_by(ResearchClaim.created_at.desc(), ResearchClaim.id)
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        return self._rows(statement), total

    def list_claim_summaries(self) -> Sequence[Row]:
        statement = (
            select(
                ResearchClaim.id.label("claim_id"),
                ResearchClaim.question,
                ResearchClaim.evidence_kind,
                ResearchClaim.lineage_kind,
                ResearchClaim.upstream_dataset_or_document,
                ResearchDocument.id.label("document_id"),
                ResearchDocumentVersion.normalized_text_sha256.label("claim_text_digest"),
            )
            .join(
                ResearchDocumentVersion,
                ResearchDocumentVersion.id == ResearchClaim.document_version_id,
            )
            .join(ResearchDocument, ResearchDocument.id == ResearchDocumentVersion.document_id)
            .order_by(ResearchClaim.created_at, ResearchClaim.id)
        )
        return self._rows(statement)

    def review_history(self, claim_ids: Sequence[UUID]) -> Mapping[UUID, Sequence[Row]]:
        if not claim_ids:
            return {}
        statement = (
            select(
                ResearchClaimReview.id,
                ResearchClaimReview.claim_id,
                ResearchClaimReview.event_sequence,
                ResearchClaimReview.state,
                ResearchClaimReview.reviewer,
                ResearchClaimReview.reason,
                ResearchClaimReview.target_version_id,
                ResearchClaimReview.reviewed_at,
            )
            .where(ResearchClaimReview.claim_id.in_(tuple(claim_ids)))
            .order_by(ResearchClaimReview.claim_id, ResearchClaimReview.event_sequence)
        )
        grouped: dict[UUID, list[Row]] = defaultdict(list)
        for row in self._rows(statement):
            grouped[_as_uuid(row.get("claim_id"))].append(row)
        return grouped

    def latest_text_digests(self, document_ids: Sequence[UUID]) -> Mapping[UUID, str | None]:
        if not document_ids:
            return {}
        latest = _latest_version_numbers()
        statement = (
            select(
                ResearchDocumentVersion.document_id,
                ResearchDocumentVersion.normalized_text_sha256,
            )
            .join(
                latest,
                (latest.c.document_id == ResearchDocumentVersion.document_id)
                & (latest.c.version_number == ResearchDocumentVersion.version_number),
            )
            .where(ResearchDocumentVersion.document_id.in_(tuple(document_ids)))
        )
        return {
            _as_uuid(row.get("document_id")): _str(row.get("normalized_text_sha256"))
            for row in self._rows(statement)
        }

    def document_counts_by_source(self) -> Sequence[Row]:
        latest = _latest_version_numbers()
        statement = (
            select(
                ResearchDocument.source_key,
                func.count(ResearchDocument.id).label("document_count"),
                func.count(ResearchDocumentVersion.id)
                .filter(ResearchDocumentVersion.normalized_text_sha256.is_not(None))
                .label("extractable_count"),
                func.count(ResearchDocumentVersion.id)
                .filter(ResearchDocumentVersion.extraction_error.is_not(None))
                .label("extraction_error_count"),
            )
            .join(latest, latest.c.document_id == ResearchDocument.id)
            .join(
                ResearchDocumentVersion,
                (ResearchDocumentVersion.document_id == ResearchDocument.id)
                & (ResearchDocumentVersion.version_number == latest.c.version_number),
            )
            .group_by(ResearchDocument.source_key)
            .order_by(ResearchDocument.source_key)
        )
        return self._rows(statement)

    def latest_registry_snapshot(self) -> Row | None:
        statement = (
            select(
                ResearchCollectionRun.id.label("run_id"),
                ResearchCollectionRun.registry_version,
                ResearchCollectionRun.registry_digest,
                ResearchCollectionRun.registry_snapshot,
                ResearchCollectionRun.trigger,
                ResearchCollectionRun.status.label("run_status"),
                ResearchCollectionRun.started_at,
                ResearchCollectionRun.finished_at,
            )
            .order_by(ResearchCollectionRun.started_at.desc(), ResearchCollectionRun.id.desc())
            .limit(1)
        )
        return self._first(statement)

    def source_attempt_statuses(self) -> Sequence[Row]:
        latest_attempt = select(
            ResearchAcquisitionAttempt.source_key,
            ResearchAcquisitionAttempt.id.label("attempt_id"),
            ResearchAcquisitionAttempt.run_id,
            ResearchAcquisitionAttempt.outcome,
            ResearchAcquisitionAttempt.occurred_at,
            ResearchAcquisitionAttempt.http_status,
            ResearchAcquisitionAttempt.bytes_received,
            ResearchAcquisitionAttempt.error_code,
            func.row_number()
            .over(
                partition_by=ResearchAcquisitionAttempt.source_key,
                order_by=(
                    ResearchAcquisitionAttempt.occurred_at.desc(),
                    ResearchAcquisitionAttempt.id.desc(),
                ),
            )
            .label("source_attempt_rank"),
        ).subquery()
        last_success = (
            select(
                ResearchAcquisitionAttempt.source_key,
                func.max(ResearchAcquisitionAttempt.occurred_at).label("last_success_at"),
            )
            .where(ResearchAcquisitionAttempt.outcome == "acquired")
            .group_by(ResearchAcquisitionAttempt.source_key)
            .subquery()
        )
        statement = (
            select(
                latest_attempt.c.source_key,
                latest_attempt.c.outcome,
                latest_attempt.c.occurred_at,
                latest_attempt.c.http_status,
                latest_attempt.c.bytes_received,
                latest_attempt.c.error_code,
                latest_attempt.c.run_id,
                ResearchCollectionRun.status.label("run_status"),
                ResearchCollectionRun.started_at.label("run_started_at"),
                ResearchCollectionRun.finished_at.label("run_finished_at"),
                last_success.c.last_success_at,
            )
            .join(ResearchCollectionRun, ResearchCollectionRun.id == latest_attempt.c.run_id)
            .outerjoin(last_success, last_success.c.source_key == latest_attempt.c.source_key)
            .where(latest_attempt.c.source_attempt_rank == 1)
            .order_by(latest_attempt.c.source_key)
        )
        return self._rows(statement)

    def acquisition_attempts(self, *, source_key: str, urls: Sequence[str]) -> Sequence[Row]:
        if not urls:
            return ()
        statement = (
            select(
                ResearchAcquisitionAttempt.id,
                ResearchAcquisitionAttempt.run_id,
                ResearchAcquisitionAttempt.requested_url,
                ResearchAcquisitionAttempt.outcome,
                ResearchAcquisitionAttempt.occurred_at,
                ResearchAcquisitionAttempt.http_status,
                ResearchAcquisitionAttempt.bytes_received,
                ResearchAcquisitionAttempt.error_code,
                ResearchCollectionRun.registry_version,
                ResearchCollectionRun.registry_digest,
                ResearchCollectionRun.trigger,
                ResearchCollectionRun.status.label("run_status"),
                ResearchCollectionRun.started_at.label("run_started_at"),
                ResearchCollectionRun.finished_at.label("run_finished_at"),
            )
            .join(
                ResearchCollectionRun, ResearchCollectionRun.id == ResearchAcquisitionAttempt.run_id
            )
            .where(
                ResearchAcquisitionAttempt.source_key == source_key,
                ResearchAcquisitionAttempt.requested_url.in_(tuple(urls)),
            )
            .order_by(ResearchAcquisitionAttempt.occurred_at, ResearchAcquisitionAttempt.id)
        )
        return self._rows(statement)

    def _claim_select(self) -> Select[Any]:
        return (
            select(
                ResearchClaim.id.label("claim_id"),
                ResearchClaim.document_version_id,
                ResearchClaim.question,
                ResearchClaim.statement,
                ResearchClaim.exact_quote,
                ResearchClaim.location,
                ResearchClaim.evidence_kind,
                ResearchClaim.evidence_details,
                ResearchClaim.lineage_kind,
                ResearchClaim.upstream_dataset_or_document,
                ResearchClaim.created_at.label("claim_created_at"),
                ResearchDocument.id.label("document_id"),
                ResearchDocument.source_key,
                ResearchDocument.canonical_url,
                ResearchDocumentVersion.id.label("version_id"),
                ResearchDocumentVersion.version_number,
                ResearchDocumentVersion.previous_version_id,
                ResearchDocumentVersion.title,
                ResearchDocumentVersion.publication_date,
                ResearchDocumentVersion.publication_date_state,
                ResearchDocumentVersion.data_period_start,
                ResearchDocumentVersion.data_period_end,
                ResearchDocumentVersion.data_period_state,
                ResearchDocumentVersion.discovered_url,
                ResearchDocumentVersion.retrieved_url,
                ResearchDocumentVersion.retrieved_at,
                ResearchDocumentVersion.response_etag,
                ResearchDocumentVersion.response_last_modified,
                ResearchDocumentVersion.media_type,
                func.octet_length(ResearchDocumentVersion.raw_body).label("body_bytes"),
                ResearchDocumentVersion.raw_sha256,
                ResearchDocumentVersion.normalized_text_sha256.label("claim_text_digest"),
                ResearchDocumentVersion.extraction_version,
                ResearchDocumentVersion.extraction_regions,
                ResearchDocumentVersion.extraction_error,
                ResearchDocumentVersion.version_relation,
            )
            .join(
                ResearchDocumentVersion,
                ResearchDocumentVersion.id == ResearchClaim.document_version_id,
            )
            .join(ResearchDocument, ResearchDocument.id == ResearchDocumentVersion.document_id)
        )

    def _rows(self, statement: Select[Any]) -> list[dict[str, object]]:
        result = self._session.execute(statement)
        return [dict(row) for row in result.mappings().all()]

    def _first(self, statement: Select[Any]) -> dict[str, object] | None:
        result = self._session.execute(statement).mappings().first()
        return None if result is None else dict(result)


def _latest_version_numbers() -> Subquery:
    return (
        select(
            ResearchDocumentVersion.document_id.label("document_id"),
            func.max(ResearchDocumentVersion.version_number).label("version_number"),
        )
        .group_by(ResearchDocumentVersion.document_id)
        .subquery()
    )


def _question_payload(
    context: ResearchEvidenceContext,
    rows: Sequence[Row],
    current_rows: Sequence[Row],
) -> dict[str, object]:
    by_lineage: dict[str, int] = defaultdict(int)
    primary_parent: dict[tuple[str, str], tuple[str, str]] = {}
    primary_group_nodes: set[tuple[str, str]] = set()
    primary_without_upstream = 0
    secondary_synthesis_primary_labels = 0

    def find(node: tuple[str, str]) -> tuple[str, str]:
        parent = primary_parent.setdefault(node, node)
        if parent != node:
            primary_parent[node] = find(parent)
        return primary_parent[node]

    def union(left: tuple[str, str], right: tuple[str, str]) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            primary_parent[right_root] = left_root

    for row in current_rows:
        lineage = _str(row.get("lineage_kind")) or "unknown"
        by_lineage[lineage] += 1
        if lineage == "primary":
            if _str(row.get("evidence_kind")) == "secondary_synthesis":
                secondary_synthesis_primary_labels += 1
                continue
            upstream = _str(row.get("upstream_dataset_or_document"))
            nodes: list[tuple[str, str]] = []
            document_id = _uuid_string(row.get("document_id"))
            if document_id is not None:
                nodes.append(("document", document_id))
            if upstream is None or not upstream.strip():
                primary_without_upstream += 1
            else:
                nodes.append(("upstream", " ".join(upstream.casefold().split())))
            if not nodes:
                # Missing both identifiers cannot establish independent lineage.
                nodes.append(("unidentified", ""))
            primary_group_nodes.update(nodes)
            for node in nodes[1:]:
                union(nodes[0], node)
    primary_groups = {find(node) for node in primary_group_nodes}
    methods = sorted(
        {method for row in current_rows if (method := _str(row.get("evidence_kind"))) is not None}
    )
    jobs = [
        {"term": term, "href": "/radar/#/explore?" + urlencode({"q": term})}
        for term in context.job_terms
    ]
    return {
        "id": context.key.value,
        "label": context.label,
        "question": context.prompt,
        "assessment": "insufficient",
        "assessment_note": (
            "No reviewed structured hypothesis synthesis is stored. Counts are descriptive; "
            "heterogeneous methods, populations, periods, and denominators are not combined."
        ),
        "reading_note": context.reading_note,
        "current_evidence_claim_count": len(current_rows),
        "all_claim_count": len(rows),
        "evidence_kinds": methods,
        "lineage": {
            "current_claims_by_kind": {
                key: by_lineage.get(key, 0)
                for key in ("primary", "replication", "derivative", "unknown")
            },
            "independent_primary_group_count": len(primary_groups),
            "primary_claims_without_named_upstream": primary_without_upstream,
            "secondary_synthesis_claims_mislabeled_primary": secondary_synthesis_primary_labels,
            "secondary_synthesis_is_independent": False,
            "note": (
                "Primary claims are grouped conservatively when they share a source document or "
                "an exact normalized upstream dataset/document name; this can merge claims that "
                "may rely on distinct datasets discussed in one publication. Secondary syntheses "
                "are excluded even if mislabeled primary; derivative claims and unknown lineage "
                "also do not count as independent primary groups."
            ),
        },
        "context": {
            "job_radar_searches": jobs,
            "skill_search_terms": list(context.skill_terms),
            "denominator_note": (
                "These are contextual search links and terms only. Job-posting mentions and "
                "research samples retain separate populations and denominators."
            ),
        },
    }


def _assess_current_evidence(
    *,
    claim_version_digest: str | None,
    latest_document_digest: str | None,
    reviews: Sequence[Row],
) -> tuple[bool, str, str | None]:
    ordered = sorted(reviews, key=lambda row: _int(row.get("event_sequence")))
    states = [(_str(row.get("state")) or "candidate") for row in ordered]
    latest_state = states[-1] if states else "candidate"
    terminal_state = next(
        (state for state in reversed(states) if state in _TERMINAL_REVIEW_STATES), None
    )
    if terminal_state is not None:
        return False, latest_state, terminal_state
    if latest_state == "revision_review_required":
        return False, latest_state, "revision_review_required"
    if latest_state != "accepted":
        return False, latest_state, latest_state
    if claim_version_digest is None or latest_document_digest is None:
        return False, latest_state, "normalized_text_unavailable"
    if claim_version_digest != latest_document_digest:
        return False, latest_state, "normalized_text_changed"
    return True, latest_state, None


def _claim_payload(
    row: Row, reviews: Sequence[Row], latest_digest: str | None
) -> dict[str, object]:
    current, review_state, exclusion_reason = _assess_current_evidence(
        claim_version_digest=_str(row.get("claim_text_digest")),
        latest_document_digest=latest_digest,
        reviews=reviews,
    )
    return {
        "id": _uuid_string(row.get("claim_id")),
        "document_version_id": _uuid_string(row.get("document_version_id")),
        "question": _str(row.get("question")),
        "statement": _str(row.get("statement")),
        "exact_quote": _str(row.get("exact_quote")),
        "location": _mapping_payload(row.get("location")),
        "evidence_kind": _str(row.get("evidence_kind")),
        "evidence_details": _mapping_payload(row.get("evidence_details")),
        "lineage_kind": _str(row.get("lineage_kind")),
        "upstream_dataset_or_document": _str(row.get("upstream_dataset_or_document")),
        "created_at": _datetime_string(row.get("claim_created_at")),
        "review_state": review_state,
        "current_evidence": current,
        "excluded_reason": exclusion_reason,
        "review_history": [_review_payload(item) for item in reviews],
        "document": {
            "id": _uuid_string(row.get("document_id")),
            "source_key": _str(row.get("source_key")),
            "canonical_url": _str(row.get("canonical_url")),
        },
        "version": _version_payload(row),
    }


def _document_payload(row: Row) -> dict[str, object]:
    return {
        "id": _uuid_string(row.get("document_id")),
        "source_key": _str(row.get("source_key")),
        "canonical_url": _str(row.get("canonical_url")),
        "created_at": _datetime_string(row.get("document_created_at")),
        "latest_version": _version_payload(row),
    }


def _version_payload(row: Row) -> dict[str, object]:
    return {
        "id": _uuid_string(row.get("version_id")),
        "version_number": _int(row.get("version_number")),
        "previous_version_id": _uuid_string(row.get("previous_version_id")),
        "title": _str(row.get("title")),
        "publication_date": {
            "value": _date_string(row.get("publication_date")),
            "state": _str(row.get("publication_date_state")),
        },
        "data_period": {
            "start": _date_string(row.get("data_period_start")),
            "end": _date_string(row.get("data_period_end")),
            "state": _str(row.get("data_period_state")),
        },
        "discovered_url": _str(row.get("discovered_url")),
        "retrieved_url": _str(row.get("retrieved_url")),
        "retrieved_at": _datetime_string(row.get("retrieved_at")),
        "response_etag": _str(row.get("response_etag")),
        "response_last_modified": _str(row.get("response_last_modified")),
        "media_type": _str(row.get("media_type")),
        "body_bytes": _int(row.get("body_bytes")),
        "raw_sha256": _str(row.get("raw_sha256")),
        "normalized_text_sha256": _str(row.get("normalized_text_sha256"))
        or _str(row.get("claim_text_digest")),
        "extraction_version": _str(row.get("extraction_version")),
        "extraction_error": _str(row.get("extraction_error")),
        "version_relation": _str(row.get("version_relation")),
        "extraction_regions": _regions_payload(row.get("extraction_regions")),
    }


def _review_payload(row: Row) -> dict[str, object]:
    return {
        "id": _uuid_string(row.get("id")),
        "event_sequence": _int(row.get("event_sequence")),
        "state": _str(row.get("state")),
        "reviewer": _str(row.get("reviewer")),
        "reason": _str(row.get("reason")),
        "target_version_id": _uuid_string(row.get("target_version_id")),
        "reviewed_at": _datetime_string(row.get("reviewed_at")),
    }


def _attempt_payload(row: Row) -> dict[str, object]:
    return {
        "id": _uuid_string(row.get("id")),
        "run_id": _uuid_string(row.get("run_id")),
        "requested_url": _str(row.get("requested_url")),
        "outcome": _str(row.get("outcome")),
        "occurred_at": _datetime_string(row.get("occurred_at")),
        "http_status": _int_or_none(row.get("http_status")),
        "bytes_received": _int(row.get("bytes_received")),
        "error_code": _str(row.get("error_code")),
        "run": {
            "registry_version": _str(row.get("registry_version")),
            "registry_digest": _str(row.get("registry_digest")),
            "trigger": _str(row.get("trigger")),
            "status": _str(row.get("run_status")),
            "started_at": _datetime_string(row.get("run_started_at")),
            "finished_at": _datetime_string(row.get("run_finished_at")),
        },
    }


def _regions_payload(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [_mapping_payload(item) for item in value if isinstance(item, Mapping)]


def _mapping_payload(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _enabled_sources_from_latest_registry(latest: Row | None) -> Sequence[Row]:
    if latest is None:
        return ()
    snapshot = _mapping_payload(latest.get("registry_snapshot"))
    sources = snapshot.get("sources")
    if not isinstance(sources, list):
        return ()
    return tuple(
        source
        for source in sources
        if isinstance(source, Mapping) and source.get("enabled") is True
    )


def _registry_summary(latest: Row | None) -> dict[str, object] | None:
    if latest is None:
        return None
    return {
        "version": _str(latest.get("registry_version")),
        "digest": _str(latest.get("registry_digest")),
        "trigger": _str(latest.get("trigger")),
        "run_status": _str(latest.get("run_status")),
        "run_id": _uuid_string(latest.get("run_id")),
        "started_at": _datetime_string(latest.get("started_at")),
        "finished_at": _datetime_string(latest.get("finished_at")),
    }


def _escape_for_html(value: str | None) -> str | None:
    if value is None:
        return None
    from html import escape

    return escape(value, quote=False)


def _str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _uuid_string(value: object) -> str | None:
    return str(value) if isinstance(value, UUID) else _str(value)


def _as_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _date_string(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return None


def _datetime_string(value: object) -> str | None:
    if not isinstance(value, datetime):
        return None
    return value.isoformat()
