"""Typed PostgreSQL metadata for the Market Explorer persistence boundary."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
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


class RadarBuild(Base):
    """One immutable, replayable roles-only Radar result."""

    __tablename__ = "radar_builds"
    __table_args__ = (
        CheckConstraint(
            "idempotence_key ~ '^[0-9a-f]{64}$'",
            name="ck_radar_builds_idempotence_key",
        ),
        CheckConstraint(
            "input_digest ~ '^[0-9a-f]{64}$'",
            name="ck_radar_builds_input_digest",
        ),
        UniqueConstraint("idempotence_key", name="uq_radar_builds_idempotence_key"),
        Index("ix_radar_builds_panel_version_cutoff", "panel_version", "cutoff_utc"),
        PrimaryKeyConstraint("id", name="pk_radar_builds"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    idempotence_key: Mapped[str] = mapped_column(String(64), nullable=False)
    panel_version: Mapped[str] = mapped_column(
        ForeignKey("radar_panel_versions.panel_version"), nullable=False
    )
    panel_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    cutoff_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class ResearchCollectionRun(Base):
    """Immutable registry snapshot and outcome summary for one discovery run."""

    __tablename__ = "research_collection_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed')",
            name="ck_research_collection_runs_status",
        ),
        CheckConstraint(
            "registry_digest ~ '^[0-9a-f]{64}$'",
            name="ck_research_collection_runs_registry_digest",
        ),
        CheckConstraint(
            "trigger IN ('daily', 'manual', 'backfill')",
            name="ck_research_collection_runs_trigger",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_research_collection_runs_finished_after_started",
        ),
        PrimaryKeyConstraint("id", name="pk_research_collection_runs"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    registry_version: Mapped[str] = mapped_column(String, nullable=False)
    registry_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    trigger: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    acquired: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    failed: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )


class ResearchAcquisitionAttempt(Base):
    """Visible result for one bounded source request, including failures."""

    __tablename__ = "research_acquisition_attempts"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('discovered', 'acquired', 'http_error', 'network_error', 'blocked', "
            "'oversize', 'unsupported_content', 'parse_error')",
            name="ck_research_acquisition_attempts_outcome",
        ),
        CheckConstraint("bytes_received >= 0", name="ck_research_acquisition_attempts_bytes"),
        CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="ck_research_acquisition_attempts_http_status",
        ),
        Index("ix_research_acquisition_attempts_run_source", "run_id", "source_key"),
        PrimaryKeyConstraint("id", name="pk_research_acquisition_attempts"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "research_collection_runs.id", ondelete="CASCADE", name="fk_research_attempt_run"
        ),
        nullable=False,
    )
    source_key: Mapped[str] = mapped_column(String, nullable=False)
    requested_url: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    http_status: Mapped[int | None] = mapped_column(Integer)
    bytes_received: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String)


class ResearchCandidate(Base):
    """Discovered URL metadata kept separate from acquired evidence."""

    __tablename__ = "research_candidates"
    __table_args__ = (
        CheckConstraint(
            "state IN ('discovered', 'acquired', 'ignored')",
            name="ck_research_candidates_state",
        ),
        UniqueConstraint("source_key", "canonical_url", name="uq_research_candidates_source_url"),
        Index("ix_research_candidates_state_seen", "state", "last_seen_at"),
        PrimaryKeyConstraint("id", name="pk_research_candidates"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    source_key: Mapped[str] = mapped_column(String, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    discovered_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(
        String, default="discovered", server_default="discovered", nullable=False
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ResearchDocument(Base):
    """Stable canonical publication identity independent of any one body version."""

    __tablename__ = "research_documents"
    __table_args__ = (
        UniqueConstraint("source_key", "canonical_url", name="uq_research_documents_source_url"),
        PrimaryKeyConstraint("id", name="pk_research_documents"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    source_key: Mapped[str] = mapped_column(String, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class ResearchDocumentVersion(Base):
    """Append-only original response bytes plus extraction and date provenance."""

    __tablename__ = "research_document_versions"
    __table_args__ = (
        CheckConstraint(
            "raw_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_document_versions_raw_digest",
        ),
        CheckConstraint(
            "normalized_text_sha256 IS NULL OR normalized_text_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_document_versions_text_digest",
        ),
        CheckConstraint(
            "media_type IN ('text/html', 'application/pdf')",
            name="ck_research_document_versions_media_type",
        ),
        CheckConstraint(
            "version_relation IN ('initial', 'equivalent_text', 'revision')",
            name="ck_research_document_versions_relation",
        ),
        CheckConstraint(
            "(version_relation = 'initial' AND previous_version_id IS NULL) OR "
            "(version_relation <> 'initial' AND previous_version_id IS NOT NULL)",
            name="ck_research_document_versions_previous_relation",
        ),
        CheckConstraint(
            "octet_length(raw_body) BETWEEN 1 AND 52428800",
            name="ck_research_document_versions_body_size",
        ),
        CheckConstraint(
            "(publication_date_state = 'reported' AND publication_date IS NOT NULL) OR "
            "(publication_date_state <> 'reported' AND publication_date IS NULL)",
            name="ck_research_document_versions_publication_date_disclosure",
        ),
        CheckConstraint(
            "publication_date_state IN ('reported', 'not_reported', 'not_applicable', 'unclear')",
            name="ck_research_document_versions_publication_date_state",
        ),
        CheckConstraint(
            "data_period_state IN ('reported', 'not_reported', 'not_applicable', 'unclear')",
            name="ck_research_document_versions_period_state",
        ),
        CheckConstraint(
            "(data_period_state = 'reported' AND "
            "(data_period_start IS NOT NULL OR data_period_end IS NOT NULL)) OR "
            "(data_period_state <> 'reported' AND "
            "data_period_start IS NULL AND data_period_end IS NULL)",
            name="ck_research_document_versions_period_disclosure",
        ),
        CheckConstraint(
            "data_period_start IS NULL OR data_period_end IS NULL OR "
            "data_period_start <= data_period_end",
            name="ck_research_document_versions_period_order",
        ),
        Index("ix_research_document_versions_document_digest", "document_id", "raw_sha256"),
        Index("ix_research_document_versions_document_retrieved", "document_id", "retrieved_at"),
        UniqueConstraint(
            "document_id", "version_number", name="uq_research_document_versions_number"
        ),
        PrimaryKeyConstraint("id", name="pk_research_document_versions"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "research_documents.id", ondelete="CASCADE", name="fk_research_version_document"
        ),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "research_document_versions.id",
            ondelete="RESTRICT",
            name="fk_research_version_previous",
        )
    )
    discovered_url: Mapped[str] = mapped_column(Text, nullable=False)
    retrieved_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    publication_date: Mapped[date | None] = mapped_column(Date)
    publication_date_state: Mapped[str] = mapped_column(String, nullable=False)
    data_period_start: Mapped[date | None] = mapped_column(Date)
    data_period_end: Mapped[date | None] = mapped_column(Date)
    data_period_state: Mapped[str] = mapped_column(String, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    response_etag: Mapped[str | None] = mapped_column(String)
    response_last_modified: Mapped[str | None] = mapped_column(String)
    media_type: Mapped[str] = mapped_column(String, nullable=False)
    raw_body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    raw_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_text: Mapped[str | None] = mapped_column(Text)
    normalized_text_sha256: Mapped[str | None] = mapped_column(String(64))
    extraction_version: Mapped[str] = mapped_column(String, nullable=False)
    extraction_regions: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    extraction_error: Mapped[str | None] = mapped_column(Text)
    version_relation: Mapped[str] = mapped_column(String, nullable=False)


class ResearchClaim(Base):
    """Immutable structured annotation candidate bound to one body version."""

    __tablename__ = "research_claims"
    __table_args__ = (
        CheckConstraint(
            "question IN ('developer_tasks', 'experience_seniority', 'production_ai_skills')",
            name="ck_research_claims_question",
        ),
        CheckConstraint(
            "evidence_kind IN ('observed', 'survey', 'experiment', 'forecast', 'model_estimate', "
            "'case_study', 'secondary_synthesis')",
            name="ck_research_claims_evidence_kind",
        ),
        CheckConstraint(
            "lineage_kind IN ('primary', 'replication', 'derivative', 'unknown')",
            name="ck_research_claims_lineage_kind",
        ),
        CheckConstraint(
            "idempotence_key ~ '^[0-9a-f]{64}$'",
            name="ck_research_claims_idempotence_key",
        ),
        UniqueConstraint("idempotence_key", name="uq_research_claims_idempotence_key"),
        Index("ix_research_claims_version_question", "document_version_id", "question"),
        PrimaryKeyConstraint("id", name="pk_research_claims"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "research_document_versions.id",
            ondelete="RESTRICT",
            name="fk_research_claim_version",
        ),
        nullable=False,
    )
    idempotence_key: Mapped[str] = mapped_column(String(64), nullable=False)
    question: Mapped[str] = mapped_column(String, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    exact_quote: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    evidence_kind: Mapped[str] = mapped_column(String, nullable=False)
    evidence_details: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    lineage_kind: Mapped[str] = mapped_column(String, nullable=False)
    upstream_dataset_or_document: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class ResearchClaimReview(Base):
    """Append-only human review event for a claim or version transition."""

    __tablename__ = "research_claim_reviews"
    __table_args__ = (
        CheckConstraint(
            "state IN ('candidate', 'accepted', 'rejected', 'revision_review_required', "
            "'superseded', 'retracted')",
            name="ck_research_claim_reviews_state",
        ),
        Index("ix_research_claim_reviews_claim_reviewed", "claim_id", "reviewed_at"),
        UniqueConstraint("event_sequence", name="uq_research_claim_reviews_event_sequence"),
        PrimaryKeyConstraint("id", name="pk_research_claim_reviews"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    event_sequence: Mapped[int] = mapped_column(BigInteger, Identity(always=False), nullable=False)
    claim_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_claims.id", ondelete="RESTRICT", name="fk_research_review_claim"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(String, nullable=False)
    reviewer: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    target_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "research_document_versions.id",
            ondelete="RESTRICT",
            name="fk_research_review_target_version",
        )
    )
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


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
