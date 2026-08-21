"""Offline, per-job transactional orchestration for deterministic enrichment."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session, sessionmaker

from career_os.db import session_scope
from career_os.domain.normalization import (
    JobNormalization,
    JobNormalizationInput,
    NormalizedCompensation,
    NormalizedLocation,
    SourceCompensationEvidence,
    SourceLocationEvidence,
)
from career_os.normalization.jobs import JOB_NORMALIZER_VERSION, normalize_job
from career_os.normalization.skills import SkillExtractor
from career_os.persistence.models import JobCompensation, JobLocation, JobPosting, Source
from career_os.persistence.repositories import JobNotFoundError, JobSkillRepository

type SessionFactory = sessionmaker[Session]
_ASHBY_COMPONENT_TYPES: Final = {
    "Salary": "base_salary",
    "Bonus": "bonus",
    "EquityPercentage": "equity",
}
_ASHBY_INTERVALS: Final = {
    "1 YEAR": "year",
    "1 MONTH": "month",
    "1 HOUR": "hour",
    "1 DAY": "day",
    "NONE": "none",
}


class JobEnrichmentError(RuntimeError):
    """Stored source evidence cannot safely be replayed for enrichment."""


@dataclass(frozen=True, slots=True)
class EnrichedJobResult:
    job_id: UUID
    normalization_replaced: bool
    skills_replaced: bool


@dataclass(frozen=True, slots=True)
class BulkEnrichmentResult:
    selected: int
    enriched: int
    unchanged: int
    failed_ids: tuple[UUID, ...]


@dataclass(slots=True)
class JobEnrichmentService:
    """Select stale jobs and enrich each in an isolated database transaction."""

    session_factory: SessionFactory
    extractor: SkillExtractor

    def enrich_stale(self) -> BulkEnrichmentResult:
        """Process stale jobs in stable UUID order; a bad row cannot roll back peers."""
        with session_scope(self.session_factory) as session:
            job_ids = tuple(
                session.scalars(
                    select(JobPosting.id)
                    .where(
                        or_(
                            JobPosting.enrichment_normalizer_version != JOB_NORMALIZER_VERSION,
                            JobPosting.taxonomy_version != self.extractor.taxonomy_version,
                        )
                    )
                    .order_by(JobPosting.id)
                )
            )
        enriched = 0
        unchanged = 0
        failed: list[UUID] = []
        for job_id in job_ids:
            try:
                result = self.enrich_job(job_id=job_id)
            except Exception:  # A malformed retained payload must not poison the batch.
                failed.append(job_id)
                continue
            if result.normalization_replaced or result.skills_replaced:
                enriched += 1
            else:
                unchanged += 1
        return BulkEnrichmentResult(len(job_ids), enriched, unchanged, tuple(failed))

    def enrich_job(self, *, job_id: UUID) -> EnrichedJobResult:
        with session_scope(self.session_factory) as session:
            return self.enrich_job_in_session(session=session, job_id=job_id)

    def enrich_job_in_session(self, *, session: Session, job_id: UUID) -> EnrichedJobResult:
        """Apply normal fields, deterministic rows, skills, and both markers atomically."""
        row = session.execute(
            select(JobPosting, Source.kind)
            .join(Source, Source.id == JobPosting.source_id)
            .where(JobPosting.id == job_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise JobNotFoundError(f"canonical job not found: {job_id}")
        job, source_kind = row
        normalized = normalize_job(build_normalization_input(job=job, source_kind=source_kind))
        normalization_replaced = _replace_normalization(
            session=session, job=job, normalized=normalized
        )
        evidence = self.extractor.extract(title=job.title_raw, description=job.description_text)
        skills = JobSkillRepository(session).replace_deterministic_evidence(
            job_id=job.id, evidence=evidence, taxonomy_version=self.extractor.taxonomy_version
        )
        return EnrichedJobResult(job.id, normalization_replaced, skills.replaced)


def build_normalization_input(*, job: JobPosting, source_kind: str) -> JobNormalizationInput:
    """Build source-specific structured facts from retained ``raw_latest`` only."""
    raw = job.raw_latest
    if not isinstance(raw, dict):
        raise JobEnrichmentError("stored raw payload must be an object")
    if source_kind == "greenhouse":
        return _greenhouse_input(job, raw)
    if source_kind == "lever":
        return _lever_input(job, raw)
    if source_kind == "ashby":
        return _ashby_input(job, raw)
    if source_kind == "workable_public":
        return _workable_input(job, raw)
    raise JobEnrichmentError(f"unsupported stored source kind: {source_kind}")


def _base_input(job: JobPosting, **facts: object) -> JobNormalizationInput:
    return JobNormalizationInput(
        title=job.title_raw,
        description=job.description_text,
        **cast(dict[str, Any], facts),
    )


def _greenhouse_input(job: JobPosting, raw: dict[str, object]) -> JobNormalizationInput:
    location = raw.get("location")
    locations: tuple[SourceLocationEvidence, ...] = ()
    if location is not None:
        location_object = _object(location, "greenhouse.location")
        name = _optional_text(location_object.get("name"), "greenhouse.location.name")
        if name is not None:
            locations = (SourceLocationEvidence(raw=name, is_primary=True),)
    return _base_input(job, locations=locations)


def _lever_input(job: JobPosting, raw: dict[str, object]) -> JobNormalizationInput:
    categories = raw.get("categories")
    category = _object(categories, "lever.categories") if categories is not None else {}
    location = _optional_text(category.get("location"), "lever.categories.location")
    return _base_input(
        job,
        locations=(SourceLocationEvidence(raw=location, is_primary=True),) if location else (),
        source_employment_type=_optional_text(
            category.get("commitment"), "lever.categories.commitment"
        ),
        source_workplace_type=_optional_text(raw.get("workplaceType"), "lever.workplaceType"),
    )


def _ashby_input(job: JobPosting, raw: dict[str, object]) -> JobNormalizationInput:
    locations: list[SourceLocationEvidence] = []
    primary = _optional_text(raw.get("location"), "ashby.location")
    if primary is not None:
        locations.append(SourceLocationEvidence(raw=primary, is_primary=True))
    secondary = raw.get("secondaryLocations")
    if secondary is not None:
        if not isinstance(secondary, list):
            raise JobEnrichmentError("ashby.secondaryLocations must be a list")
        for index, value in enumerate(secondary):
            item = _object(value, f"ashby.secondaryLocations[{index}]")
            location = _required_text(
                item.get("location"), f"ashby.secondaryLocations[{index}].location"
            )
            address = item.get("address")
            address_object = (
                _object(address, f"ashby.secondaryLocations[{index}].address")
                if address is not None
                else {}
            )
            locations.append(
                SourceLocationEvidence(
                    raw=location,
                    city=_optional_text(
                        address_object.get("addressLocality"), "ashby.addressLocality"
                    ),
                    region=_optional_text(
                        address_object.get("addressRegion"), "ashby.addressRegion"
                    ),
                    country_code=_optional_text(
                        address_object.get("addressCountry"), "ashby.addressCountry"
                    ),
                )
            )
    compensation, compensation_text = _ashby_compensation(raw)
    return _base_input(
        job,
        locations=tuple(locations),
        source_employment_type=_optional_text(raw.get("employmentType"), "ashby.employmentType"),
        source_workplace_type=_optional_text(raw.get("workplaceType"), "ashby.workplaceType"),
        source_is_remote=_optional_bool(raw.get("isRemote"), "ashby.isRemote"),
        structured_compensation=compensation,
        compensation_text=compensation_text,
    )


def _workable_input(job: JobPosting, raw: dict[str, object]) -> JobNormalizationInput:
    city = _optional_text(raw.get("city"), "workable.city")
    region = _optional_text(raw.get("state"), "workable.state")
    country = _optional_text(raw.get("country"), "workable.country")
    raw_location = ", ".join(value for value in (city, region, country) if value)
    return _base_input(
        job,
        locations=(
            SourceLocationEvidence(raw=raw_location, city=city, region=region, is_primary=True),
        )
        if raw_location
        else (),
        source_employment_type=_optional_text(
            raw.get("employment_type"), "workable.employment_type"
        ),
        source_workplace_type=_optional_text(raw.get("workplace_type"), "workable.workplace_type"),
        source_is_remote=_optional_bool(raw.get("telecommuting"), "workable.telecommuting"),
    )


def _ashby_compensation(
    raw: dict[str, object],
) -> tuple[tuple[SourceCompensationEvidence, ...], str | None]:
    compensation = raw.get("compensation")
    if compensation is None:
        return (), None
    value = _object(compensation, "ashby.compensation")
    text = _optional_text(
        value.get("scrapeableCompensationSalarySummary"), "ashby.compensation.summary"
    )
    tiers = value.get("compensationTiers")
    if tiers is None:
        return (), text
    if not isinstance(tiers, list):
        raise JobEnrichmentError("ashby.compensation.compensationTiers must be a list")
    output: list[SourceCompensationEvidence] = []
    for tier_index, tier_value in enumerate(tiers):
        tier = _object(tier_value, f"ashby.compensation.compensationTiers[{tier_index}]")
        tier_name = _optional_text(tier.get("tierSummary"), "ashby.compensation.tierSummary")
        components = tier.get("components")
        if not isinstance(components, list):
            raise JobEnrichmentError("ashby compensation tier components must be a list")
        for component_index, component_value in enumerate(components):
            component = _object(component_value, f"ashby.component[{component_index}]")
            output.append(
                SourceCompensationEvidence(
                    component_type=_ashby_component_type(component.get("compensationType")),
                    min_value=_optional_decimal(
                        component.get("minValue"), "ashby.component.minValue"
                    ),
                    max_value=_optional_decimal(
                        component.get("maxValue"), "ashby.component.maxValue"
                    ),
                    currency=_optional_text(
                        component.get("currencyCode"), "ashby.component.currencyCode"
                    ),
                    interval=_ashby_interval(component.get("interval")),
                    location_tier=tier_name,
                    raw_text=_optional_text(component.get("summary"), "ashby.component.summary"),
                )
            )
    return tuple(output), text


def _replace_normalization(
    *, session: Session, job: JobPosting, normalized: JobNormalization
) -> bool:
    scalar_values = {
        "title_normalized": normalized.title_normalized,
        "role_family": normalized.role_family.value,
        "seniority": normalized.seniority.value,
        "seniority_confidence": normalized.seniority.confidence,
        "employment_type": normalized.employment_type.value,
        "workplace_type": normalized.workplace_type.value,
        "remote_scope": normalized.remote_scope.value,
    }
    scalar_changed = any(getattr(job, key) != value for key, value in scalar_values.items())
    locations_changed = _location_facts(job, session) != _normalized_location_facts(
        normalized.locations
    )
    compensation_changed = _compensation_facts(job, session) != _normalized_compensation_facts(
        normalized.compensation
    )
    marker_changed = job.enrichment_normalizer_version != normalized.normalizer_version
    if not (scalar_changed or locations_changed or compensation_changed or marker_changed):
        return False
    for key, value in scalar_values.items():
        setattr(job, key, value)
    if locations_changed:
        session.execute(delete(JobLocation).where(JobLocation.job_id == job.id))
        session.add_all(
            [
                JobLocation(
                    job_id=job.id,
                    ordinal=item.ordinal,
                    raw=item.raw,
                    city=item.city,
                    region=item.region,
                    country_code=item.country_code,
                    is_primary=item.is_primary,
                    confidence=item.confidence,
                )
                for item in normalized.locations
            ]
        )
    if compensation_changed:
        session.execute(delete(JobCompensation).where(JobCompensation.job_id == job.id))
        session.add_all(
            [
                JobCompensation(
                    job_id=job.id,
                    component_type=item.component_type,
                    min_value=item.min_value,
                    max_value=item.max_value,
                    currency=item.currency,
                    interval=item.interval,
                    location_tier=item.location_tier,
                    raw_text=item.raw_text,
                    source_method=item.source_method,
                    confidence=item.confidence,
                )
                for item in normalized.compensation
            ]
        )
    job.enrichment_normalizer_version = normalized.normalizer_version
    session.flush()
    return True


def _location_facts(job: JobPosting, session: Session) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            row.ordinal,
            row.raw,
            row.city,
            row.region,
            row.country_code,
            row.is_primary,
            row.confidence,
        )
        for row in session.scalars(
            select(JobLocation).where(JobLocation.job_id == job.id).order_by(JobLocation.ordinal)
        )
    )


def _normalized_location_facts(
    values: tuple[NormalizedLocation, ...],
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            item.ordinal,
            item.raw,
            item.city,
            item.region,
            item.country_code,
            item.is_primary,
            item.confidence,
        )
        for item in values
    )


def _compensation_facts(job: JobPosting, session: Session) -> tuple[tuple[object, ...], ...]:
    facts = tuple(
        (
            row.component_type,
            row.min_value,
            row.max_value,
            row.currency,
            row.interval,
            row.location_tier,
            row.raw_text,
            row.source_method,
            row.confidence,
        )
        for row in session.scalars(select(JobCompensation).where(JobCompensation.job_id == job.id))
    )
    return tuple(sorted(facts, key=repr))


def _normalized_compensation_facts(
    values: tuple[NormalizedCompensation, ...],
) -> tuple[tuple[object, ...], ...]:
    facts = tuple(
        (
            item.component_type,
            item.min_value,
            item.max_value,
            item.currency,
            item.interval,
            item.location_tier,
            item.raw_text,
            item.source_method,
            item.confidence,
        )
        for item in values
    )
    return tuple(sorted(facts, key=repr))


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise JobEnrichmentError(f"{name} must be an object")
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise JobEnrichmentError(f"{name} must be a non-blank string when present")
    return value.strip()


def _required_text(value: object, name: str) -> str:
    text = _optional_text(value, name)
    if text is None:
        raise JobEnrichmentError(f"{name} is required")
    return text


def _optional_bool(value: object, name: str) -> bool | None:
    if value is None:
        return None
    if type(value) is not bool:
        raise JobEnrichmentError(f"{name} must be a bool when present")
    return value


def _optional_decimal(value: object, name: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise JobEnrichmentError(f"{name} must be a JSON number when present")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise JobEnrichmentError(f"{name} must be a JSON number when present") from error
    if not result.is_finite():
        raise JobEnrichmentError(f"{name} must be finite")
    return result


def _ashby_component_type(value: object) -> str:
    source_value = _required_text(value, "ashby.component.compensationType")
    try:
        return _ASHBY_COMPONENT_TYPES[source_value]
    except KeyError as error:
        raise JobEnrichmentError("unsupported Ashby compensation component type") from error


def _ashby_interval(value: object) -> str | None:
    source_value = _optional_text(value, "ashby.component.interval")
    if source_value is None:
        return None
    try:
        return _ASHBY_INTERVALS[source_value]
    except KeyError as error:
        raise JobEnrichmentError("unsupported Ashby compensation interval") from error
