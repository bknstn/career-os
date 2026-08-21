"""Pure, transparent comparison of one declared profile with market aggregates."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import log1p
from uuid import UUID

from career_os.config.profiles import Profile, validate_profile_taxonomy
from career_os.config.skills import SkillTaxonomy
from career_os.config.sources import ConfigurationError
from career_os.domain.markets import MarketAggregates
from career_os.domain.profiles import (
    GapThresholds,
    JobSkillCoverage,
    ProfileComparison,
    SkillGap,
    gap_sort_key,
    representative_job_sort_key,
)


class ProfileComparisonConsistencyError(RuntimeError):
    """Profile comparison inputs cannot be reconciled without inventing evidence."""


@dataclass(frozen=True, slots=True)
class ProfileComparisonService:
    """Compare declared skills without re-filtering the supplied market snapshot."""

    taxonomy: SkillTaxonomy
    gap_thresholds: GapThresholds = field(default_factory=GapThresholds)
    representative_job_limit: int = 5

    def __post_init__(self) -> None:
        if type(self.representative_job_limit) is not int or self.representative_job_limit < 0:
            raise ValueError("representative_job_limit must be a non-negative integer")

    def calculate(self, profile: Profile, market: MarketAggregates) -> ProfileComparison:
        self._validate_inputs(profile, market)
        profile_skill_ids = profile.skill_ids
        demanded = tuple(sorted(market.skills, key=lambda item: item.value))
        covered = tuple(item for item in demanded if item.value in profile_skill_ids)
        demanded_ids = {item.value for item in demanded}
        neutral = tuple(sorted(profile_skill_ids - demanded_ids))
        gaps = tuple(
            sorted(
                (
                    SkillGap(
                        demand=item,
                        rank_score=float(item.job_share) * log1p(item.job_count),
                    )
                    for item in demanded
                    if item.value not in profile_skill_ids
                    and item.job_count >= self.gap_thresholds.min_job_count
                    and item.job_share >= self.gap_thresholds.min_job_share
                ),
                key=gap_sort_key,
            )
        )
        coverage = self._job_skill_coverage(profile_skill_ids, gaps, market)
        representatives = tuple(
            sorted(
                (item for item in coverage if item.skill_coverage is not None),
                key=representative_job_sort_key,
            )[: self.representative_job_limit]
        )
        return ProfileComparison(
            profile_name=profile.name,
            market_name=market.market_name,
            matching_job_ids=market.matching_job_ids,
            skill_denominator_job_ids=market.skill_denominator_job_ids,
            demanded_skills=demanded,
            covered_skills=covered,
            neutral_profile_skill_ids=neutral,
            gaps=gaps,
            gap_thresholds=self.gap_thresholds,
            job_skill_coverage=coverage,
            representative_jobs=representatives,
        )

    def _validate_inputs(self, profile: Profile, market: MarketAggregates) -> None:
        try:
            validate_profile_taxonomy(profile, taxonomy=self.taxonomy)
        except ConfigurationError as error:
            raise ProfileComparisonConsistencyError(str(error)) from error
        if market.taxonomy_version != self.taxonomy.version:
            raise ProfileComparisonConsistencyError(
                "market taxonomy version does not match the loaded taxonomy"
            )
        taxonomy_ids = {skill.id for skill in self.taxonomy.skills}
        market_skill_ids = [item.value for item in market.skills]
        if len(set(market_skill_ids)) != len(market_skill_ids):
            raise ProfileComparisonConsistencyError("market contains duplicate skill buckets")
        unknown = sorted(set(market_skill_ids) - taxonomy_ids)
        if unknown:
            raise ProfileComparisonConsistencyError(
                "market contains unknown taxonomy skill IDs: " + ", ".join(unknown)
            )

    @staticmethod
    def _job_skill_coverage(
        profile_skill_ids: frozenset[str],
        gaps: tuple[SkillGap, ...],
        market: MarketAggregates,
    ) -> tuple[JobSkillCoverage, ...]:
        mentioned_by_job: dict[UUID, set[str]] = {
            job_id: set() for job_id in market.matching_job_ids
        }
        for bucket in market.skills:
            for job_id in bucket.job_ids:
                mentioned_by_job[job_id].add(bucket.value)
        denominator_ids = set(market.skill_denominator_job_ids)
        gap_ids = {gap.skill_id for gap in gaps}
        return tuple(
            JobSkillCoverage(
                job_id=job_id,
                in_skill_denominator=job_id in denominator_ids,
                mentioned_skill_ids=tuple(sorted(mentioned_by_job[job_id])),
                covered_skill_ids=tuple(sorted(mentioned_by_job[job_id] & profile_skill_ids)),
                gap_skill_ids=tuple(sorted(mentioned_by_job[job_id] & gap_ids)),
            )
            for job_id in market.matching_job_ids
        )
