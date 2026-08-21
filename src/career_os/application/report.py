"""Thin orchestration for one evidence-reconciled market/profile report."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from career_os.application.market import MarketAggregateService
from career_os.application.profile_fit import ProfileComparisonService
from career_os.config.markets import MarketFilters
from career_os.config.profiles import Profile
from career_os.config.skills import SkillTaxonomy
from career_os.domain.markets import MarketAggregates
from career_os.domain.profiles import ProfileComparison
from career_os.persistence.report_queries import ReportEvidenceRepository, ReportJobEvidence


@dataclass(frozen=True, slots=True)
class MarketProfileReport:
    """Complete report inputs with exact stored evidence and taxonomy labels."""

    filters: MarketFilters
    profile: Profile
    market: MarketAggregates
    comparison: ProfileComparison
    jobs: tuple[ReportJobEvidence, ...]
    skill_labels: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if self.filters.name != self.market.market_name:
            raise ValueError("filters and market names must match")
        if self.profile.name != self.comparison.profile_name:
            raise ValueError("profile and comparison names must match")
        if self.market.market_name != self.comparison.market_name:
            raise ValueError("market and comparison names must match")
        if self.market.matching_job_ids != self.comparison.matching_job_ids:
            raise ValueError("market and comparison job IDs must match")
        if self.market.skill_denominator_job_ids != self.comparison.skill_denominator_job_ids:
            raise ValueError("market and comparison skill denominators must match")
        if tuple(item.job_id for item in self.jobs) != self.market.matching_job_ids:
            raise ValueError("report jobs must exactly cover matching market jobs")
        skill_ids = tuple(skill_id for skill_id, _ in self.skill_labels)
        if skill_ids != tuple(sorted(set(skill_ids))):
            raise ValueError("skill_labels must contain unique skill IDs in stable order")
        if any(not skill_id or not label.strip() for skill_id, label in self.skill_labels):
            raise ValueError("skill labels must be non-blank")
        reported_skill_ids = {item.value for item in self.market.skills} | self.profile.skill_ids
        if not reported_skill_ids <= set(skill_ids):
            raise ValueError("skill_labels must cover every reported skill")


@dataclass(slots=True)
class MarketReportService:
    """Compose one report inside the caller-owned read transaction."""

    session: Session
    taxonomy: SkillTaxonomy
    normalizer_version: str

    def calculate(self, *, filters: MarketFilters, profile: Profile) -> MarketProfileReport:
        market = MarketAggregateService(
            self.session,
            taxonomy_version=self.taxonomy.version,
            normalizer_version=self.normalizer_version,
        ).calculate(filters)
        comparison = ProfileComparisonService(self.taxonomy).calculate(profile, market)
        jobs = ReportEvidenceRepository(self.session).get_jobs(market.matching_job_ids)
        return MarketProfileReport(
            filters=filters,
            profile=profile,
            market=market,
            comparison=comparison,
            jobs=jobs,
            skill_labels=tuple(sorted((skill.id, skill.label) for skill in self.taxonomy.skills)),
        )
