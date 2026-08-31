"""Thin orchestration for one evidence-reconciled market/profile report."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from career_os.application.market import MarketAggregateService
from career_os.config.markets import MarketFilters
from career_os.config.profiles import Profile
from career_os.config.skills import SkillTaxonomy
from career_os.domain.hybrid_results import (
    GATE_IDS,
    HybridBucket,
    HybridCompensation,
    HybridConcentration,
    HybridEvaluation,
    HybridFreshness,
    HybridGate,
    HybridJobCompensation,
    HybridLocation,
    HybridObservations,
    HybridOpportunities,
    HybridOpportunity,
    HybridProfileMentions,
    HybridProvenance,
    HybridQuality,
    HybridResult,
    HybridSample,
    HybridSkill,
    HybridSkillMention,
    HybridSource,
    HybridSourceCoverage,
    QualityReviewInput,
)
from career_os.domain.markets import MarketAggregates
from career_os.domain.profiles import ProfileComparison
from career_os.persistence.report_queries import (
    ReportEvidenceConsistencyError,
    ReportEvidenceRepository,
    ReportEvidenceSnapshot,
    ReportJobEvidence,
)


@dataclass(frozen=True, slots=True)
class MarketProfileReport:
    """Legacy internal report model retained for lower-level aggregate tests."""

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

    def calculate(
        self,
        *,
        filters: MarketFilters,
        profile: Profile,
        quality_review: QualityReviewInput | None = None,
    ) -> HybridResult:
        market = MarketAggregateService(
            self.session,
            taxonomy_version=self.taxonomy.version,
            normalizer_version=self.normalizer_version,
        ).calculate(filters)
        evidence = ReportEvidenceRepository(self.session).get_bulk(
            job_ids=market.matching_job_ids, filters=filters
        )
        return HybridResultBuilder(self.taxonomy).build(
            filters=filters,
            profile=profile,
            market=market,
            evidence=evidence,
            quality_review=quality_review,
        )


@dataclass(frozen=True, slots=True)
class HybridResultBuilder:
    """Compose exactly one immutable H1 result from the evidence snapshot."""

    taxonomy: SkillTaxonomy

    def build(
        self,
        *,
        filters: MarketFilters,
        profile: Profile,
        market: MarketAggregates,
        evidence: ReportEvidenceSnapshot,
        quality_review: QualityReviewInput | None = None,
    ) -> HybridResult:
        if market.market_name != filters.name:
            raise ReportEvidenceConsistencyError(
                "market name changed while report evidence was read"
            )
        if market.taxonomy_version != self.taxonomy.version:
            raise ReportEvidenceConsistencyError(
                "market taxonomy version changed while report evidence was read"
            )
        jobs = evidence.jobs
        if tuple(job.job_id for job in jobs) != market.matching_job_ids:
            raise ReportEvidenceConsistencyError(
                "market selection changed while report evidence was read"
            )
        source_by_key = {source.key: source for source in evidence.configured_sources}
        if len(source_by_key) != len(evidence.configured_sources):
            raise ReportEvidenceConsistencyError("configured source keys are not unique")
        if any(not source.enabled for source in evidence.configured_sources):
            raise ReportEvidenceConsistencyError(
                "disabled sources cannot enter the configured H1 panel"
            )
        for job in jobs:
            source = source_by_key.get(job.source_key)
            if source is None:
                raise ReportEvidenceConsistencyError(
                    "matching job source is not an enabled configured source"
                )
            if not source.enabled:
                raise ReportEvidenceConsistencyError(
                    "matching job source is disabled and outside the configured panel"
                )
            if source.company_name != job.company_name:
                raise ReportEvidenceConsistencyError(
                    f"source/company identity mismatch for job {job.job_id}"
                )
        if quality_review is not None:
            if not set(quality_review.reviewed_job_ids) <= set(market.matching_job_ids):
                raise ValueError("reviewed_job_ids must be a subset of matching job IDs")
            if not set(quality_review.false_positive_job_ids) <= set(
                quality_review.reviewed_job_ids
            ):
                raise ValueError("false_positive_job_ids must be a subset of reviewed_job_ids")

        try:
            matching_ids = market.matching_job_ids
            described_ids = tuple(job.job_id for job in jobs if job.description_text.strip())
            compensation_ids = tuple(
                job.job_id
                for job in jobs
                if any(
                    component.currency is not None
                    and (component.min_value is not None or component.max_value is not None)
                    for component in job.compensation
                )
            )
            # The aggregate service and this bulk projection deliberately read
            # the same selection in one caller-owned repeatable-read transaction.
            # Do not let a disagreement silently change a denominator at the H1
            # boundary: a report with mismatched evidence is a consistency error.
            if market.skill_denominator_job_ids != described_ids:
                raise ReportEvidenceConsistencyError(
                    "market skill denominator changed while report evidence was read"
                )
            if market.compensation_job_ids != compensation_ids:
                raise ReportEvidenceConsistencyError(
                    "market compensation denominator changed while report evidence was read"
                )
            if market.collection_cutoff != (
                max(
                    (job.last_seen_at for job in jobs if job.last_seen_at is not None),
                    default=None,
                )
            ):
                raise ReportEvidenceConsistencyError(
                    "market collection cutoff changed while report evidence was read"
                )
            opportunities = tuple(self._opportunity(job) for job in jobs)
            source_coverage = self._source_coverage(evidence, jobs)
            companies = self._buckets(
                ((job.company_name, job.job_id) for job in jobs), denominator=matching_ids
            )
            seniorities = self._buckets(
                ((job.seniority or "unknown", job.job_id) for job in jobs), denominator=matching_ids
            )
            workplace_types = self._buckets(
                ((job.workplace_type or "unknown", job.job_id) for job in jobs),
                denominator=matching_ids,
            )
            countries = self._countries(jobs, matching_ids)
            skills = self._skills(jobs, described_ids)
            compensation = self._compensation(jobs, matching_ids)
            concentration = self._concentration(companies, matching_ids)
            last_seen_values = tuple(
                job.last_seen_at for job in jobs if job.last_seen_at is not None
            )
            collection_cutoff = max(last_seen_values) if last_seen_values else None
            sample = HybridSample(
                job_ids=matching_ids,
                company_names=tuple(sorted({job.company_name for job in jobs})),
                max_company_concentration=concentration,
                source_coverage=source_coverage,
                collection_cutoff=collection_cutoff,
                skill_denominator_job_ids=described_ids,
                compensation_job_ids=compensation_ids,
            )
            observations = HybridObservations(
                sample=sample,
                companies=companies,
                skills=skills,
                seniorities=seniorities,
                workplace_types=workplace_types,
                countries=countries,
                compensation=compensation,
                profile_mentions=self._profile_mentions(profile, skills),
            )
            quality = self._quality(
                jobs=jobs,
                sample=sample,
                skills=skills,
                compensation=compensation,
                quality_review=quality_review,
            )
            return HybridResult(
                filters=filters,
                profile=profile,
                quality=quality,
                opportunities=HybridOpportunities(items=opportunities),
                observations=observations,
            )
        except ReportEvidenceConsistencyError:
            raise
        except ValueError as error:
            raise ReportEvidenceConsistencyError(
                "H1 evidence could not be reconciled to the canonical result contract"
            ) from error

    def _opportunity(self, job: ReportJobEvidence) -> HybridOpportunity:
        if job.first_seen_at is None or job.last_seen_at is None:
            raise ReportEvidenceConsistencyError("selected job is missing persisted freshness")
        return HybridOpportunity(
            job_id=job.job_id,
            company_name=job.company_name,
            title=job.title,
            source=HybridSource(key=job.source_key, kind=job.source_kind, url=job.source_url),
            freshness=HybridFreshness(
                first_seen_at=job.first_seen_at, last_seen_at=job.last_seen_at
            ),
            locations=tuple(
                HybridLocation(
                    ordinal=item.ordinal,
                    city=item.city,
                    region=item.region,
                    country_code=item.country_code,
                    is_primary=item.is_primary,
                    confidence=item.confidence,
                )
                for item in job.locations
            ),
            remote_scope=job.remote_scope,
            skill_mentions=tuple(
                HybridSkillMention(
                    id=item.id,
                    skill_id=item.skill_id,
                    matched_text=item.matched_text,
                    section=item.section,
                    method=item.method,
                    confidence=item.confidence,
                    taxonomy_version=item.taxonomy_version,
                )
                for item in job.skill_mentions
            ),
            compensation=tuple(
                HybridJobCompensation(
                    component_id=item.component_id,
                    component_type=item.component_type,
                    min_value=item.min_value,
                    max_value=item.max_value,
                    currency=item.currency,
                    interval=item.interval,
                    location_tier=item.location_tier,
                    source_method=item.source_method,
                )
                for item in job.compensation
                if item.currency is not None
                and (item.min_value is not None or item.max_value is not None)
            ),
            status=job.status,
            role_family=job.role_family,
            seniority=job.seniority,
            workplace_type=job.workplace_type,
        )

    @staticmethod
    def _buckets(
        rows: Iterable[tuple[str | None, UUID]], *, denominator: tuple[UUID, ...]
    ) -> tuple[HybridBucket, ...]:
        grouped: dict[str, set[UUID]] = defaultdict(set)
        for value, job_id in rows:
            grouped[str(value)].add(job_id)
        return tuple(
            HybridBucket(
                value=value, job_ids=tuple(sorted(grouped[value])), denominator_job_ids=denominator
            )
            for value in sorted(grouped)
        )

    @staticmethod
    def _countries(
        jobs: tuple[ReportJobEvidence, ...], matching: tuple[UUID, ...]
    ) -> tuple[HybridBucket, ...]:
        rows = [
            (location.country_code, job.job_id)
            for job in jobs
            for location in job.locations
            if location.country_code is not None
        ]
        denominator = tuple(sorted({job_id for _, job_id in rows}))
        return HybridResultBuilder._buckets(rows, denominator=denominator)

    def _skills(
        self, jobs: tuple[ReportJobEvidence, ...], denominator: tuple[UUID, ...]
    ) -> tuple[HybridSkill, ...]:
        described_companies = {job.company_name for job in jobs if job.job_id in set(denominator)}
        by_skill: dict[str, set[UUID]] = defaultdict(set)
        company_by_skill: dict[str, set[str]] = defaultdict(set)
        for job in jobs:
            if job.job_id not in set(denominator):
                continue
            for item in job.skill_mentions:
                by_skill[item.skill_id].add(job.job_id)
                company_by_skill[item.skill_id].add(job.company_name)
        labels = {item.id: item.label for item in self.taxonomy.skills}
        unknown = sorted(set(by_skill) - set(labels))
        if unknown:
            raise ValueError(
                "skill evidence references unknown taxonomy IDs: " + ", ".join(unknown)
            )
        denominator_companies = tuple(sorted(described_companies))
        return tuple(
            HybridSkill(
                skill_id=skill_id,
                label=labels[skill_id],
                job_ids=tuple(sorted(by_skill[skill_id])),
                denominator_job_ids=denominator,
                company_names=tuple(sorted(company_by_skill[skill_id])),
                denominator_company_names=denominator_companies,
            )
            for skill_id in sorted(by_skill)
        )

    @staticmethod
    def _compensation(
        jobs: tuple[ReportJobEvidence, ...], matching: tuple[UUID, ...]
    ) -> tuple[HybridCompensation, ...]:
        grouped: dict[
            tuple[str, str, str, str | None, UUID | None], list[HybridJobCompensation]
        ] = defaultdict(list)
        for job in jobs:
            for item in job.compensation:
                if item.currency is None or (item.min_value is None and item.max_value is None):
                    continue
                interval = item.interval or "unknown"
                component = HybridJobCompensation(
                    component_id=item.component_id,
                    component_type=item.component_type,
                    min_value=item.min_value,
                    max_value=item.max_value,
                    currency=item.currency,
                    interval=item.interval,
                    location_tier=item.location_tier,
                    source_method=item.source_method,
                )
                grouped[
                    (
                        item.component_type,
                        item.currency,
                        interval,
                        item.location_tier,
                        job.job_id if interval == "unknown" else None,
                    )
                ].append(component)
        result: list[HybridCompensation] = []
        for key in sorted(
            grouped,
            key=lambda value: (value[0], value[1], value[2], value[3] or "", str(value[4] or "")),
        ):
            group = grouped[key]
            minimums = [item.min_value for item in group if item.min_value is not None]
            maximums = [item.max_value for item in group if item.max_value is not None]
            result.append(
                HybridCompensation(
                    component_type=key[0],
                    currency=key[1],
                    interval=key[2],
                    location_tier=key[3],
                    component_ids=tuple(sorted(item.component_id for item in group)),
                    job_ids=tuple(
                        sorted(
                            {
                                job_id
                                for job in jobs
                                for item in job.compensation
                                if item.component_id
                                in {component.component_id for component in group}
                                for job_id in (job.job_id,)
                            }
                        )
                    ),
                    denominator_job_ids=matching,
                    min_value=min(minimums) if minimums else None,
                    max_value=max(maximums) if maximums else None,
                )
            )
        return tuple(result)

    @staticmethod
    def _concentration(
        companies: tuple[HybridBucket, ...], matching: tuple[UUID, ...]
    ) -> HybridConcentration | None:
        if not matching or not companies:
            return None
        selected = min(
            companies,
            key=lambda item: (
                -(item.job_share or Decimal(0)),
                -item.job_count,
                item.value,
            ),
        )
        return HybridConcentration(
            company_name=selected.value,
            job_ids=selected.job_ids,
            denominator_job_ids=matching,
        )

    @staticmethod
    def _source_coverage(
        evidence: ReportEvidenceSnapshot, jobs: tuple[ReportJobEvidence, ...]
    ) -> HybridSourceCoverage:
        configured = tuple(source.key for source in evidence.configured_sources)
        ids_by_source = {
            key: tuple(sorted(job.job_id for job in jobs if job.source_key == key))
            for key in configured
        }
        matching = tuple(key for key in configured if ids_by_source[key])
        return HybridSourceCoverage(
            configured_source_keys=configured,
            matching_source_keys=matching,
            job_ids_by_source=tuple((key, ids_by_source[key]) for key in configured),
        )

    def _profile_mentions(
        self, profile: Profile, skills: tuple[HybridSkill, ...]
    ) -> HybridProfileMentions:
        profile_ids = tuple(sorted(profile.skill_ids))
        market_ids = {item.skill_id for item in skills}
        covered = tuple(sorted(set(profile_ids) & market_ids))
        neutral = tuple(sorted(set(profile_ids) - market_ids))
        uncovered = tuple(item for item in skills if item.skill_id not in set(profile_ids))
        return HybridProfileMentions(
            profile_skill_ids=profile_ids,
            covered_skill_ids=covered,
            neutral_skill_ids=neutral,
            uncovered_mentions=uncovered,
        )

    @staticmethod
    def _quality(
        *,
        jobs: tuple[ReportJobEvidence, ...],
        sample: HybridSample,
        skills: tuple[HybridSkill, ...],
        compensation: tuple[HybridCompensation, ...],
        quality_review: QualityReviewInput | None,
    ) -> HybridQuality:
        ids = sample.job_ids
        companies = sample.company_names
        review_ref = (
            quality_review.ref if quality_review is not None else "quality_review:not_provided"
        )
        reviewed_ids = quality_review.reviewed_job_ids if quality_review is not None else ()
        false_positive_ids = (
            quality_review.false_positive_job_ids if quality_review is not None else ()
        )
        manual_evaluable = quality_review is not None and bool(reviewed_ids)
        reviewed_status = (
            "passed"
            if manual_evaluable and len(reviewed_ids) >= 20
            else "failed"
            if manual_evaluable
            else "not_evaluable"
        )
        false_status = (
            "passed"
            if manual_evaluable and not false_positive_ids
            else "failed"
            if manual_evaluable
            else "not_evaluable"
        )
        represented_status = (
            "not_evaluable" if not ids else "passed" if len(companies) >= 5 else "failed"
        )
        concentration_value = (
            sample.max_company_concentration.job_share if sample.max_company_concentration else None
        )
        concentration_status = (
            "not_evaluable"
            if concentration_value is None
            else "passed"
            if concentration_value <= Decimal("0.40")
            else "failed"
        )
        gates = (
            HybridGate(
                "reviewed_eligible_jobs",
                reviewed_status,
                ">=",
                20,
                len(reviewed_ids),
                "reviewed_jobs",
                reviewed_ids,
                tuple(
                    sorted({job.company_name for job in jobs if job.job_id in set(reviewed_ids)})
                ),
            ),
            HybridGate(
                "represented_companies",
                represented_status,
                ">=",
                5,
                len(companies),
                "companies",
                ids if ids else (),
                companies,
            ),
            HybridGate(
                "max_company_concentration",
                concentration_status,
                "<=",
                Decimal("0.40"),
                concentration_value,
                "share",
                sample.max_company_concentration.job_ids
                if sample.max_company_concentration
                else (),
                (sample.max_company_concentration.company_name,)
                if sample.max_company_concentration
                else (),
            ),
            HybridGate(
                "demonstrated_eligibility_false_positives",
                false_status,
                "==",
                0,
                len(false_positive_ids),
                "false_positive_jobs",
                false_positive_ids,
                tuple(
                    sorted(
                        {job.company_name for job in jobs if job.job_id in set(false_positive_ids)}
                    )
                ),
            ),
            HybridGate(
                "skill_semantics",
                "passed",
                "==",
                "mentions_only",
                "mentions_only",
                "semantics",
                ids,
                companies,
            ),
            HybridGate(
                "compensation_reconciled",
                "passed",
                "==",
                "reconciled",
                "reconciled",
                "evidence",
                tuple(sorted({job_id for item in compensation for job_id in item.job_ids})),
                tuple(
                    sorted(
                        {
                            job.company_name
                            for job in jobs
                            if job.job_id
                            in {job_id for item in compensation for job_id in item.job_ids}
                        }
                    )
                ),
            ),
        )
        provenance = tuple(
            (
                gate_id,
                HybridEvaluation(
                    basis="manual_review"
                    if gate_id
                    in {"reviewed_eligible_jobs", "demonstrated_eligibility_false_positives"}
                    else "computed"
                    if gate_id in {"represented_companies", "max_company_concentration"}
                    else "contract_invariant",
                    provenance=HybridProvenance(
                        kind="review_artifact"
                        if gate_id
                        in {"reviewed_eligible_jobs", "demonstrated_eligibility_false_positives"}
                        else "result_derived"
                        if gate_id in {"represented_companies", "max_company_concentration"}
                        else "contract_rule",
                        ref=review_ref
                        if gate_id
                        in {"reviewed_eligible_jobs", "demonstrated_eligibility_false_positives"}
                        else "opportunities.items"
                        if gate_id == "represented_companies"
                        else "observations.sample.companies"
                        if gate_id == "max_company_concentration"
                        else "quality.claim_policy.skill_semantics"
                        if gate_id == "skill_semantics"
                        else "per_job.compensation",
                        reviewed_job_ids=(
                            reviewed_ids
                            if gate_id
                            in {
                                "reviewed_eligible_jobs",
                                "demonstrated_eligibility_false_positives",
                            }
                            else ()
                        ),
                    ),
                ),
            )
            for gate_id in GATE_IDS
        )
        return HybridQuality(
            state="accepted"
            if all(item.status == "passed" for item in gates)
            else "evidence_limited",
            gates=gates,
            failed_gates=tuple(item.gate_id for item in gates if item.status != "passed"),
            evaluation_provenance=provenance,
            observed_values={
                "matching_job_count": len(ids),
                "company_count": len(companies),
                "reviewed_eligible_job_count": len(reviewed_ids),
                "represented_company_count": len(companies),
                "demonstrated_eligibility_false_positive_count": len(false_positive_ids),
                "skill_semantics": "mentions_only",
                "compensation_reconciled": True,
                "max_company_concentration": concentration_value,
                "source_coverage": sample.source_coverage.source_share,
                "compensation_coverage": Decimal(len(sample.compensation_job_ids))
                / Decimal(len(ids))
                if ids
                else None,
                "collection_cutoff": sample.collection_cutoff,
            },
        )


def build_hybrid_result(
    *,
    filters: MarketFilters,
    profile: Profile,
    market: MarketAggregates,
    evidence: ReportEvidenceSnapshot,
    taxonomy: SkillTaxonomy,
    quality_review: QualityReviewInput | None = None,
) -> HybridResult:
    """Functional entrypoint for callers that do not need the service object."""
    return HybridResultBuilder(taxonomy).build(
        filters=filters,
        profile=profile,
        market=market,
        evidence=evidence,
        quality_review=quality_review,
    )
