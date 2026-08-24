"""Deterministic eligibility-funnel and Markdown contract tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from career_os.application.eligibility_funnel import (
    EligibilityFunnelConsistencyError,
    EligibilityFunnelService,
)
from career_os.config.markets import MarketFilters
from career_os.persistence.models import JobPosting
from career_os.reporting.eligibility_funnel import render_eligibility_funnel


class _FakeSession:
    def __init__(
        self,
        jobs: tuple[tuple[JobPosting, str, str], ...],
        locations: tuple[tuple[UUID, str], ...],
        expected: tuple[UUID, ...],
    ) -> None:
        self._results: list[object] = [jobs, locations]
        self._expected = expected

    def execute(self, _query: object) -> object:
        return self._results.pop(0)

    def scalars(self, _query: object) -> tuple[UUID, ...]:
        return self._expected


def test_funnel_partitions_first_failures_and_reconciles_final_query() -> None:
    jobs = tuple(
        (
            _job(index, **spec),
            "alpha" if spec.get("company", "Alpha") == "Alpha" else "beta",
            "greenhouse",
        )
        for index, spec in enumerate(
            (
                {"status": "inactive"},
                {"title": "Data Engineer"},
                {"title": "Senior Frontend Backend Engineer"},
                {"role": "data_engineering"},
                {"seniority": "mid"},
                {"workplace": "onsite"},
                {"workplace": "remote", "scope": None},
                {"workplace": "remote", "scope": "Global"},
                {"company": "Beta"},
                {},
            ),
            start=1,
        )
    )
    local_ids = (UUID(int=2), UUID(int=9), UUID(int=10))
    session = _FakeSession(
        jobs,
        tuple((job_id, "PL") for job_id in local_ids),
        expected=(UUID(int=10),),
    )
    filters = MarketFilters(
        name="senior-backend-pl",
        title_include=("backend",),
        title_exclude=("frontend",),
        role_families=("backend_engineering",),
        seniorities=("senior",),
        country_codes=("PL",),
        include_remote=True,
        remote_scopes=("EU",),
        company_names=("Alpha",),
    )

    audit = EligibilityFunnelService(session).calculate(filters)  # type: ignore[arg-type]

    assert tuple(len(stage.job_ids) for stage in audit.stages) == (10, 9, 8, 7, 6, 5, 4, 2, 1, 1)
    assert audit.matching_job_ids == (UUID(int=10),)
    assert audit.title_include_only_exclusion_job_ids == (UUID(int=2),)
    assert {item.reason: item.job_ids for item in audit.exclusions} == {
        "inactive": (UUID(int=1),),
        "title_not_included": (UUID(int=2),),
        "title_excluded": (UUID(int=3),),
        "role_family_not_accepted": (UUID(int=4),),
        "seniority_not_accepted": (UUID(int=5),),
        "no_target_country_or_remote_evidence": (UUID(int=6),),
        "remote_scope_unknown": (UUID(int=7),),
        "remote_scope_not_accepted": (UUID(int=8),),
        "company_not_configured": (UUID(int=9),),
    }
    assert dict(audit.groups[0].counts)["final_match"] == 1
    assert dict(audit.groups[1].counts)["final_match"] == 0
    rendered = render_eligibility_funnel(audit)
    assert "Canonical cohort: **10** jobs" in rendered
    assert "reason buckets are mutually exclusive" in rendered
    assert "Zero-final-yield source/company rows to review or replace: **1**" in rendered
    assert str(UUID(int=10)) in rendered


def test_funnel_fails_closed_when_stages_diverge_from_market_query() -> None:
    job = _job(1)
    session = _FakeSession(((job, "alpha", "greenhouse"),), ((job.id, "PL"),), expected=())
    filters = MarketFilters(name="backend", title_include=("backend",))

    with pytest.raises(EligibilityFunnelConsistencyError, match="does not reconcile"):
        EligibilityFunnelService(session).calculate(filters)  # type: ignore[arg-type]


def _job(
    index: int,
    *,
    status: str = "active",
    title: str = "Senior Backend Engineer",
    role: str = "backend_engineering",
    seniority: str = "senior",
    workplace: str = "onsite",
    scope: str | None = None,
    company: str = "Alpha",
    **_extra: Any,
) -> JobPosting:
    observed = datetime(2026, 8, 23, 10, index, tzinfo=UTC)
    return JobPosting(
        id=UUID(int=index),
        external_id=str(index),
        company_name=company,
        title_raw=title,
        title_normalized=title.lower(),
        role_family=role,
        seniority=seniority,
        seniority_confidence=Decimal("1"),
        description_text="Python",
        workplace_type=workplace,
        remote_scope=scope,
        first_seen_at=observed,
        last_seen_at=observed,
        status=status,
        content_hash=str(index),
        raw_latest={},
        normalizer_version="source-v1",
        enrichment_normalizer_version="job-normalization-v3",
        taxonomy_version="1.0.2",
    )
