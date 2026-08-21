"""Immutable market aggregate contracts with explicit evidence denominators."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID


def _nonblank(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-blank")


def _ids(value: tuple[UUID, ...], field: str) -> None:
    if value != tuple(sorted(set(value))):
        raise ValueError(f"{field} must contain unique UUIDs in stable order")


def _share(job_ids: tuple[UUID, ...], denominator_job_ids: tuple[UUID, ...]) -> Decimal:
    return (
        Decimal(len(job_ids)) / Decimal(len(denominator_job_ids))
        if denominator_job_ids
        else Decimal(0)
    )


@dataclass(frozen=True, slots=True)
class MarketBucket:
    """One categorical or skill count reconciled to an exact denominator."""

    value: str
    job_ids: tuple[UUID, ...]
    denominator_job_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        _nonblank(self.value, "value")
        _ids(self.job_ids, "job_ids")
        _ids(self.denominator_job_ids, "denominator_job_ids")
        if not set(self.job_ids) <= set(self.denominator_job_ids):
            raise ValueError("job_ids must be a subset of denominator_job_ids")

    @property
    def job_count(self) -> int:
        return len(self.job_ids)

    @property
    def denominator(self) -> int:
        return len(self.denominator_job_ids)

    @property
    def job_share(self) -> Decimal:
        return _share(self.job_ids, self.denominator_job_ids)


@dataclass(frozen=True, slots=True)
class CompensationAggregate:
    """An unconverted component group; tiers and component types never compose."""

    component_type: str
    currency: str
    interval: str
    location_tier: str | None
    component_ids: tuple[UUID, ...]
    job_ids: tuple[UUID, ...]
    denominator_job_ids: tuple[UUID, ...]
    min_value: Decimal | None
    max_value: Decimal | None

    def __post_init__(self) -> None:
        for field in ("component_type", "currency", "interval"):
            _nonblank(getattr(self, field), field)
        if self.location_tier is not None:
            _nonblank(self.location_tier, "location_tier")
        _ids(self.component_ids, "component_ids")
        _ids(self.job_ids, "job_ids")
        _ids(self.denominator_job_ids, "denominator_job_ids")
        if not self.component_ids or not self.job_ids:
            raise ValueError("a compensation group requires component_ids and job_ids")
        if not set(self.job_ids) <= set(self.denominator_job_ids):
            raise ValueError("job_ids must be a subset of denominator_job_ids")
        if self.min_value is None and self.max_value is None:
            raise ValueError("a numerical compensation group requires an amount")
        for field in ("min_value", "max_value"):
            value = getattr(self, field)
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite() or value < 0
            ):
                raise ValueError(f"{field} must be a finite non-negative Decimal or None")
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError("min_value must not exceed max_value")

    @property
    def component_count(self) -> int:
        return len(self.component_ids)

    @property
    def job_count(self) -> int:
        return len(self.job_ids)

    @property
    def denominator(self) -> int:
        return len(self.denominator_job_ids)

    @property
    def job_share(self) -> Decimal:
        return _share(self.job_ids, self.denominator_job_ids)


@dataclass(frozen=True, slots=True)
class MarketAggregates:
    """A reproducible aggregate snapshot and all reconciliation evidence."""

    market_name: str
    matching_job_ids: tuple[UUID, ...]
    skill_denominator_job_ids: tuple[UUID, ...]
    compensation_job_ids: tuple[UUID, ...]
    collection_cutoff: datetime | None
    taxonomy_version: str
    normalizer_version: str
    companies: tuple[MarketBucket, ...]
    skills: tuple[MarketBucket, ...]
    seniorities: tuple[MarketBucket, ...]
    workplace_types: tuple[MarketBucket, ...]
    countries: tuple[MarketBucket, ...]
    compensation: tuple[CompensationAggregate, ...]

    def __post_init__(self) -> None:
        for field in ("market_name", "taxonomy_version", "normalizer_version"):
            _nonblank(getattr(self, field), field)
        if self.collection_cutoff is not None and (
            not isinstance(self.collection_cutoff, datetime)
            or self.collection_cutoff.tzinfo is None
            or self.collection_cutoff.utcoffset() is None
        ):
            raise ValueError("collection_cutoff must be timezone-aware or None")
        for field in ("matching_job_ids", "skill_denominator_job_ids", "compensation_job_ids"):
            _ids(getattr(self, field), field)
        matching = set(self.matching_job_ids)
        if not set(self.skill_denominator_job_ids) <= matching:
            raise ValueError("skill denominator must be a subset of matching jobs")
        if not set(self.compensation_job_ids) <= matching:
            raise ValueError("compensation jobs must be a subset of matching jobs")
        for field in ("companies", "seniorities", "workplace_types"):
            if any(
                item.denominator_job_ids != self.matching_job_ids for item in getattr(self, field)
            ):
                raise ValueError(f"{field} must use matching_job_ids as their denominator")
        if any(item.denominator_job_ids != self.skill_denominator_job_ids for item in self.skills):
            raise ValueError("skills must use skill_denominator_job_ids as their denominator")
        country_denominators = {item.denominator_job_ids for item in self.countries}
        if len(country_denominators) > 1 or any(
            not set(denominator) <= matching for denominator in country_denominators
        ):
            raise ValueError("countries must share one denominator contained in matching jobs")
        if any(item.denominator_job_ids != self.matching_job_ids for item in self.compensation):
            raise ValueError("compensation groups must use matching_job_ids as their denominator")
        grouped_compensation_jobs = {
            job_id for item in self.compensation for job_id in item.job_ids
        }
        if grouped_compensation_jobs != set(self.compensation_job_ids):
            raise ValueError("compensation_job_ids must reconcile to compensation groups")

    @property
    def matching_job_count(self) -> int:
        return len(self.matching_job_ids)

    @property
    def company_count(self) -> int:
        return len(self.companies)

    @property
    def compensation_coverage(self) -> Decimal:
        return _share(self.compensation_job_ids, self.matching_job_ids)
