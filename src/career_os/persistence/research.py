"""Transactional persistence boundary for research candidates and body versions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from career_os.config.research_sources import ResearchSourceRegistry
from career_os.domain.research import (
    ResearchClaimDraft,
    ResearchClaimReviewState,
    ResearchMediaType,
    ResearchOriginalVersion,
    ResearchTextLocation,
    ResearchTextRegion,
    ResearchVersionRelation,
    normalize_research_url,
    quote_matches_location,
)
from career_os.persistence.models import (
    ResearchCandidate,
    ResearchClaim,
    ResearchClaimReview,
    ResearchDocument,
    ResearchDocumentVersion,
)


class ResearchPersistenceError(ValueError):
    """Raised when an import conflicts with durable research evidence invariants."""


@dataclass(frozen=True, slots=True)
class ResearchVersionInsert:
    """Stored version row and whether this transaction appended a new body."""

    version: ResearchDocumentVersion
    inserted: bool


class ResearchRepository:
    """One session-scoped repository; callers own transaction commit and rollback."""

    def __init__(self, session: Session, registry: ResearchSourceRegistry) -> None:
        self.session = session
        self.registry = registry

    def upsert_candidate(
        self,
        *,
        source_key: str,
        discovered_url: str,
        canonical_url: str,
        title: str | None = None,
        seen_at: datetime | None = None,
    ) -> ResearchCandidate:
        """Idempotently record a source-allowlisted discovered URL."""
        source = self.registry.get(source_key)
        if not source.enabled:
            raise ResearchPersistenceError("disabled sources cannot create candidates")
        discovered = normalize_research_url(
            discovered_url, allowed_hosts=tuple(source.allowed_hosts)
        )
        canonical = normalize_research_url(canonical_url, allowed_hosts=tuple(source.allowed_hosts))
        occurred_at = _utc(seen_at or datetime.now(UTC))
        statement = (
            insert(ResearchCandidate)
            .values(
                source_key=source_key,
                canonical_url=canonical,
                discovered_url=discovered,
                title=title,
                state="discovered",
                first_seen_at=occurred_at,
                last_seen_at=occurred_at,
            )
            .on_conflict_do_update(
                constraint="uq_research_candidates_source_url",
                set_={
                    "discovered_url": discovered,
                    "title": func.coalesce(title, ResearchCandidate.title),
                    "last_seen_at": occurred_at,
                },
            )
            .returning(ResearchCandidate)
        )
        return self.session.execute(statement).scalar_one()

    def append_version(self, value: ResearchOriginalVersion) -> ResearchVersionInsert:
        """Append exact original bytes once and classify text-equivalent revisions safely."""
        source = self.registry.get(value.source_key)
        if not source.enabled:
            raise ResearchPersistenceError("disabled sources cannot acquire documents")
        allowed_hosts = tuple(source.allowed_hosts)
        canonical = normalize_research_url(value.canonical_url, allowed_hosts=allowed_hosts)
        discovered = normalize_research_url(value.discovered_url, allowed_hosts=allowed_hosts)
        retrieved = normalize_research_url(value.retrieved_url, allowed_hosts=allowed_hosts)
        if len(value.original_bytes) > source.max_response_bytes:
            raise ResearchPersistenceError("response exceeds the configured source byte limit")
        if value.media_type.value not in {"text/html", "application/pdf"}:
            raise ResearchPersistenceError("unsupported research media type")

        doc_insert = (
            insert(ResearchDocument)
            .values(source_key=value.source_key, canonical_url=canonical)
            .on_conflict_do_nothing(constraint="uq_research_documents_source_url")
        )
        self.session.execute(doc_insert)
        document = self.session.scalar(
            select(ResearchDocument)
            .where(
                ResearchDocument.source_key == value.source_key,
                ResearchDocument.canonical_url == canonical,
            )
            .with_for_update()
        )
        if document is None:
            raise ResearchPersistenceError(
                "canonical document insert was not visible in the transaction"
            )

        previous = self.session.scalar(
            select(ResearchDocumentVersion)
            .where(ResearchDocumentVersion.document_id == document.id)
            .order_by(ResearchDocumentVersion.version_number.desc())
            .limit(1)
        )
        if previous is not None and previous.raw_sha256 == value.raw_sha256:
            if previous.raw_body != value.original_bytes:
                raise ResearchPersistenceError("SHA-256 collision detected for stored body")
            if _same_observation(previous, value):
                return ResearchVersionInsert(previous, inserted=False)
        relation = _relation(previous, value.normalized_text_sha256)
        if value.relation is not relation:
            raise ResearchPersistenceError(
                f"version relation must be '{relation.value}' for the previous normalized text"
            )
        expected_previous = previous.id if previous is not None else None
        if value.previous_version_id != expected_previous:
            raise ResearchPersistenceError("version must link to the current latest body version")

        row = ResearchDocumentVersion(
            id=value.id,
            document_id=document.id,
            version_number=(previous.version_number + 1) if previous is not None else 1,
            previous_version_id=expected_previous,
            discovered_url=discovered,
            retrieved_url=retrieved,
            title=value.title,
            publication_date=value.publication_date.value,
            publication_date_state=value.publication_date.state.value,
            data_period_start=value.data_period.start,
            data_period_end=value.data_period.end,
            data_period_state=value.data_period.state.value,
            retrieved_at=value.retrieved_at,
            response_etag=value.response_etag,
            response_last_modified=value.response_last_modified,
            media_type=value.media_type.value,
            raw_body=value.original_bytes,
            raw_sha256=value.raw_sha256,
            normalized_text=value.normalized_text,
            normalized_text_sha256=value.normalized_text_sha256,
            extraction_version=value.extraction_version,
            extraction_regions=[_region_json(region) for region in value.extraction_regions],
            extraction_error=value.extraction_error,
            version_relation=relation.value,
        )
        self.session.add(row)
        self.session.flush()
        if relation is ResearchVersionRelation.REVISION:
            self._require_review_for_changed_claims(
                document.id, value.normalized_text_sha256, row.id
            )
        return ResearchVersionInsert(row, inserted=True)

    def insert_claim_candidate(self, draft: ResearchClaimDraft) -> ResearchClaim:
        """Persist a quote-bound annotation as a candidate, never as accepted evidence."""
        version = self.session.get(ResearchDocumentVersion, draft.document_version_id)
        if version is None:
            raise ResearchPersistenceError("claim references an unknown document version")
        self._validate_claim_location(draft.exact_quote, draft.location, version)
        details = _claim_details(draft)
        identity = {
            "document_version_id": str(draft.document_version_id),
            "question": draft.question.value,
            "statement": draft.statement,
            "exact_quote": draft.exact_quote,
            "location": _location_json(draft.location),
            "evidence_kind": draft.evidence_kind.value,
            "evidence_details": details,
            "lineage_kind": draft.lineage_kind.value,
            "upstream_dataset_or_document": draft.upstream_dataset_or_document,
        }
        key = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        statement = (
            insert(ResearchClaim)
            .values(
                idempotence_key=key,
                **(identity | {"document_version_id": draft.document_version_id}),
            )
            .on_conflict_do_nothing(constraint="uq_research_claims_idempotence_key")
            .returning(ResearchClaim)
        )
        claim = self.session.execute(statement).scalar_one_or_none()
        if claim is not None:
            self.session.add(
                ResearchClaimReview(
                    claim_id=claim.id,
                    state=ResearchClaimReviewState.CANDIDATE.value,
                    reviewer="system:annotation-import",
                    reason="Imported annotation is unreviewed and excluded from synthesis.",
                )
            )
            self.session.flush()
            return claim
        claim = self.session.scalar(
            select(ResearchClaim).where(ResearchClaim.idempotence_key == key)
        )
        if claim is None or claim.location != identity["location"]:
            raise ResearchPersistenceError("idempotent claim import did not resolve consistently")
        return claim

    def append_claim_review(
        self,
        *,
        claim_id: UUID,
        state: ResearchClaimReviewState,
        reviewer: str,
        reason: str,
    ) -> ResearchClaimReview:
        """Append a review after serializing against document version changes."""
        if not isinstance(state, ResearchClaimReviewState):
            raise TypeError("state must be a ResearchClaimReviewState")
        if not reviewer.strip() or not reason.strip():
            raise ValueError("reviewer and reason must be non-blank")

        # Use the same document→claim lock order as append_version. The initial
        # reads discover the immutable document relationship without locking a
        # claim first, which would deadlock against versioning's claim scan.
        version_id = self.session.scalar(
            select(ResearchClaim.document_version_id).where(ResearchClaim.id == claim_id)
        )
        if version_id is None:
            raise ResearchPersistenceError("cannot review an unknown claim")
        document_id = self.session.scalar(
            select(ResearchDocumentVersion.document_id).where(
                ResearchDocumentVersion.id == version_id
            )
        )
        if document_id is None:
            raise ResearchPersistenceError("claim document version is missing")
        locked_document_id = self.session.scalar(
            select(ResearchDocument.id).where(ResearchDocument.id == document_id).with_for_update()
        )
        if locked_document_id is None:
            raise ResearchPersistenceError("claim document is missing")
        claim = self.session.scalar(
            select(ResearchClaim).where(ResearchClaim.id == claim_id).with_for_update()
        )
        if claim is None:
            raise ResearchPersistenceError("cannot review an unknown claim")
        if claim.document_version_id != version_id:
            raise ResearchPersistenceError("claim document version changed during review")
        if state is ResearchClaimReviewState.ACCEPTED:
            version = self.session.get(ResearchDocumentVersion, claim.document_version_id)
            if version is None:
                raise ResearchPersistenceError("claim document version is missing")
            latest = self.session.scalar(
                select(ResearchDocumentVersion)
                .where(ResearchDocumentVersion.document_id == version.document_id)
                .order_by(ResearchDocumentVersion.version_number.desc())
                .limit(1)
            )
            if latest is None or latest.normalized_text_sha256 != version.normalized_text_sha256:
                raise ResearchPersistenceError(
                    "changed source text requires a new version-bound claim before acceptance"
                )
            self._validate_claim_location(claim.exact_quote, claim.location, version)
        review = ResearchClaimReview(
            claim_id=claim.id,
            state=state.value,
            reviewer=reviewer.strip(),
            reason=reason.strip(),
        )
        self.session.add(review)
        self.session.flush()
        return review

    def _validate_claim_location(
        self,
        quote: str,
        location_value: ResearchTextLocation | dict[str, object],
        version: ResearchDocumentVersion,
    ) -> None:
        if version.normalized_text is None:
            raise ResearchPersistenceError("claims require extracted normalized text")
        location = (
            location_value
            if isinstance(location_value, ResearchTextLocation)
            else _location_from_json(location_value)
        )
        if not quote_matches_location(version.normalized_text, quote, location):
            raise ResearchPersistenceError("exact quote does not match its normalized-text offsets")
        regions = tuple(_region_from_json(value) for value in version.extraction_regions)
        if not any(region.contains(location) for region in regions):
            raise ResearchPersistenceError(
                "claim locator is outside its actual extraction section/page"
            )

    def _require_review_for_changed_claims(
        self, document_id: object, text_digest: str | None, target_version_id: object
    ) -> None:
        """Append a review-required event for each currently accepted changed-text claim."""
        latest_review = select(
            ResearchClaimReview.claim_id.label("claim_id"),
            ResearchClaimReview.state.label("state"),
            func.row_number()
            .over(
                partition_by=ResearchClaimReview.claim_id,
                order_by=ResearchClaimReview.event_sequence.desc(),
            )
            .label("sequence"),
        ).subquery()
        accepted_claims = self.session.scalars(
            select(ResearchClaim)
            .join(
                ResearchDocumentVersion,
                ResearchClaim.document_version_id == ResearchDocumentVersion.id,
            )
            .join(latest_review, latest_review.c.claim_id == ResearchClaim.id)
            .where(
                ResearchDocumentVersion.document_id == document_id,
                ResearchDocumentVersion.normalized_text_sha256.is_distinct_from(text_digest),
                latest_review.c.sequence == 1,
                latest_review.c.state == "accepted",
            )
            .with_for_update(of=ResearchClaim)
        )
        for claim in accepted_claims:
            self.session.add(
                ResearchClaimReview(
                    claim_id=claim.id,
                    state="revision_review_required",
                    reviewer="system:versioning",
                    reason="Normalized source text changed; review the exact claim and location.",
                    target_version_id=target_version_id,
                )
            )
        self.session.flush()


def _relation(
    previous: ResearchDocumentVersion | None, normalized_digest: str | None
) -> ResearchVersionRelation:
    if previous is None:
        return ResearchVersionRelation.INITIAL
    if normalized_digest is not None and previous.normalized_text_sha256 == normalized_digest:
        return ResearchVersionRelation.EQUIVALENT_TEXT
    return ResearchVersionRelation.REVISION


def _same_observation(previous: ResearchDocumentVersion, value: ResearchOriginalVersion) -> bool:
    """Deduplicate content/extraction replays; transport route and validators stay telemetry."""
    return (
        previous.title == value.title
        and previous.publication_date == value.publication_date.value
        and previous.publication_date_state == value.publication_date.state.value
        and previous.data_period_start == value.data_period.start
        and previous.data_period_end == value.data_period.end
        and previous.data_period_state == value.data_period.state.value
        and previous.media_type == value.media_type.value
        and previous.normalized_text_sha256 == value.normalized_text_sha256
        and previous.extraction_version == value.extraction_version
        and previous.extraction_regions
        == [_region_json(region) for region in value.extraction_regions]
        and previous.extraction_error == value.extraction_error
    )


def _region_json(region: ResearchTextRegion) -> dict[str, object]:
    return {
        "media_type": region.media_type.value,
        "start_offset": region.start_offset,
        "end_offset": region.end_offset,
        "section": region.section,
        "page_number": region.page_number,
    }


def _location_json(location: ResearchTextLocation) -> dict[str, object]:
    return {
        "media_type": location.media_type.value,
        "start_offset": location.start_offset,
        "end_offset": location.end_offset,
        "section": location.section,
        "page_number": location.page_number,
    }


def _location_from_json(value: Mapping[str, object]) -> ResearchTextLocation:
    try:
        media_type = ResearchMediaType(str(value["media_type"]))
        start_offset = _json_integer(value, "start_offset")
        end_offset = _json_integer(value, "end_offset")
        section = value.get("section")
        page_number = value.get("page_number")
    except (KeyError, ValueError) as error:
        raise ResearchPersistenceError("claim locator metadata is malformed") from error
    if section is not None and not isinstance(section, str):
        raise ResearchPersistenceError("claim section locator must be text or null")
    if page_number is not None and (
        not isinstance(page_number, int) or isinstance(page_number, bool)
    ):
        raise ResearchPersistenceError("claim page locator must be an integer or null")
    return ResearchTextLocation(
        media_type=media_type,
        start_offset=start_offset,
        end_offset=end_offset,
        section=section,
        page_number=page_number,
    )


def _region_from_json(value: Mapping[str, object]) -> ResearchTextRegion:
    try:
        media_type = ResearchMediaType(str(value["media_type"]))
        start_offset = _json_integer(value, "start_offset")
        end_offset = _json_integer(value, "end_offset")
        section = value.get("section")
        page_number = value.get("page_number")
    except (KeyError, ValueError) as error:
        raise ResearchPersistenceError("stored extraction locator metadata is malformed") from error
    if section is not None and not isinstance(section, str):
        raise ResearchPersistenceError("stored extraction section must be text or null")
    if page_number is not None and (
        not isinstance(page_number, int) or isinstance(page_number, bool)
    ):
        raise ResearchPersistenceError("stored extraction page must be an integer or null")
    return ResearchTextRegion(
        media_type=media_type,
        start_offset=start_offset,
        end_offset=end_offset,
        section=section,
        page_number=page_number,
    )


def _json_integer(value: Mapping[str, object], key: str) -> int:
    raw = value[key]
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ResearchPersistenceError(f"stored locator {key} must be an integer")
    return raw


def _claim_details(draft: ResearchClaimDraft) -> dict[str, object]:
    return {
        "metric_definition": draft.metric_definition,
        "metric_value": draft.metric_value,
        "denominator": draft.denominator,
        "geography": draft.geography,
        "sample_description": draft.sample_description,
        "sample_size": draft.sample_size,
        "data_period": {
            "start": draft.data_period.start.isoformat() if draft.data_period.start else None,
            "end": draft.data_period.end.isoformat() if draft.data_period.end else None,
            "state": draft.data_period.state.value,
        },
        "limitations": draft.limitations,
        "missing_reasons": {
            name: reason.value for name, reason in sorted(draft.missing_reasons.items())
        },
    }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("research timestamps must be timezone-aware")
    return value.astimezone(UTC)
