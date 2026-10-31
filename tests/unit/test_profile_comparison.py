"""Coverage, gap ranking, representative selection, and edge-case tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from math import log1p
from uuid import UUID

import pytest

from career_os.application.profile_fit import (
    ProfileComparisonConsistencyError,
    ProfileComparisonService,
)
from career_os.config.profiles import Profile
from career_os.config.skills import SkillTaxonomy
from career_os.domain.markets import MarketAggregates, MarketBucket
from career_os.domain.profiles import GapThresholds, JobSkillCoverage, SkillGap

JOBS = tuple(UUID(int=value) for value in range(1, 11))


def test_comparison_exposes_covered_neutral_and_ranked_gap_evidence() -> None:
    market = _market(
        skills=(
            _bucket("python", JOBS[:8]),
            _bucket("kafka", JOBS[:6]),
            _bucket("postgresql", JOBS[:5]),
            _bucket("redis", JOBS[:1]),
        )
    )
    profile = _profile("python", "terraform")

    result = ProfileComparisonService(_taxonomy()).calculate(profile, market)

    assert result.matching_job_ids == JOBS
    assert result.skill_denominator_job_ids == JOBS
    assert tuple(item.value for item in result.covered_skills) == ("python",)
    assert result.neutral_profile_skill_ids == ("terraform",)
    assert result.demanded_skill_coverage == Decimal("0.25")
    assert tuple(gap.skill_id for gap in result.gaps) == ("kafka", "postgresql")
    kafka = result.gaps[0]
    assert kafka.job_ids == JOBS[:6]
    assert kafka.denominator_job_ids == JOBS
    assert kafka.job_count == 6
    assert kafka.job_share == Decimal("0.6")
    assert kafka.rank_score == pytest.approx(0.6 * log1p(6))


def test_job_skill_coverage_inverts_deduplicated_evidence_without_claiming_requirements() -> None:
    market = _market(
        skills=(
            _bucket("python", (JOBS[0], JOBS[1])),
            _bucket("kafka", (JOBS[0],)),
            _bucket("redis", (JOBS[1],)),
        ),
        matching=JOBS[:4],
        denominator=JOBS[:3],
    )
    result = ProfileComparisonService(
        _taxonomy(),
        gap_thresholds=GapThresholds(min_job_count=1, min_job_share=Decimal("0")),
    ).calculate(_profile("python"), market)

    first, second, no_mentions, no_description = result.job_skill_coverage
    assert first.mentioned_skill_ids == ("kafka", "python")
    assert first.covered_skill_ids == ("python",)
    assert first.gap_skill_ids == ("kafka",)
    assert first.skill_coverage == Decimal("0.5")
    assert second.mentioned_skill_ids == ("python", "redis")
    assert second.skill_coverage == Decimal("0.5")
    assert no_mentions.in_skill_denominator is True
    assert no_mentions.skill_coverage is None
    assert no_description.in_skill_denominator is False
    assert no_description.skill_coverage is None


def test_representative_jobs_use_meaningful_criteria_then_stable_uuid_tie_break() -> None:
    market = _market(
        skills=(
            _bucket("python", (JOBS[0], JOBS[1], JOBS[2])),
            _bucket("kafka", (JOBS[0], JOBS[2])),
            _bucket("redis", (JOBS[2],)),
        ),
        matching=JOBS[:4],
        denominator=JOBS[:4],
    )
    result = ProfileComparisonService(_taxonomy(), representative_job_limit=3).calculate(
        _profile("python", "kafka"), market
    )

    # Job 1 has 2/2, job 2 has 1/1, then job 3 has 2/3. Equal 100% jobs
    # are ordered by contributing covered-skill count before UUID.
    assert tuple(item.job_id for item in result.representative_jobs) == (
        JOBS[0],
        JOBS[1],
        JOBS[2],
    )
    assert all(item.skill_coverage is not None for item in result.representative_jobs)
    assert JOBS[3] not in {item.job_id for item in result.representative_jobs}

    tied = _market(
        skills=(_bucket("python", (JOBS[1], JOBS[0])),),
        matching=JOBS[:2],
        denominator=JOBS[:2],
    )
    tied_result = ProfileComparisonService(_taxonomy()).calculate(_profile("python"), tied)
    assert tuple(item.job_id for item in tied_result.representative_jobs) == JOBS[:2]


def test_empty_demand_and_empty_profile_have_explicit_undefined_or_zero_results() -> None:
    empty = ProfileComparisonService(_taxonomy()).calculate(_profile(), _market(skills=()))
    assert empty.demanded_skill_coverage is None
    assert empty.gaps == ()
    assert empty.representative_jobs == ()
    assert all(item.skill_coverage is None for item in empty.job_skill_coverage)

    demand = ProfileComparisonService(
        _taxonomy(),
        gap_thresholds=GapThresholds(min_job_count=1, min_job_share=Decimal("0.1")),
    ).calculate(_profile(), _market(skills=(_bucket("python", JOBS[:2]),)))
    assert demand.demanded_skill_coverage == Decimal(0)
    assert tuple(gap.skill_id for gap in demand.gaps) == ("python",)


def test_gap_thresholds_are_inclusive_and_ties_have_deterministic_skill_id_order() -> None:
    market = _market(
        skills=(
            _bucket("redis", JOBS[:5]),
            _bucket("kafka", JOBS[:5]),
            _bucket("python", JOBS[:4]),
        )
    )
    result = ProfileComparisonService(_taxonomy()).calculate(_profile(), market)

    assert tuple(gap.skill_id for gap in result.gaps) == ("kafka", "redis")


def test_comparison_fails_closed_for_taxonomy_mismatch_unknown_or_duplicate_market_skills() -> None:
    service = ProfileComparisonService(_taxonomy())
    with pytest.raises(ProfileComparisonConsistencyError, match="version"):
        service.calculate(_profile("python"), _market(skills=(), taxonomy_version="2.0.0"))
    with pytest.raises(ProfileComparisonConsistencyError, match="unknown taxonomy"):
        service.calculate(_profile("python"), _market(skills=(_bucket("unknown_skill", JOBS[:1]),)))
    with pytest.raises(ProfileComparisonConsistencyError, match="duplicate skill buckets"):
        duplicate = _bucket("python", JOBS[:1])
        service.calculate(_profile("python"), _market(skills=(duplicate, duplicate)))
    with pytest.raises(ProfileComparisonConsistencyError, match="unknown taxonomy IDs"):
        service.calculate(_profile("unknown_skill"), _market(skills=()))


def test_invalid_thresholds_and_representative_limit_are_rejected() -> None:
    with pytest.raises(ValueError, match="min_job_count"):
        GapThresholds(min_job_count=-1)
    with pytest.raises(ValueError, match="min_job_count"):
        GapThresholds(min_job_count=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="min_job_count"):
        GapThresholds(min_job_count=True)
    with pytest.raises(ValueError, match="min_job_share"):
        GapThresholds(min_job_share=Decimal("NaN"))
    with pytest.raises(ValueError, match="representative_job_limit"):
        ProfileComparisonService(_taxonomy(), representative_job_limit=-1)
    with pytest.raises(ValueError, match="representative_job_limit"):
        ProfileComparisonService(_taxonomy(), representative_job_limit=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="representative_job_limit"):
        ProfileComparisonService(_taxonomy(), representative_job_limit=True)


def test_gap_and_job_coverage_contracts_reject_invented_or_overlapping_evidence() -> None:
    demand = _bucket("python", JOBS[:5])
    with pytest.raises(ValueError, match=r"job_share \* log1p"):
        SkillGap(demand=demand, rank_score=1.0)
    with pytest.raises(ValueError, match="UUID"):
        JobSkillCoverage("not-a-uuid", True, (), (), ())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be a bool"):
        JobSkillCoverage(JOBS[0], 1, (), (), ())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="disjoint"):
        JobSkillCoverage(JOBS[0], True, ("python",), ("python",), ("python",))


def test_profile_comparison_contract_reconciles_cross_object_evidence() -> None:
    service = ProfileComparisonService(
        _taxonomy(),
        gap_thresholds=GapThresholds(min_job_count=1, min_job_share=Decimal("0")),
    )
    result = service.calculate(
        _profile("python"),
        _market(
            skills=(
                _bucket("python", JOBS[:5]),
                _bucket("kafka", JOBS[:2]),
            )
        ),
    )
    wrong_python = MarketBucket("python", JOBS[:4], JOBS)
    with pytest.raises(ValueError, match="preserve demanded skill evidence"):
        replace(result, covered_skills=(wrong_python,))
    with pytest.raises(ValueError, match="neutral profile skills"):
        replace(result, neutral_profile_skill_ids=("python",))
    with pytest.raises(ValueError, match="support thresholds"):
        replace(result, gap_thresholds=GapThresholds())

    with pytest.raises(ValueError, match="exactly cover matching_job_ids"):
        replace(
            result,
            job_skill_coverage=result.job_skill_coverage[:-1],
            representative_jobs=(),
        )
    first_coverage = result.job_skill_coverage[0]
    omitted_covered = replace(first_coverage, covered_skill_ids=())
    with pytest.raises(ValueError, match="reconcile to demanded"):
        replace(
            result,
            job_skill_coverage=(omitted_covered, *result.job_skill_coverage[1:]),
            representative_jobs=(),
        )
    wrong_denominator_flag = replace(result.job_skill_coverage[-1], in_skill_denominator=False)
    with pytest.raises(ValueError, match="reconcile to demanded"):
        replace(
            result,
            job_skill_coverage=(*result.job_skill_coverage[:-1], wrong_denominator_flag),
            representative_jobs=(),
        )

    with pytest.raises(ValueError, match="include every absent demanded skill"):
        replace(result, gaps=(), representative_jobs=())

    wrong_denominator_bucket = MarketBucket("python", JOBS[:5], JOBS[:5])
    with pytest.raises(ValueError, match="must use skill_denominator_job_ids"):
        replace(
            result,
            demanded_skills=(result.demanded_skills[0], wrong_denominator_bucket),
            covered_skills=(wrong_denominator_bucket,),
        )

    original = result.job_skill_coverage[0]
    invented = replace(original, covered_skill_ids=())
    with pytest.raises(ValueError, match="drawn from job_skill_coverage"):
        replace(result, representative_jobs=(invented,))
    unknown = JobSkillCoverage(JOBS[-1], True, (), (), ())
    with pytest.raises(ValueError, match="known skill coverage"):
        replace(
            result,
            job_skill_coverage=tuple(
                sorted((*result.job_skill_coverage[:-1], unknown), key=lambda x: x.job_id)
            ),
            representative_jobs=(unknown,),
        )


def test_profile_comparison_rejects_reordered_gap_and_representative_outputs() -> None:
    result = ProfileComparisonService(
        _taxonomy(),
        gap_thresholds=GapThresholds(min_job_count=1, min_job_share=Decimal("0")),
    ).calculate(
        _profile("python"),
        _market(
            skills=(
                _bucket("python", JOBS[:8]),
                _bucket("kafka", JOBS[:6]),
                _bucket("redis", JOBS[:5]),
            )
        ),
    )
    assert len(result.gaps) == 2
    assert len(result.representative_jobs) > 1

    with pytest.raises(ValueError, match="deterministic rank ordering"):
        replace(result, gaps=tuple(reversed(result.gaps)), representative_jobs=())
    with pytest.raises(ValueError, match="deterministic coverage ordering"):
        replace(result, representative_jobs=tuple(reversed(result.representative_jobs)))


def _profile(*skill_ids: str) -> Profile:
    return Profile.model_validate(
        {
            "name": "current",
            "target_role_families": ["backend_engineering"],
            "seniority": "senior",
            "skills": [{"id": skill_id, "level": "working"} for skill_id in skill_ids],
        }
    )


def _taxonomy() -> SkillTaxonomy:
    return SkillTaxonomy.model_validate(
        {
            "version": "1.0.0",
            "skills": [
                {"id": skill_id, "label": skill_id, "category": "database", "aliases": [skill_id]}
                for skill_id in ("python", "kafka", "postgresql", "redis", "terraform")
            ],
        }
    )


def _bucket(skill_id: str, job_ids: tuple[UUID, ...]) -> MarketBucket:
    return MarketBucket(skill_id, tuple(sorted(job_ids)), JOBS)


def _market(
    *,
    skills: tuple[MarketBucket, ...],
    matching: tuple[UUID, ...] = JOBS,
    denominator: tuple[UUID, ...] = JOBS,
    taxonomy_version: str = "1.0.0",
) -> MarketAggregates:
    if denominator != JOBS:
        skills = tuple(MarketBucket(item.value, item.job_ids, denominator) for item in skills)
    return MarketAggregates(
        market_name="backend",
        matching_job_ids=matching,
        skill_denominator_job_ids=denominator,
        compensation_job_ids=(),
        collection_cutoff=datetime(2026, 8, 21, tzinfo=UTC),
        taxonomy_version=taxonomy_version,
        normalizer_version="job-normalization-v3",
        companies=(),
        skills=skills,
        seniorities=(),
        workplace_types=(),
        countries=(),
        compensation=(),
    )
