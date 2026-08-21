"""Explicit stable request and response contracts for the read-only API."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from career_os.config.markets import MarketFilters
from career_os.config.profiles import Profile
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


class BucketResponse(ApiModel):
    value: str
    job_ids: tuple[UUID, ...]
    denominator_job_ids: tuple[UUID, ...]
    job_count: int
    denominator: int
    job_share: Decimal


class CompensationAggregateResponse(ApiModel):
    component_type: str
    currency: str
    interval: str
    location_tier: str | None
    component_ids: tuple[UUID, ...]
    job_ids: tuple[UUID, ...]
    denominator_job_ids: tuple[UUID, ...]
    component_count: int
    job_count: int
    denominator: int
    job_share: Decimal
    min_value: Decimal | None
    max_value: Decimal | None


class MarketAggregatesResponse(ApiModel):
    market_name: str
    matching_job_ids: tuple[UUID, ...]
    skill_denominator_job_ids: tuple[UUID, ...]
    compensation_job_ids: tuple[UUID, ...]
    matching_job_count: int
    company_count: int
    compensation_coverage: Decimal
    collection_cutoff: datetime | None
    taxonomy_version: str
    normalizer_version: str
    companies: tuple[BucketResponse, ...]
    skills: tuple[BucketResponse, ...]
    seniorities: tuple[BucketResponse, ...]
    workplace_types: tuple[BucketResponse, ...]
    countries: tuple[BucketResponse, ...]
    compensation: tuple[CompensationAggregateResponse, ...]


class GapThresholdsResponse(ApiModel):
    min_job_count: int
    min_job_share: Decimal


class SkillGapResponse(ApiModel):
    skill_id: str
    demand: BucketResponse
    rank_score: float


class JobSkillCoverageResponse(ApiModel):
    job_id: UUID
    in_skill_denominator: bool
    mentioned_skill_ids: tuple[str, ...]
    covered_skill_ids: tuple[str, ...]
    gap_skill_ids: tuple[str, ...]
    mentioned_skill_count: int
    covered_skill_count: int
    skill_coverage: Decimal | None


class ProfileComparisonResponse(ApiModel):
    profile_name: str
    market_name: str
    matching_job_ids: tuple[UUID, ...]
    skill_denominator_job_ids: tuple[UUID, ...]
    demanded_skills: tuple[BucketResponse, ...]
    covered_skills: tuple[BucketResponse, ...]
    neutral_profile_skill_ids: tuple[str, ...]
    gaps: tuple[SkillGapResponse, ...]
    gap_thresholds: GapThresholdsResponse
    job_skill_coverage: tuple[JobSkillCoverageResponse, ...]
    representative_jobs: tuple[JobSkillCoverageResponse, ...]


class ReportJobEvidenceResponse(ApiModel):
    job_id: UUID
    company_name: str
    title: str
    source_url: str | None


class SkillLabelResponse(ApiModel):
    skill_id: str
    label: str


class MarketReportResponse(ApiModel):
    filters: MarketFilters
    profile: Profile
    market: MarketAggregatesResponse
    comparison: ProfileComparisonResponse
    jobs: tuple[ReportJobEvidenceResponse, ...]
    skill_labels: tuple[SkillLabelResponse, ...]


class JobFilters(ApiModel):
    status: Literal["active", "inactive"] | None = None
    company: str | None = None
    role_family: RoleFamily | None = None
    seniority: Seniority | None = None
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    skill: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
