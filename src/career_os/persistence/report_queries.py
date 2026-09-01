"""Read-only canonical-job evidence used by reconciled reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career_os.config.markets import MarketFilters
from career_os.persistence.models import (
    JobCompensation,
    JobLocation,
    JobPosting,
    JobSkill,
    Source,
)


class ReportEvidenceConsistencyError(RuntimeError):
    """The requested report selection cannot be reconciled to stored jobs."""


@dataclass(frozen=True, slots=True)
class ReportJobEvidence:
    """The immutable stored-job projection used by the H1 result.

    The first four fields intentionally retain the old positional constructor so
    lower-level report tests and callers can continue to construct a minimal
    evidence row.  The bulk projection fills every field required by H1.
    """

    job_id: UUID
    company_name: str
    title: str
    source_url: str | None
    source_key: str = ""
    source_kind: str = ""
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    status: str = "active"
    role_family: str | None = None
    seniority: str | None = None
    workplace_type: str | None = None
    remote_scope: str | None = None
    description_text: str = ""
    locations: tuple[ReportLocationEvidence, ...] = ()
    skill_mentions: tuple[ReportSkillEvidence, ...] = ()
    compensation: tuple[ReportCompensationEvidence, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, UUID):
            raise ValueError("job_id must be a UUID")
        for value, field in ((self.company_name, "company_name"), (self.title, "title")):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be non-blank")
        if self.source_url is not None:
            if not isinstance(self.source_url, str):
                raise ValueError("source_url must be a string or None")
            try:
                parsed = urlsplit(self.source_url)
                hostname = parsed.hostname
                _ = parsed.port
            except ValueError as error:
                raise ValueError(
                    "source_url must be a safe absolute HTTP(S) URL or None"
                ) from error
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.netloc
                or hostname is None
                or parsed.username is not None
                or parsed.password is not None
                or "\\" in self.source_url
                or any(
                    character.isspace() or ord(character) < 32 or ord(character) == 127
                    for character in self.source_url
                )
            ):
                raise ValueError("source_url must be a safe absolute HTTP(S) URL or None")


@dataclass(frozen=True, slots=True)
class ReportSourceEvidence:
    """Persisted source configuration included in the configured panel."""

    key: str
    kind: str
    company_name: str
    enabled: bool

    def __post_init__(self) -> None:
        for value, field in (
            (self.key, "key"),
            (self.kind, "kind"),
            (self.company_name, "company_name"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be non-blank")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a bool")


@dataclass(frozen=True, slots=True)
class ReportLocationEvidence:
    ordinal: int
    city: str | None
    region: str | None
    country_code: str | None
    is_primary: bool
    confidence: Decimal


@dataclass(frozen=True, slots=True)
class ReportSkillEvidence:
    id: UUID
    skill_id: str
    matched_text: str
    section: str | None
    method: str
    confidence: Decimal
    taxonomy_version: str


@dataclass(frozen=True, slots=True)
class ReportCompensationEvidence:
    component_id: UUID
    component_type: str
    min_value: Decimal | None
    max_value: Decimal | None
    currency: str | None
    interval: str | None
    location_tier: str | None
    source_method: str


@dataclass(frozen=True, slots=True)
class ReportEvidenceSnapshot:
    """One bulk read of configured sources and the exact selected job evidence."""

    configured_sources: tuple[ReportSourceEvidence, ...]
    jobs: tuple[ReportJobEvidence, ...]

    def __post_init__(self) -> None:
        source_keys = tuple(source.key for source in self.configured_sources)
        if source_keys != tuple(sorted(set(source_keys))):
            raise ValueError("configured sources must be unique and sorted")
        job_ids = tuple(job.job_id for job in self.jobs)
        if job_ids != tuple(sorted(set(job_ids))):
            raise ValueError("evidence jobs must be unique and sorted")


@dataclass(slots=True)
class ReportEvidenceRepository:
    """Fetch a stable projection and fail closed if the selection changes."""

    session: Session

    def get_jobs(self, job_ids: tuple[UUID, ...]) -> tuple[ReportJobEvidence, ...]:
        if not job_ids:
            return ()
        rows = tuple(
            self.session.execute(
                select(
                    JobPosting.id,
                    JobPosting.company_name,
                    JobPosting.title_raw,
                    JobPosting.source_url,
                )
                .where(JobPosting.id.in_(job_ids))
                .order_by(JobPosting.id)
            )
        )
        evidence = tuple(
            ReportJobEvidence(
                job_id=job_id,
                company_name=company_name,
                title=title,
                source_url=source_url,
            )
            for job_id, company_name, title, source_url in rows
        )
        if tuple(item.job_id for item in evidence) != job_ids:
            raise ReportEvidenceConsistencyError(
                "market selection changed while report evidence was read"
            )
        return evidence

    def get_bulk(
        self,
        job_ids: tuple[UUID, ...],
        filters: MarketFilters,
        *,
        source_keys: tuple[str, ...] = (),
    ) -> ReportEvidenceSnapshot:
        """Load all H1 evidence with bounded, set-based queries.

        The caller owns the repeatable-read transaction.  This method performs
        no per-job queries and verifies that every selected row still exists.
        """
        configured_rows = tuple(
            self.session.execute(
                select(Source.key, Source.kind, Source.company_name, Source.enabled)
                .where(
                    Source.enabled.is_(True),
                    *(
                        [Source.key.in_(source_keys)]
                        if source_keys
                        else [func.lower(Source.company_name).in_(filters.company_names)]
                        if filters.company_names
                        else []
                    ),
                )
                .order_by(Source.key)
            )
        )
        configured_sources = tuple(
            ReportSourceEvidence(key=key, kind=kind, company_name=company_name, enabled=enabled)
            for key, kind, company_name, enabled in configured_rows
        )
        if not job_ids:
            return ReportEvidenceSnapshot(configured_sources=configured_sources, jobs=())

        selected_rows = tuple(
            self.session.execute(
                select(JobPosting, Source.key, Source.kind)
                .join(Source, Source.id == JobPosting.source_id)
                .where(JobPosting.id.in_(job_ids))
                .order_by(JobPosting.id)
            )
        )
        if tuple(row[0].id for row in selected_rows) != job_ids:
            raise ReportEvidenceConsistencyError(
                "market selection changed while report evidence was read"
            )

        location_rows = tuple(
            self.session.scalars(
                select(JobLocation)
                .where(JobLocation.job_id.in_(job_ids))
                .order_by(JobLocation.job_id, JobLocation.ordinal)
            )
        )
        skill_rows = tuple(
            self.session.scalars(
                select(JobSkill)
                .where(JobSkill.job_id.in_(job_ids))
                .order_by(JobSkill.job_id, JobSkill.id)
            )
        )
        compensation_rows = tuple(
            self.session.scalars(
                select(JobCompensation)
                .where(JobCompensation.job_id.in_(job_ids))
                .order_by(JobCompensation.job_id, JobCompensation.id)
            )
        )
        locations_by_job: dict[UUID, list[ReportLocationEvidence]] = {}
        for location_row in location_rows:
            locations_by_job.setdefault(location_row.job_id, []).append(
                ReportLocationEvidence(
                    ordinal=location_row.ordinal,
                    city=location_row.city,
                    region=location_row.region,
                    country_code=location_row.country_code,
                    is_primary=location_row.is_primary,
                    confidence=location_row.confidence,
                )
            )
        skills_by_job: dict[UUID, list[ReportSkillEvidence]] = {}
        for skill_row in skill_rows:
            skills_by_job.setdefault(skill_row.job_id, []).append(
                ReportSkillEvidence(
                    id=skill_row.id,
                    skill_id=skill_row.skill_id,
                    matched_text=skill_row.matched_text,
                    section=skill_row.section,
                    method=skill_row.method,
                    confidence=skill_row.confidence,
                    taxonomy_version=skill_row.taxonomy_version,
                )
            )
        compensation_by_job: dict[UUID, list[ReportCompensationEvidence]] = {}
        for compensation_row in compensation_rows:
            compensation_by_job.setdefault(compensation_row.job_id, []).append(
                ReportCompensationEvidence(
                    component_id=compensation_row.id,
                    component_type=compensation_row.component_type,
                    min_value=compensation_row.min_value,
                    max_value=compensation_row.max_value,
                    currency=compensation_row.currency,
                    interval=compensation_row.interval,
                    location_tier=compensation_row.location_tier,
                    source_method=compensation_row.source_method,
                )
            )
        jobs = tuple(
            ReportJobEvidence(
                job_id=job.id,
                company_name=job.company_name,
                title=job.title_raw,
                source_url=job.source_url,
                source_key=source_key,
                source_kind=source_kind,
                first_seen_at=job.first_seen_at,
                last_seen_at=job.last_seen_at,
                status=job.status,
                role_family=job.role_family,
                seniority=job.seniority,
                workplace_type=job.workplace_type,
                remote_scope=job.remote_scope,
                description_text=job.description_text,
                locations=tuple(locations_by_job.get(job.id, ())),
                skill_mentions=tuple(skills_by_job.get(job.id, ())),
                compensation=tuple(compensation_by_job.get(job.id, ())),
            )
            for job, source_key, source_kind in selected_rows
        )
        return ReportEvidenceSnapshot(configured_sources=configured_sources, jobs=jobs)
