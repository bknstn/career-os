"""PostgreSQL query construction for deterministic market selection."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Select, and_, exists, func, or_, select

from career_os.config.markets import MarketFilters
from career_os.persistence.models import JobLocation, JobPosting, Source


def build_market_job_ids_query(
    filters: MarketFilters, *, source_keys: tuple[str, ...] = ()
) -> Select[tuple[UUID]]:
    """Select stable job IDs without joins that could multiply canonical jobs.

    Title inclusion is ANY requested literal substring; exclusion is NONE.
    Country matching uses normalized location rows.  Eligible remote jobs are
    added to a country market only by exact, case-insensitive scope equality.
    ``source_keys`` is an internal, exact source-identity restriction used by
    H2 Target Universe; it is deliberately not part of ``MarketFilters``.
    """
    predicates = []
    if filters.active_only:
        predicates.append(JobPosting.status == "active")
    folded_title = func.lower(JobPosting.title_normalized)
    if filters.title_include:
        predicates.append(
            or_(*(func.strpos(folded_title, value) > 0 for value in filters.title_include))
        )
    if filters.title_exclude:
        predicates.extend(func.strpos(folded_title, value) == 0 for value in filters.title_exclude)
    if filters.role_families:
        predicates.append(JobPosting.role_family.in_(filters.role_families))
    if filters.seniorities:
        predicates.append(JobPosting.seniority.in_(filters.seniorities))
    if filters.company_names:
        predicates.append(func.lower(JobPosting.company_name).in_(filters.company_names))
    if source_keys:
        predicates.append(
            JobPosting.source_id.in_(select(Source.id).where(Source.key.in_(source_keys)))
        )
    if filters.country_codes:
        local_match = exists(
            select(1).where(
                and_(
                    JobLocation.job_id == JobPosting.id,
                    JobLocation.country_code.in_(filters.country_codes),
                )
            )
        )
        if filters.include_remote:
            remote_match = and_(
                JobPosting.workplace_type == "remote",
                func.lower(func.btrim(JobPosting.remote_scope)).in_(filters.remote_scopes),
            )
            predicates.append(or_(local_match, remote_match))
        else:
            predicates.append(local_match)
    return select(JobPosting.id).where(*predicates).order_by(JobPosting.id)
