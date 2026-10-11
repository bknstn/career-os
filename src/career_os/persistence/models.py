"""Typed PostgreSQL metadata for the Market Explorer persistence boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from career_os.domain.versions import NO_ENRICHMENT_NORMALIZER_VERSION


def utcnow() -> datetime:
    """Return a timezone-aware timestamp for SQLAlchemy-managed updates."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Base class for all persistence metadata."""


class AuditTimestampMixin:
    """Creation/update audit fields with UTC-aware application and DB defaults."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class Source(AuditTimestampMixin, Base):
    """A configured public ATS board and its validated non-secret settings."""

    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('greenhouse', 'lever', 'ashby', 'workable_public')",
            name="ck_sources_kind",
        ),
        PrimaryKeyConstraint("id", name="pk_sources"),
        UniqueConstraint("key", name="uq_sources_key"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    company_name: Mapped[str] = mapped_column(String, nullable=False)
    config: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
        nullable=False,
    )


class RadarPanelVersion(Base):
    """One immutable manifest and source-configuration identity for Radar capture."""

    __tablename__ = "radar_panel_versions"
    __table_args__ = (
        CheckConstraint(
            "manifest_digest ~ '^[0-9a-f]{64}$'",
            name="ck_radar_panel_versions_manifest_digest",
        ),
        CheckConstraint(
            "panel_digest ~ '^[0-9a-f]{64}$'",
            name="ck_radar_panel_versions_panel_digest",
        ),
        PrimaryKeyConstraint("panel_version", name="pk_radar_panel_versions"),
    )

    panel_version: Mapped[str] = mapped_column(String, primary_key=True)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    panel_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_manifest: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    source_config_digests: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class CollectionRun(Base):
    """A single attempted scan of one configured source."""

    __tablename__ = "collection_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed')",
            name="ck_collection_runs_status",
        ),
        CheckConstraint(
            "items_fetched >= 0 AND inserted >= 0 AND updated >= 0 AND unchanged >= 0 "
            "AND inactivated >= 0 AND failed_items >= 0 AND http_requests >= 0 "
            "AND retry_count >= 0 AND rate_limit_events >= 0 "
            "AND normalization_errors >= 0",
            name="ck_collection_runs_counts_nonnegative",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_collection_runs_finished_after_started",
        ),
        CheckConstraint(
            "(radar_panel_version IS NULL AND radar_manifest_digest IS NULL "
            "AND radar_panel_digest IS NULL) OR "
            "(radar_panel_version IS NOT NULL AND radar_manifest_digest IS NOT NULL "
            "AND radar_panel_digest IS NOT NULL)",
            name="ck_collection_runs_radar_panel_provenance_complete",
        ),
        Index(
            "ix_collection_runs_radar_panel_version_finished_at",
            "radar_panel_version",
            "finished_at",
        ),
        PrimaryKeyConstraint("id", name="pk_collection_runs"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    items_fetched: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    inserted: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    updated: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    unchanged: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    inactivated: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    failed_items: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    http_requests: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    rate_limit_events: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    normalization_errors: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    normalizer_version: Mapped[str] = mapped_column(String, nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(String, nullable=False)
    radar_panel_version: Mapped[str | None] = mapped_column(
        ForeignKey("radar_panel_versions.panel_version")
    )
    radar_manifest_digest: Mapped[str | None] = mapped_column(String(64))
    radar_panel_digest: Mapped[str | None] = mapped_column(String(64))
    error_summary: Mapped[str | None] = mapped_column(Text)
    incomplete_reason: Mapped[str | None] = mapped_column(Text)


class RadarScanObservation(Base):
    """Immutable per-run classifier inputs, independent of mutable job projections."""

    __tablename__ = "radar_scan_observations"
    __table_args__ = (
        CheckConstraint("length(external_id) > 0", name="ck_radar_scan_observations_external_id"),
        PrimaryKeyConstraint("run_id", "external_id", name="pk_radar_scan_observations"),
    )

    run_id: Mapped[UUID] = mapped_column(ForeignKey("collection_runs.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)


class JobPosting(AuditTimestampMixin, Base):
    """A canonical job posting and its latest observed source payload."""

    __tablename__ = "job_postings"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_job_postings"),
        UniqueConstraint("source_id", "external_id", name="uq_job_postings_source_external_id"),
        CheckConstraint("status IN ('active', 'inactive')", name="ck_job_postings_status"),
        CheckConstraint(
            "seniority IS NULL OR seniority IN "
            "('intern', 'junior', 'mid', 'senior', 'lead', 'staff', 'principal', "
            "'manager', 'director', 'executive')",
            name="ck_job_postings_seniority",
        ),
        CheckConstraint(
            "seniority_confidence IS NULL OR "
            "(seniority_confidence >= 0 AND seniority_confidence <= 1)",
            name="ck_job_postings_seniority_confidence",
        ),
        CheckConstraint(
            "employment_type IS NULL OR employment_type IN "
            "('full_time', 'part_time', 'contract', 'internship', 'temporary', 'other')",
            name="ck_job_postings_employment_type",
        ),
        CheckConstraint(
            "workplace_type IS NULL OR "
            "workplace_type IN ('onsite', 'hybrid', 'remote', 'unspecified')",
            name="ck_job_postings_workplace_type",
        ),
        CheckConstraint(
            "last_seen_at >= first_seen_at", name="ck_job_postings_last_seen_after_first"
        ),
        CheckConstraint(
            "inactive_at IS NULL OR inactive_at >= first_seen_at",
            name="ck_job_postings_inactive_after_first",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    company_name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    title_raw: Mapped[str] = mapped_column(Text, nullable=False)
    title_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    role_family: Mapped[str | None] = mapped_column(String, index=True)
    seniority: Mapped[str | None] = mapped_column(String, index=True)
    seniority_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    description_html: Mapped[str | None] = mapped_column(Text)
    description_text: Mapped[str] = mapped_column(Text, nullable=False)
    department_raw: Mapped[str | None] = mapped_column(Text)
    team_raw: Mapped[str | None] = mapped_column(Text)
    employment_type: Mapped[str | None] = mapped_column(String)
    workplace_type: Mapped[str | None] = mapped_column(String, index=True)
    remote_scope: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
        index=True,
    )
    inactive_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String,
        default="active",
        server_default=text("'active'"),
        nullable=False,
        index=True,
    )
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    raw_latest: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    normalizer_version: Mapped[str] = mapped_column(String, nullable=False)
    enrichment_normalizer_version: Mapped[str] = mapped_column(
        String,
        default=NO_ENRICHMENT_NORMALIZER_VERSION,
        server_default=text("'none-v1'"),
        nullable=False,
    )
    taxonomy_version: Mapped[str] = mapped_column(String, nullable=False)


class JobVersion(Base):
    """An immutable changed snapshot of a canonical job posting."""

    __tablename__ = "job_versions"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_job_versions"),
        UniqueConstraint("job_id", "content_hash", name="uq_job_versions_job_content_hash"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("job_postings.id"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    canonical_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    raw_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    changed_fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        default=list,
        server_default=text("ARRAY[]::text[]"),
        nullable=False,
    )


class JobLocation(Base):
    """A source-preserved location with optional normalized country metadata."""

    __tablename__ = "job_locations"
    __table_args__ = (
        PrimaryKeyConstraint("job_id", "ordinal", name="pk_job_locations"),
        CheckConstraint("ordinal >= 0", name="ck_job_locations_ordinal_nonnegative"),
        CheckConstraint(
            "country_code IS NULL OR country_code ~ '^[A-Z]{2}$'",
            name="ck_job_locations_country_code",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_job_locations_confidence",
        ),
    )

    job_id: Mapped[UUID] = mapped_column(ForeignKey("job_postings.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    raw: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str | None] = mapped_column(String)
    region: Mapped[str | None] = mapped_column(String)
    country_code: Mapped[str | None] = mapped_column(String(2))
    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)


class JobSkill(Base):
    """One skill evidence row; several rows may represent the same job and skill."""

    __tablename__ = "job_skills"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_job_skills"),
        Index("ix_job_skills_job_id_skill_id", "job_id", "skill_id"),
        CheckConstraint(
            "method IN ('alias', 'exact', 'pattern', 'manual', 'llm')",
            name="ck_job_skills_method",
        ),
        CheckConstraint(
            "section IS NULL OR section IN ('title', 'requirements', 'responsibilities', 'other')",
            name="ck_job_skills_section",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_job_skills_confidence"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("job_postings.id"), nullable=False)
    skill_id: Mapped[str] = mapped_column(String, nullable=False)
    matched_text: Mapped[str] = mapped_column(Text, nullable=False)
    section: Mapped[str | None] = mapped_column(String)
    method: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(String, nullable=False)


class JobCompensation(Base):
    """A compensation component preserved in its original currency and interval."""

    __tablename__ = "job_compensation"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_job_compensation"),
        CheckConstraint(
            "component_type IN ('base_salary', 'bonus', 'equity', 'other')",
            name="ck_job_compensation_component_type",
        ),
        CheckConstraint(
            "min_value IS NULL OR min_value >= 0",
            name="ck_job_compensation_min_nonnegative",
        ),
        CheckConstraint(
            "max_value IS NULL OR max_value >= 0",
            name="ck_job_compensation_max_nonnegative",
        ),
        CheckConstraint(
            "min_value IS NULL OR max_value IS NULL OR min_value <= max_value",
            name="ck_job_compensation_min_lte_max",
        ),
        CheckConstraint(
            "currency IS NULL OR currency ~ '^[A-Z]{3}$'",
            name="ck_job_compensation_currency",
        ),
        CheckConstraint(
            "interval IS NULL OR interval IN ('hour', 'day', 'month', 'year', 'none')",
            name="ck_job_compensation_interval",
        ),
        CheckConstraint(
            "source_method IN ('structured_source', 'text_parse')",
            name="ck_job_compensation_source_method",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_job_compensation_confidence",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("job_postings.id"), nullable=False)
    component_type: Mapped[str] = mapped_column(String, nullable=False)
    min_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    max_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    interval: Mapped[str | None] = mapped_column(String)
    location_tier: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text)
    source_method: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
