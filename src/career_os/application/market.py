"""Read-only market aggregation over one exact canonical-job selection."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from career_os.config.markets import MarketFilters
from career_os.domain.markets import CompensationAggregate, MarketAggregates, MarketBucket
from career_os.persistence.market_queries import build_market_job_ids_query
from career_os.persistence.models import JobCompensation, JobLocation, JobPosting, JobSkill


class MarketDataConsistencyError(RuntimeError):
    """Matching jobs are not all enriched with the requested rule versions."""


@dataclass(slots=True)
class MarketAggregateService:
    """Compute deterministic aggregates without mutating caller-owned state."""

    session: Session
    taxonomy_version: str
    normalizer_version: str

    def __post_init__(self) -> None:
        for field in ("taxonomy_version", "normalizer_version"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be non-blank")

    def calculate(
        self, filters: MarketFilters, *, source_keys: tuple[str, ...] = ()
    ) -> MarketAggregates:
        """Calculate one selection, optionally restricted to exact source identity."""
        matching_ids = tuple(
            self.session.scalars(build_market_job_ids_query(filters, source_keys=source_keys))
        )
        if not matching_ids:
            return MarketAggregates(
                market_name=filters.name,
                matching_job_ids=(),
                skill_denominator_job_ids=(),
                compensation_job_ids=(),
                collection_cutoff=None,
                taxonomy_version=self.taxonomy_version,
                normalizer_version=self.normalizer_version,
                companies=(),
                skills=(),
                seniorities=(),
                workplace_types=(),
                countries=(),
                compensation=(),
            )

        jobs = tuple(
            self.session.scalars(
                select(JobPosting).where(JobPosting.id.in_(matching_ids)).order_by(JobPosting.id)
            )
        )
        _assert_same_selection(matching_ids, tuple(job.id for job in jobs))
        self._assert_current(jobs)
        matching = tuple(job.id for job in jobs)
        description_ids = tuple(job.id for job in jobs if job.description_text.strip())

        companies = _buckets(((job.company_name, job.id) for job in jobs), denominator=matching)
        seniorities = _buckets(
            ((job.seniority or "unknown", job.id) for job in jobs), denominator=matching
        )
        workplace_types = _buckets(
            ((job.workplace_type or "unknown", job.id) for job in jobs), denominator=matching
        )
        countries = self._countries(matching)
        skills = self._skills(description_ids)
        compensation, compensation_ids = self._compensation(matching)
        return MarketAggregates(
            market_name=filters.name,
            matching_job_ids=matching,
            skill_denominator_job_ids=description_ids,
            compensation_job_ids=compensation_ids,
            collection_cutoff=max(job.last_seen_at for job in jobs),
            taxonomy_version=self.taxonomy_version,
            normalizer_version=self.normalizer_version,
            companies=companies,
            skills=skills,
            seniorities=seniorities,
            workplace_types=workplace_types,
            countries=countries,
            compensation=compensation,
        )

    def _assert_current(self, jobs: tuple[JobPosting, ...]) -> None:
        stale = tuple(
            job.id
            for job in jobs
            if job.taxonomy_version != self.taxonomy_version
            or job.enrichment_normalizer_version != self.normalizer_version
        )
        if stale:
            joined = ", ".join(str(job_id) for job_id in stale)
            raise MarketDataConsistencyError(
                "matching jobs require enrichment with the requested taxonomy and normalizer "
                f"versions: {joined}"
            )

    def _skills(self, denominator: tuple[UUID, ...]) -> tuple[MarketBucket, ...]:
        if not denominator:
            return ()
        rows = self.session.execute(
            select(JobSkill.job_id, JobSkill.skill_id)
            .where(JobSkill.job_id.in_(denominator))
            .distinct()
            .order_by(JobSkill.skill_id, JobSkill.job_id)
        )
        return _buckets(((skill_id, job_id) for job_id, skill_id in rows), denominator=denominator)

    def _countries(self, matching: tuple[UUID, ...]) -> tuple[MarketBucket, ...]:
        rows = tuple(
            self.session.execute(
                select(JobLocation.country_code, JobLocation.job_id)
                .where(
                    JobLocation.job_id.in_(matching),
                    JobLocation.country_code.is_not(None),
                )
                .distinct()
                .order_by(JobLocation.country_code, JobLocation.job_id)
            )
        )
        denominator = tuple(sorted({job_id for _, job_id in rows}))
        return _buckets(((country, job_id) for country, job_id in rows), denominator=denominator)

    def _compensation(
        self, matching: tuple[UUID, ...]
    ) -> tuple[tuple[CompensationAggregate, ...], tuple[UUID, ...]]:
        rows = tuple(
            self.session.scalars(
                select(JobCompensation)
                .where(
                    JobCompensation.job_id.in_(matching),
                    JobCompensation.currency.is_not(None),
                    (JobCompensation.min_value.is_not(None))
                    | (JobCompensation.max_value.is_not(None)),
                )
                .order_by(
                    JobCompensation.component_type,
                    JobCompensation.currency,
                    JobCompensation.interval.nulls_first(),
                    JobCompensation.location_tier.nulls_first(),
                    JobCompensation.id,
                )
            )
        )
        grouped: dict[tuple[str, str, str, str | None, UUID | None], list[JobCompensation]] = (
            defaultdict(list)
        )
        for row in rows:
            assert row.currency is not None
            interval = row.interval or "unknown"
            grouped[
                (
                    row.component_type,
                    row.currency,
                    interval,
                    row.location_tier,
                    row.job_id if interval == "unknown" else None,
                )
            ].append(row)
        output: list[CompensationAggregate] = []
        for key in sorted(
            grouped,
            key=lambda item: (item[0], item[1], item[2], item[3] or "", str(item[4] or "")),
        ):
            group = grouped[key]
            minimums = [row.min_value for row in group if row.min_value is not None]
            maximums = [row.max_value for row in group if row.max_value is not None]
            output.append(
                CompensationAggregate(
                    component_type=key[0],
                    currency=key[1],
                    interval=key[2],
                    location_tier=key[3],
                    component_ids=tuple(sorted(row.id for row in group)),
                    job_ids=tuple(sorted({row.job_id for row in group})),
                    denominator_job_ids=matching,
                    min_value=min(minimums) if minimums else None,
                    max_value=max(maximums) if maximums else None,
                )
            )
        return tuple(output), tuple(sorted({row.job_id for row in rows}))


def _buckets(
    rows: Iterable[tuple[str | None, UUID]], *, denominator: tuple[UUID, ...]
) -> tuple[MarketBucket, ...]:
    grouped: dict[str, set[UUID]] = defaultdict(set)
    for value, job_id in rows:
        grouped[str(value)].add(job_id)
    return tuple(
        MarketBucket(
            value=value,
            job_ids=tuple(sorted(grouped[value])),
            denominator_job_ids=denominator,
        )
        for value in sorted(grouped)
    )


def _assert_same_selection(initial: tuple[UUID, ...], fetched: tuple[UUID, ...]) -> None:
    if fetched != initial:
        raise MarketDataConsistencyError("market selection changed while aggregates were read")
