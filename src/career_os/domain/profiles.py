"""Immutable, evidence-reconciled profile comparison contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from math import isfinite, log1p
from uuid import UUID

from career_os.domain.markets import MarketBucket

_SKILL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def _skill_ids(values: tuple[str, ...], field: str) -> None:
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{field} must contain unique skill IDs in stable order")
    if any(
        not isinstance(value, str) or not _SKILL_ID_PATTERN.fullmatch(value) for value in values
    ):
        raise ValueError(f"{field} must contain lowercase snake_case skill IDs")


def _job_ids(values: tuple[UUID, ...], field: str) -> None:
    if values != tuple(sorted(set(values))) or any(not isinstance(value, UUID) for value in values):
        raise ValueError(f"{field} must contain unique UUIDs in stable order")


@dataclass(frozen=True, slots=True)
class GapThresholds:
    """Explicit support required before an absent demanded skill is a gap."""

    min_job_count: int = 5
    min_job_share: Decimal = Decimal("0.10")

    def __post_init__(self) -> None:
        if type(self.min_job_count) is not int or self.min_job_count < 0:
            raise ValueError("min_job_count must be a non-negative integer")
        if (
            not isinstance(self.min_job_share, Decimal)
            or not self.min_job_share.is_finite()
            or not Decimal(0) <= self.min_job_share <= Decimal(1)
        ):
            raise ValueError("min_job_share must be a finite Decimal between zero and one")


@dataclass(frozen=True, slots=True)
class SkillGap:
    """A supported absent skill with its exact rank inputs and evidence jobs."""

    demand: MarketBucket
    rank_score: float

    def __post_init__(self) -> None:
        expected = float(self.demand.job_share) * log1p(self.demand.job_count)
        if (
            type(self.rank_score) is not float
            or not isfinite(self.rank_score)
            or self.rank_score != expected
        ):
            raise ValueError("rank_score must equal job_share * log1p(job_count)")

    @property
    def skill_id(self) -> str:
        return self.demand.value

    @property
    def job_ids(self) -> tuple[UUID, ...]:
        return self.demand.job_ids

    @property
    def denominator_job_ids(self) -> tuple[UUID, ...]:
        return self.demand.denominator_job_ids

    @property
    def job_count(self) -> int:
        return self.demand.job_count

    @property
    def job_share(self) -> Decimal:
        return self.demand.job_share


@dataclass(frozen=True, slots=True)
class JobSkillCoverage:
    """Mention-based skill coverage for one reconciled job, never a fit probability."""

    job_id: UUID
    in_skill_denominator: bool
    mentioned_skill_ids: tuple[str, ...]
    covered_skill_ids: tuple[str, ...]
    gap_skill_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, UUID):
            raise ValueError("job_id must be a UUID")
        if not isinstance(self.in_skill_denominator, bool):
            raise ValueError("in_skill_denominator must be a bool")
        for field in ("mentioned_skill_ids", "covered_skill_ids", "gap_skill_ids"):
            _skill_ids(getattr(self, field), field)
        mentioned = set(self.mentioned_skill_ids)
        if not set(self.covered_skill_ids) <= mentioned:
            raise ValueError("covered_skill_ids must be mentioned by the job")
        if not set(self.gap_skill_ids) <= mentioned:
            raise ValueError("gap_skill_ids must be mentioned by the job")
        if set(self.covered_skill_ids) & set(self.gap_skill_ids):
            raise ValueError("covered and gap skill IDs must be disjoint")
        if self.mentioned_skill_ids and not self.in_skill_denominator:
            raise ValueError("a job with mentioned skills must be in the skill denominator")

    @property
    def numerator(self) -> int:
        return len(self.covered_skill_ids)

    @property
    def denominator(self) -> int:
        return len(self.mentioned_skill_ids)

    @property
    def skill_coverage(self) -> Decimal | None:
        if not self.mentioned_skill_ids:
            return None
        return Decimal(self.numerator) / Decimal(self.denominator)


def gap_sort_key(item: SkillGap) -> tuple[float, Decimal, int, str]:
    """Deterministic gap rank: formula, raw inputs, then stable skill ID."""
    return (-item.rank_score, -item.job_share, -item.job_count, item.skill_id)


def representative_job_sort_key(
    item: JobSkillCoverage,
) -> tuple[Decimal, int, int, UUID]:
    """Known skill coverage and evidence breadth, with UUID as final tie-break."""
    if item.skill_coverage is None:
        raise ValueError("representative jobs require known skill coverage")
    return (-item.skill_coverage, -item.numerator, -item.denominator, item.job_id)


@dataclass(frozen=True, slots=True)
class ProfileComparison:
    """Transparent market demand/profile intersection with complete evidence IDs."""

    profile_name: str
    market_name: str
    matching_job_ids: tuple[UUID, ...]
    skill_denominator_job_ids: tuple[UUID, ...]
    demanded_skills: tuple[MarketBucket, ...]
    covered_skills: tuple[MarketBucket, ...]
    neutral_profile_skill_ids: tuple[str, ...]
    gaps: tuple[SkillGap, ...]
    gap_thresholds: GapThresholds
    job_skill_coverage: tuple[JobSkillCoverage, ...]
    representative_jobs: tuple[JobSkillCoverage, ...]

    def __post_init__(self) -> None:
        for value, field in (
            (self.profile_name, "profile_name"),
            (self.market_name, "market_name"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be non-blank")
        _job_ids(self.matching_job_ids, "matching_job_ids")
        _job_ids(self.skill_denominator_job_ids, "skill_denominator_job_ids")
        if not set(self.skill_denominator_job_ids) <= set(self.matching_job_ids):
            raise ValueError("skill denominator must be a subset of matching jobs")
        demanded_ids = tuple(item.value for item in self.demanded_skills)
        _skill_ids(demanded_ids, "demanded_skills")
        demanded_by_id = {item.value: item for item in self.demanded_skills}
        if any(
            item.denominator_job_ids != self.skill_denominator_job_ids
            for item in self.demanded_skills
        ):
            raise ValueError("demanded skills must use skill_denominator_job_ids")
        covered_ids = tuple(item.value for item in self.covered_skills)
        if covered_ids != tuple(skill_id for skill_id in demanded_ids if skill_id in covered_ids):
            raise ValueError("covered_skills must be an ordered subset of demanded_skills")
        if any(demanded_by_id.get(item.value) != item for item in self.covered_skills):
            raise ValueError("covered_skills must preserve demanded skill evidence")
        _skill_ids(self.neutral_profile_skill_ids, "neutral_profile_skill_ids")
        if set(self.neutral_profile_skill_ids) & set(demanded_ids):
            raise ValueError("neutral profile skills must not be demanded skills")
        gap_ids = tuple(item.skill_id for item in self.gaps)
        if len(set(gap_ids)) != len(gap_ids) or not set(gap_ids) <= set(demanded_ids):
            raise ValueError("gaps must uniquely reference demanded skills")
        if set(gap_ids) & set(covered_ids):
            raise ValueError("gaps must not contain covered skills")
        if any(demanded_by_id.get(item.skill_id) != item.demand for item in self.gaps):
            raise ValueError("gaps must preserve demanded skill evidence")
        if any(
            item.job_count < self.gap_thresholds.min_job_count
            or item.job_share < self.gap_thresholds.min_job_share
            for item in self.gaps
        ):
            raise ValueError("gaps must meet the supplied support thresholds")
        expected_gap_ids = {
            item.value
            for item in self.demanded_skills
            if item.value not in covered_ids
            and item.job_count >= self.gap_thresholds.min_job_count
            and item.job_share >= self.gap_thresholds.min_job_share
        }
        if set(gap_ids) != expected_gap_ids:
            raise ValueError("gaps must include every absent demanded skill meeting thresholds")
        if self.gaps != tuple(sorted(self.gaps, key=gap_sort_key)):
            raise ValueError("gaps must use deterministic rank ordering")
        coverage_ids = tuple(item.job_id for item in self.job_skill_coverage)
        if coverage_ids != self.matching_job_ids:
            raise ValueError("job_skill_coverage must exactly cover matching_job_ids")
        coverage_by_id = {item.job_id: item for item in self.job_skill_coverage}
        demanded_id_set = set(demanded_ids)
        covered_id_set = set(covered_ids)
        gap_id_set = set(gap_ids)
        denominator_id_set = set(self.skill_denominator_job_ids)
        if any(
            set(item.mentioned_skill_ids) - demanded_id_set
            or set(item.covered_skill_ids) != set(item.mentioned_skill_ids) & covered_id_set
            or set(item.gap_skill_ids) != set(item.mentioned_skill_ids) & gap_id_set
            or item.in_skill_denominator != (item.job_id in denominator_id_set)
            for item in self.job_skill_coverage
        ):
            raise ValueError(
                "job skill evidence must reconcile to demanded, covered, and gap skills"
            )
        if any(coverage_by_id.get(item.job_id) != item for item in self.representative_jobs):
            raise ValueError("representative_jobs must be drawn from job_skill_coverage")
        representative_ids = tuple(item.job_id for item in self.representative_jobs)
        if len(set(representative_ids)) != len(representative_ids):
            raise ValueError("representative_jobs must not contain duplicate jobs")
        if any(item.skill_coverage is None for item in self.representative_jobs):
            raise ValueError("representative_jobs must have known skill coverage")
        if self.representative_jobs != tuple(
            sorted(self.representative_jobs, key=representative_job_sort_key)
        ):
            raise ValueError("representative_jobs must use deterministic coverage ordering")

    @property
    def demanded_skill_count(self) -> int:
        return len(self.demanded_skills)

    @property
    def covered_skill_count(self) -> int:
        return len(self.covered_skills)

    @property
    def demanded_skill_coverage(self) -> Decimal | None:
        if not self.demanded_skills:
            return None
        return Decimal(self.covered_skill_count) / Decimal(self.demanded_skill_count)
