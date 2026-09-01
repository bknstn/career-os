"""Explicit stable request and response contracts for the read-only API."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from career_os.config.markets import MarketFilters
from career_os.config.profiles import Profile
from career_os.config.target_universes import TargetUniverse
from career_os.domain.normalization import EmploymentType, RoleFamily, Seniority


class ApiModel(BaseModel):
    """Shared strict API serialization policy."""

    model_config = ConfigDict(extra="forbid")


class LiveResponse(ApiModel):
    status: Literal["live"] = "live"


class ReadyResponse(ApiModel):
    status: Literal["ready"] = "ready"


class ErrorResponse(ApiModel):
    detail: str


class SourceResponse(ApiModel):
    id: UUID
    key: str
    kind: Literal["greenhouse", "lever", "ashby", "workable_public"]
    company_name: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class RunCountsResponse(ApiModel):
    items_fetched: int
    inserted: int
    updated: int
    unchanged: int
    inactivated: int
    failed_items: int
    http_requests: int
    retry_count: int
    rate_limit_events: int
    normalization_errors: int


class RunResponse(ApiModel):
    id: UUID
    source_id: UUID
    source_key: str
    source_kind: Literal["greenhouse", "lever", "ashby", "workable_public"]
    status: Literal["running", "succeeded", "partial", "failed"]
    started_at: datetime
    finished_at: datetime | None
    counts: RunCountsResponse
    normalizer_version: str
    taxonomy_version: str
    error_summary: str | None
    incomplete_reason: str | None


class JobSummaryResponse(ApiModel):
    id: UUID
    source_id: UUID
    source_key: str
    source_kind: Literal["greenhouse", "lever", "ashby", "workable_public"]
    external_id: str
    company_name: str
    title: str
    role_family: RoleFamily | None
    seniority: Seniority | None
    workplace_type: Literal["onsite", "hybrid", "remote", "unspecified"] | None
    status: Literal["active", "inactive"]
    source_url: str | None
    apply_url: str | None
    published_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    inactive_at: datetime | None


class JobPageResponse(ApiModel):
    items: tuple[JobSummaryResponse, ...]
    next_cursor: str | None
    order: Literal["last_seen_at_desc_id_asc"] = "last_seen_at_desc_id_asc"


class JobLocationResponse(ApiModel):
    ordinal: int
    city: str | None
    region: str | None
    country_code: str | None
    is_primary: bool
    confidence: Decimal


class JobSkillEvidenceResponse(ApiModel):
    id: UUID
    skill_id: str
    matched_text: str
    section: Literal["title", "requirements", "responsibilities", "other"] | None
    method: Literal["alias", "exact", "pattern", "manual", "llm"]
    confidence: Decimal
    taxonomy_version: str


class JobCompensationResponse(ApiModel):
    id: UUID
    component_type: Literal["base_salary", "bonus", "equity", "other"]
    min_value: Decimal | None
    max_value: Decimal | None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    interval: Literal["hour", "day", "month", "year", "none"] | None
    location_tier: str | None
    source_method: Literal["structured_source", "text_parse"]
    confidence: Decimal


class JobVersionResponse(ApiModel):
    id: UUID
    observed_at: datetime
    content_hash: str
    changed_fields: tuple[str, ...]


class JobDetailResponse(JobSummaryResponse):
    title_normalized: str
    seniority_confidence: Decimal | None
    description_html: str | None
    description_text: str
    department_raw: str | None
    team_raw: str | None
    employment_type: EmploymentType | None
    remote_scope: str | None
    content_hash: str
    normalizer_version: str
    enrichment_normalizer_version: str
    taxonomy_version: str
    created_at: datetime
    updated_at: datetime
    locations: tuple[JobLocationResponse, ...]
    skill_evidence: tuple[JobSkillEvidenceResponse, ...]
    compensation: tuple[JobCompensationResponse, ...]
    versions: tuple[JobVersionResponse, ...]


class MarketReportRequest(ApiModel):
    market: MarketFilters
    profile: Profile
    quality_review: QualityReviewRequest | None = None


class QualityReviewRequest(ApiModel):
    ref: str = Field(min_length=1)
    reviewed_job_ids: tuple[UUID, ...] = ()
    false_positive_job_ids: tuple[UUID, ...] = ()

    @field_validator("ref")
    @classmethod
    def nonblank_ref(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("ref must be non-blank")
        return value

    @field_validator("reviewed_job_ids", "false_positive_job_ids")
    @classmethod
    def sorted_unique_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("review IDs must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_subset(self) -> QualityReviewRequest:
        if not set(self.false_positive_job_ids) <= set(self.reviewed_job_ids):
            raise ValueError("false_positive_job_ids must be a subset of reviewed_job_ids")
        return self


MarketReportRequest.model_rebuild()


class TargetUniverseReportRequest(ApiModel):
    """H2 uses an explicit universe, not a generic market-filter request."""

    target_universe: TargetUniverse
    profile: Profile
    quality_review: QualityReviewRequest | None = None


class HybridScopeResponse(ApiModel):
    kind: Literal["configured_source_panel"]
    name: str
    target_universe: None = None


class HybridClaimPolicyResponse(ApiModel):
    opportunities: Literal["available"]
    panel_observations: Literal["panel_bounded", "qualified"]
    broad_market_claims: Literal["disabled"]
    skill_semantics: Literal["mentions_only"]
    fit_probability: Literal["not_provided"]


class HybridProvenanceResponse(ApiModel):
    kind: Literal["result_derived", "contract_rule", "review_artifact"]
    ref: str
    reviewed_job_ids: tuple[UUID, ...]


class HybridEvaluationResponse(ApiModel):
    basis: Literal["computed", "contract_invariant", "manual_review"]
    provenance: HybridProvenanceResponse


class HybridGateResponse(ApiModel):
    gate_id: str
    status: Literal["passed", "failed", "not_evaluable"]
    operator: str
    threshold: int | float | str | bool | None
    observed: int | float | str | bool | None
    unit: str
    evidence_job_ids: tuple[UUID, ...]
    evidence_company_names: tuple[str, ...]


class HybridObservedValuesResponse(ApiModel):
    matching_job_count: int
    company_count: int
    max_company_concentration: float | None
    reviewed_eligible_job_count: int
    represented_company_count: int
    demonstrated_eligibility_false_positive_count: int
    skill_semantics: Literal["mentions_only"]
    compensation_reconciled: bool
    source_coverage: float | None
    compensation_coverage: float | None
    collection_cutoff: datetime | None


class HybridQualityResponse(ApiModel):
    state: Literal["accepted", "evidence_limited"]
    claim_policy: HybridClaimPolicyResponse
    gates: tuple[HybridGateResponse, ...]
    failed_gates: tuple[str, ...]
    evaluation_provenance: dict[str, HybridEvaluationResponse]
    observed_values: HybridObservedValuesResponse


class HybridSourceResponse(ApiModel):
    key: str
    kind: str
    url: str | None


class HybridFreshnessResponse(ApiModel):
    first_seen_at: datetime
    last_seen_at: datetime


class HybridLocationResponse(ApiModel):
    ordinal: int
    city: str | None
    region: str | None
    country_code: str | None
    is_primary: bool
    confidence: float


class HybridSkillMentionResponse(ApiModel):
    id: UUID
    skill_id: str
    matched_text: str
    section: Literal["title", "requirements", "responsibilities", "other"] | None
    method: Literal["alias", "exact", "pattern", "manual", "llm"]
    confidence: float
    taxonomy_version: str


class HybridJobCompensationResponse(ApiModel):
    component_id: UUID
    component_type: str
    min_value: float | None
    max_value: float | None
    currency: str | None
    interval: str | None
    location_tier: str | None
    source_method: str


class HybridOpportunityResponse(ApiModel):
    job_id: UUID
    company_name: str
    title: str
    source: HybridSourceResponse
    freshness: HybridFreshnessResponse
    locations: tuple[HybridLocationResponse, ...]
    remote_scope: str | None
    skill_mentions: tuple[HybridSkillMentionResponse, ...]
    compensation: tuple[HybridJobCompensationResponse, ...]
    status: Literal["active", "inactive"]
    role_family: str | None
    seniority: str | None
    workplace_type: str | None


class HybridOpportunitiesResponse(ApiModel):
    job_ids: tuple[UUID, ...]
    count: int
    company_count: int
    items: tuple[HybridOpportunityResponse, ...]


class HybridConcentrationResponse(ApiModel):
    company_name: str
    job_ids: tuple[UUID, ...]
    denominator_job_ids: tuple[UUID, ...]
    job_count: int
    job_share: float | None


class HybridSourceJobsResponse(ApiModel):
    source_key: str
    job_ids: tuple[UUID, ...]


class HybridSourceCoverageResponse(ApiModel):
    configured_source_count: int
    configured_source_keys: tuple[str, ...]
    matching_source_count: int
    matching_source_keys: tuple[str, ...]
    source_share: float | None
    job_ids_by_source: tuple[HybridSourceJobsResponse, ...]


class HybridSampleResponse(ApiModel):
    job_ids: tuple[UUID, ...]
    job_count: int
    company_names: tuple[str, ...]
    company_count: int
    max_company_concentration: HybridConcentrationResponse | None
    source_coverage: HybridSourceCoverageResponse
    collection_cutoff: datetime | None
    skill_denominator_job_ids: tuple[UUID, ...]
    compensation_job_ids: tuple[UUID, ...]


class HybridBucketResponse(ApiModel):
    value: str
    job_ids: tuple[UUID, ...]
    denominator_job_ids: tuple[UUID, ...]
    job_count: int
    job_share: float | None


class HybridSkillResponse(ApiModel):
    skill_id: str
    label: str
    semantics: Literal["mention"]
    job_ids: tuple[UUID, ...]
    job_count: int
    job_share: float | None
    denominator_job_ids: tuple[UUID, ...]
    company_names: tuple[str, ...]
    company_count: int
    company_share: float | None
    denominator_company_names: tuple[str, ...]


class HybridCompensationResponse(ApiModel):
    component_type: str
    currency: str
    interval: str
    location_tier: str | None
    component_ids: tuple[UUID, ...]
    job_ids: tuple[UUID, ...]
    denominator_job_ids: tuple[UUID, ...]
    component_count: int
    job_count: int
    job_share: float | None
    min_value: float | None
    max_value: float | None


class HybridProfileMentionsResponse(ApiModel):
    semantics: Literal["mention_only"]
    profile_skill_ids: tuple[str, ...]
    covered_skill_ids: tuple[str, ...]
    neutral_skill_ids: tuple[str, ...]
    uncovered_mentions: tuple[HybridSkillResponse, ...]


class HybridObservationsResponse(ApiModel):
    scope: Literal["configured_source_panel"]
    sample: HybridSampleResponse
    companies: tuple[HybridBucketResponse, ...]
    skills: tuple[HybridSkillResponse, ...]
    seniorities: tuple[HybridBucketResponse, ...]
    workplace_types: tuple[HybridBucketResponse, ...]
    countries: tuple[HybridBucketResponse, ...]
    compensation: tuple[HybridCompensationResponse, ...]
    profile_mentions: HybridProfileMentionsResponse


class MarketReportResponse(ApiModel):
    contract_version: Literal["h1.hybrid-result.v1"]
    scope: HybridScopeResponse
    filters: MarketFilters
    profile: Profile
    quality: HybridQualityResponse
    opportunities: HybridOpportunitiesResponse
    observations: HybridObservationsResponse


class TargetUniverseScopeResponse(ApiModel):
    kind: Literal["target_universe"]
    name: str
    target_universe: TargetUniverse


class TargetUniverseObservationsResponse(ApiModel):
    scope: Literal["target_universe_configured_source_panel"]
    sample: HybridSampleResponse
    companies: tuple[HybridBucketResponse, ...]
    skills: tuple[HybridSkillResponse, ...]
    seniorities: tuple[HybridBucketResponse, ...]
    workplace_types: tuple[HybridBucketResponse, ...]
    countries: tuple[HybridBucketResponse, ...]
    compensation: tuple[HybridCompensationResponse, ...]
    profile_mentions: HybridProfileMentionsResponse


class TargetUniverseClaimPolicyResponse(ApiModel):
    opportunities: Literal["available"]
    target_universe_observations: Literal["bounded", "qualified"]
    broad_market_claims: Literal["disabled"]
    skill_semantics: Literal["mentions_only"]
    fit_probability: Literal["not_provided"]


class TargetUniverseObservedValuesResponse(ApiModel):
    matching_opportunity_count: int
    source_panel_source_count: int
    reviewed_eligible_opportunity_count: int
    demonstrated_eligibility_false_positive_count: int
    target_universe_boundary_resolved: bool
    opportunity_evidence_reconciled: bool
    skill_semantics: Literal["mentions_only"]
    compensation_reconciled: bool
    source_coverage: float | None
    compensation_coverage: float | None
    collection_cutoff: datetime | None


class TargetUniverseQualityResponse(ApiModel):
    state: Literal["accepted", "evidence_limited"]
    claim_policy: TargetUniverseClaimPolicyResponse
    gates: tuple[HybridGateResponse, ...]
    failed_gates: tuple[str, ...]
    evaluation_provenance: dict[str, HybridEvaluationResponse]
    observed_values: TargetUniverseObservedValuesResponse


class TargetUniverseReportResponse(ApiModel):
    contract_version: Literal["h2.target-universe.v1"]
    scope: TargetUniverseScopeResponse
    profile: Profile
    quality: TargetUniverseQualityResponse
    opportunities: HybridOpportunitiesResponse
    observations: TargetUniverseObservationsResponse


class JobFilters(ApiModel):
    status: Literal["active", "inactive"] | None = None
    company: str | None = None
    role_family: RoleFamily | None = None
    seniority: Seniority | None = None
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    skill: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
