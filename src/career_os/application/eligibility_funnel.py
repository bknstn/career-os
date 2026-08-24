"""Deterministic, read-only eligibility-funnel audit over canonical jobs."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from career_os.config.markets import MarketFilters
from career_os.persistence.market_queries import build_market_job_ids_query
from career_os.persistence.models import JobLocation, JobPosting, Source


class EligibilityFunnelConsistencyError(RuntimeError):
    """The staged audit does not reconcile to the accepted market query."""


@dataclass(frozen=True, slots=True)
class FunnelStage:
    """One cumulative funnel stage reconciled to exact canonical job IDs."""

    key: str
    label: str
    job_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class FunnelGroup:
    """Stage counts for one stored source/company pair."""

    source_key: str
    source_kind: str
    company_name: str
    counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class FunnelExclusion:
    """One mutually exclusive first-failure reason and its evidence IDs."""

    reason: str
    label: str
    job_ids: tuple[UUID, ...]
    representative_job_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class FunnelJobEvidence:
    """Stored normalized evidence used to inspect an exclusion."""

    job_id: UUID
    source_key: str
    company_name: str
    title: str
    role_family: str | None
    seniority: str | None
    workplace_type: str | None
    remote_scope: str | None
    country_codes: tuple[str, ...]
    source_url: str | None
    exclusion_reason: str | None


@dataclass(frozen=True, slots=True)
class EligibilityFunnelAudit:
    """Complete current-state audit with explicit cohort and reconciliation evidence."""

    filters: MarketFilters
    cohort_first_seen_at: datetime | None
    collection_cutoff: datetime | None
    taxonomy_versions: tuple[str, ...]
    enrichment_normalizer_versions: tuple[str, ...]
    stages: tuple[FunnelStage, ...]
    groups: tuple[FunnelGroup, ...]
    exclusions: tuple[FunnelExclusion, ...]
    representative_jobs: tuple[FunnelJobEvidence, ...]
    title_include_only_exclusion_job_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if not self.stages or self.stages[0].key != "cohort":
            raise ValueError("funnel must begin with the cohort stage")
        keys = tuple(stage.key for stage in self.stages)
        if keys != tuple(dict.fromkeys(keys)):
            raise ValueError("funnel stage keys must be unique and stable")
        previous = set(self.stages[0].job_ids)
        for stage in self.stages:
            if stage.job_ids != tuple(sorted(set(stage.job_ids))):
                raise ValueError("funnel stage IDs must be unique and stable")
            if not set(stage.job_ids) <= previous:
                raise ValueError("each funnel stage must be a subset of the preceding stage")
            previous = set(stage.job_ids)
        excluded = {job_id for item in self.exclusions for job_id in item.job_ids}
        final = set(self.stages[-1].job_ids)
        cohort = set(self.stages[0].job_ids)
        if excluded & final or excluded | final != cohort:
            raise ValueError("first-failure exclusions and final matches must partition the cohort")
        if len(excluded) != sum(len(item.job_ids) for item in self.exclusions):
            raise ValueError("first-failure exclusion buckets must not overlap")
        if (
            self.title_include_only_exclusion_job_ids
            != tuple(sorted(set(self.title_include_only_exclusion_job_ids)))
            or not set(self.title_include_only_exclusion_job_ids) <= excluded
        ):
            raise ValueError("title-only counterfactual IDs must be stable excluded cohort IDs")

    @property
    def cohort_job_ids(self) -> tuple[UUID, ...]:
        return self.stages[0].job_ids

    @property
    def matching_job_ids(self) -> tuple[UUID, ...]:
        return self.stages[-1].job_ids


@dataclass(frozen=True, slots=True)
class _JobRow:
    job: JobPosting
    source_key: str
    source_kind: str
    country_codes: tuple[str, ...]


_STAGES = (
    ("cohort", "Stored canonical cohort"),
    ("active", "Active posting"),
    ("title_included", "Title candidate"),
    ("title_accepted", "Title exclusions accepted"),
    ("role_family", "Accepted role family"),
    ("seniority", "Accepted seniority"),
    ("geography_evidence", "Target-country or explicit remote evidence"),
    ("country_eligible", "Country / remote-scope eligible"),
    ("company_accepted", "Configured company"),
    ("final_match", "Final market match"),
)

_REASON_LABELS = {
    "inactive": "Posting is inactive",
    "title_not_included": "Title contains no configured inclusion term",
    "title_excluded": "Title contains a configured exclusion term",
    "role_family_not_accepted": "Normalized role family is not accepted",
    "seniority_not_accepted": "Normalized seniority is not accepted",
    "target_country_not_found": "No normalized target-country location",
    "no_target_country_or_remote_evidence": (
        "No normalized target-country location or explicit remote workplace evidence"
    ),
    "remote_scope_unknown": "Remote scope is unknown",
    "remote_scope_not_accepted": "Remote scope is not accepted",
    "company_not_configured": "Company is outside the configured panel",
}


@dataclass(slots=True)
class EligibilityFunnelService:
    """Measure current rules without mutating jobs, configuration, or transactions."""

    session: Session
    representative_limit: int = 5

    def __post_init__(self) -> None:
        if self.representative_limit < 1:
            raise ValueError("representative_limit must be positive")

    def calculate(self, filters: MarketFilters) -> EligibilityFunnelAudit:
        rows = self._load_rows()
        stage_ids: dict[str, list[UUID]] = {key: [] for key, _ in _STAGES}
        reasons: dict[str, list[UUID]] = defaultdict(list)
        evidence: dict[UUID, FunnelJobEvidence] = {}
        title_only_exclusions: list[UUID] = []

        for row in rows:
            job = row.job
            stage_ids["cohort"].append(job.id)
            passed, reason = self._evaluate(row, filters, stage_ids)
            if not passed:
                assert reason is not None
                reasons[reason].append(job.id)
                if reason == "title_not_included" and self._passes_without_title_include(
                    row, filters
                ):
                    title_only_exclusions.append(job.id)
            evidence[job.id] = FunnelJobEvidence(
                job_id=job.id,
                source_key=row.source_key,
                company_name=job.company_name,
                title=job.title_raw,
                role_family=job.role_family,
                seniority=job.seniority,
                workplace_type=job.workplace_type,
                remote_scope=job.remote_scope,
                country_codes=row.country_codes,
                source_url=job.source_url,
                exclusion_reason=reason,
            )

        stages = tuple(
            FunnelStage(key=key, label=label, job_ids=tuple(sorted(stage_ids[key])))
            for key, label in _STAGES
        )
        expected = tuple(self.session.scalars(build_market_job_ids_query(filters)))
        if stages[-1].job_ids != expected:
            raise EligibilityFunnelConsistencyError(
                "staged final match does not reconcile to build_market_job_ids_query"
            )
        exclusions = tuple(
            FunnelExclusion(
                reason=reason,
                label=_REASON_LABELS[reason],
                job_ids=tuple(sorted(job_ids)),
                representative_job_ids=tuple(sorted(job_ids))[: self.representative_limit],
            )
            for reason, job_ids in sorted(
                reasons.items(), key=lambda item: (-len(item[1]), item[0])
            )
        )
        representative_ids = {
            job_id for item in exclusions for job_id in item.representative_job_ids
        }
        return EligibilityFunnelAudit(
            filters=filters,
            cohort_first_seen_at=min((row.job.first_seen_at for row in rows), default=None),
            collection_cutoff=max((row.job.last_seen_at for row in rows), default=None),
            taxonomy_versions=tuple(sorted({row.job.taxonomy_version for row in rows})),
            enrichment_normalizer_versions=tuple(
                sorted({row.job.enrichment_normalizer_version for row in rows})
            ),
            stages=stages,
            groups=self._groups(rows, stages),
            exclusions=exclusions,
            representative_jobs=tuple(evidence[job_id] for job_id in sorted(representative_ids)),
            title_include_only_exclusion_job_ids=tuple(sorted(title_only_exclusions)),
        )

    def _load_rows(self) -> tuple[_JobRow, ...]:
        jobs = tuple(
            self.session.execute(
                select(JobPosting, Source.key, Source.kind)
                .join(Source, Source.id == JobPosting.source_id)
                .order_by(JobPosting.id)
            )
        )
        locations: dict[UUID, list[str]] = defaultdict(list)
        for job_id, country_code in self.session.execute(
            select(JobLocation.job_id, JobLocation.country_code)
            .where(JobLocation.country_code.is_not(None))
            .distinct()
            .order_by(JobLocation.job_id, JobLocation.country_code)
        ):
            assert country_code is not None
            locations[job_id].append(country_code)
        return tuple(
            _JobRow(
                job=job,
                source_key=source_key,
                source_kind=source_kind,
                country_codes=tuple(locations[job.id]),
            )
            for job, source_key, source_kind in jobs
        )

    @staticmethod
    def _evaluate(
        row: _JobRow,
        filters: MarketFilters,
        stage_ids: dict[str, list[UUID]],
    ) -> tuple[bool, str | None]:
        job = row.job

        def pass_stage(key: str) -> None:
            stage_ids[key].append(job.id)

        if filters.active_only and job.status != "active":
            return False, "inactive"
        pass_stage("active")
        title = job.title_normalized.lower()
        if filters.title_include and not any(value in title for value in filters.title_include):
            return False, "title_not_included"
        pass_stage("title_included")
        if any(value in title for value in filters.title_exclude):
            return False, "title_excluded"
        pass_stage("title_accepted")
        if filters.role_families and job.role_family not in filters.role_families:
            return False, "role_family_not_accepted"
        pass_stage("role_family")
        if filters.seniorities and job.seniority not in filters.seniorities:
            return False, "seniority_not_accepted"
        pass_stage("seniority")

        local_match = bool(set(row.country_codes) & set(filters.country_codes))
        if filters.country_codes:
            if filters.include_remote:
                if not local_match and job.workplace_type != "remote":
                    return False, "no_target_country_or_remote_evidence"
            elif not local_match:
                return False, "target_country_not_found"
        pass_stage("geography_evidence")

        if filters.country_codes and not local_match and filters.include_remote:
            if job.remote_scope is None or not job.remote_scope.strip():
                return False, "remote_scope_unknown"
            if job.remote_scope.strip().lower() not in filters.remote_scopes:
                return False, "remote_scope_not_accepted"
        pass_stage("country_eligible")
        if filters.company_names and job.company_name.lower() not in filters.company_names:
            return False, "company_not_configured"
        pass_stage("company_accepted")
        pass_stage("final_match")
        return True, None

    @staticmethod
    def _groups(
        rows: tuple[_JobRow, ...], stages: tuple[FunnelStage, ...]
    ) -> tuple[FunnelGroup, ...]:
        memberships = {stage.key: set(stage.job_ids) for stage in stages}
        grouped: dict[tuple[str, str, str], list[UUID]] = defaultdict(list)
        for row in rows:
            grouped[(row.source_key, row.source_kind, row.job.company_name)].append(row.job.id)
        return tuple(
            FunnelGroup(
                source_key=key[0],
                source_kind=key[1],
                company_name=key[2],
                counts=tuple(
                    (stage.key, sum(job_id in memberships[stage.key] for job_id in job_ids))
                    for stage in stages
                ),
            )
            for key, job_ids in sorted(grouped.items(), key=lambda item: item[0])
        )

    @classmethod
    def _passes_without_title_include(cls, row: _JobRow, filters: MarketFilters) -> bool:
        counterfactual = filters.model_copy(update={"title_include": ()})
        scratch: dict[str, list[UUID]] = {key: [] for key, _ in _STAGES}
        passed, _ = cls._evaluate(row, counterfactual, scratch)
        return passed
