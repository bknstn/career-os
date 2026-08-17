"""Create the initial Market Explorer persistence schema.

Revision ID: 20260817_0001
Revises:
Create Date: 2026-08-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260817_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the source, lifecycle, canonical job, and extraction tables."""
    op.create_table(
        "sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("company_name", sa.String(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('greenhouse', 'lever', 'ashby', 'workable_public')",
            name="ck_sources_kind",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sources"),
        sa.UniqueConstraint("key", name="uq_sources_key"),
    )
    op.create_table(
        "collection_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("items_fetched", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("inserted", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("unchanged", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("inactivated", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failed_items", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("http_requests", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("normalizer_version", sa.String(), nullable=False),
        sa.Column("taxonomy_version", sa.String(), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed')",
            name="ck_collection_runs_status",
        ),
        sa.CheckConstraint(
            "items_fetched >= 0 AND inserted >= 0 AND updated >= 0 AND unchanged >= 0 "
            "AND inactivated >= 0 AND failed_items >= 0 AND http_requests >= 0 "
            "AND retry_count >= 0",
            name="ck_collection_runs_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_collection_runs_finished_after_started",
        ),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_collection_runs"),
    )
    op.create_table(
        "job_postings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("company_name", sa.String(), nullable=False),
        sa.Column("title_raw", sa.Text(), nullable=False),
        sa.Column("title_normalized", sa.Text(), nullable=False),
        sa.Column("role_family", sa.String(), nullable=True),
        sa.Column("seniority", sa.String(), nullable=True),
        sa.Column("seniority_confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("description_html", sa.Text(), nullable=True),
        sa.Column("description_text", sa.Text(), nullable=False),
        sa.Column("department_raw", sa.Text(), nullable=True),
        sa.Column("team_raw", sa.Text(), nullable=True),
        sa.Column("employment_type", sa.String(), nullable=True),
        sa.Column("workplace_type", sa.String(), nullable=True),
        sa.Column("remote_scope", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("apply_url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("inactive_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column(
            "raw_latest",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("normalizer_version", sa.String(), nullable=False),
        sa.Column("taxonomy_version", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_job_postings_status"),
        sa.CheckConstraint(
            "seniority IS NULL OR seniority IN "
            "('intern', 'junior', 'mid', 'senior', 'lead', 'staff', 'principal', "
            "'manager', 'director', 'executive')",
            name="ck_job_postings_seniority",
        ),
        sa.CheckConstraint(
            "seniority_confidence IS NULL OR "
            "(seniority_confidence >= 0 AND seniority_confidence <= 1)",
            name="ck_job_postings_seniority_confidence",
        ),
        sa.CheckConstraint(
            "employment_type IS NULL OR employment_type IN "
            "('full_time', 'part_time', 'contract', 'internship', 'temporary', 'other')",
            name="ck_job_postings_employment_type",
        ),
        sa.CheckConstraint(
            "workplace_type IS NULL OR "
            "workplace_type IN ('onsite', 'hybrid', 'remote', 'unspecified')",
            name="ck_job_postings_workplace_type",
        ),
        sa.CheckConstraint(
            "last_seen_at >= first_seen_at", name="ck_job_postings_last_seen_after_first"
        ),
        sa.CheckConstraint(
            "inactive_at IS NULL OR inactive_at >= first_seen_at",
            name="ck_job_postings_inactive_after_first",
        ),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_job_postings"),
        sa.UniqueConstraint("source_id", "external_id", name="uq_job_postings_source_external_id"),
    )
    op.create_index("ix_job_postings_status", "job_postings", ["status"], unique=False)
    op.create_index("ix_job_postings_last_seen_at", "job_postings", ["last_seen_at"], unique=False)
    op.create_index("ix_job_postings_published_at", "job_postings", ["published_at"], unique=False)
    op.create_index("ix_job_postings_company_name", "job_postings", ["company_name"], unique=False)
    op.create_index("ix_job_postings_role_family", "job_postings", ["role_family"], unique=False)
    op.create_index("ix_job_postings_seniority", "job_postings", ["seniority"], unique=False)
    op.create_index(
        "ix_job_postings_workplace_type", "job_postings", ["workplace_type"], unique=False
    )
    op.create_table(
        "job_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("canonical_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "changed_fields",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("ARRAY[]::text[]"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_job_versions"),
        sa.UniqueConstraint("job_id", "content_hash", name="uq_job_versions_job_content_hash"),
    )
    op.create_table(
        "job_locations",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("raw", sa.Text(), nullable=False),
        sa.Column("city", sa.String(), nullable=True),
        sa.Column("region", sa.String(), nullable=True),
        sa.Column("country_code", sa.String(length=2), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_job_locations_ordinal_nonnegative"),
        sa.CheckConstraint(
            "country_code IS NULL OR country_code ~ '^[A-Z]{2}$'",
            name="ck_job_locations_country_code",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_job_locations_confidence",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"]),
        sa.PrimaryKeyConstraint("job_id", "ordinal", name="pk_job_locations"),
    )
    op.create_table(
        "job_skills",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("skill_id", sa.String(), nullable=False),
        sa.Column("matched_text", sa.Text(), nullable=False),
        sa.Column("section", sa.String(), nullable=True),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("taxonomy_version", sa.String(), nullable=False),
        sa.CheckConstraint(
            "method IN ('alias', 'exact', 'pattern', 'manual', 'llm')",
            name="ck_job_skills_method",
        ),
        sa.CheckConstraint(
            "section IS NULL OR section IN ('title', 'requirements', 'responsibilities', 'other')",
            name="ck_job_skills_section",
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_job_skills_confidence"),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_job_skills"),
    )
    op.create_index(
        "ix_job_skills_job_id_skill_id", "job_skills", ["job_id", "skill_id"], unique=False
    )
    op.create_table(
        "job_compensation",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("component_type", sa.String(), nullable=False),
        sa.Column("min_value", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("max_value", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("interval", sa.String(), nullable=True),
        sa.Column("location_tier", sa.Text(), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("source_method", sa.String(), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.CheckConstraint(
            "component_type IN ('base_salary', 'bonus', 'equity', 'other')",
            name="ck_job_compensation_component_type",
        ),
        sa.CheckConstraint(
            "min_value IS NULL OR min_value >= 0",
            name="ck_job_compensation_min_nonnegative",
        ),
        sa.CheckConstraint(
            "max_value IS NULL OR max_value >= 0",
            name="ck_job_compensation_max_nonnegative",
        ),
        sa.CheckConstraint(
            "min_value IS NULL OR max_value IS NULL OR min_value <= max_value",
            name="ck_job_compensation_min_lte_max",
        ),
        sa.CheckConstraint(
            "currency IS NULL OR currency ~ '^[A-Z]{3}$'",
            name="ck_job_compensation_currency",
        ),
        sa.CheckConstraint(
            "interval IS NULL OR interval IN ('hour', 'day', 'month', 'year', 'none')",
            name="ck_job_compensation_interval",
        ),
        sa.CheckConstraint(
            "source_method IN ('structured_source', 'text_parse')",
            name="ck_job_compensation_source_method",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_job_compensation_confidence",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_job_compensation"),
    )


def downgrade() -> None:
    """Drop dependent extraction tables before canonical jobs and sources."""
    op.drop_table("job_compensation")
    op.drop_index("ix_job_skills_job_id_skill_id", table_name="job_skills")
    op.drop_table("job_skills")
    op.drop_table("job_locations")
    op.drop_table("job_versions")
    op.drop_index("ix_job_postings_workplace_type", table_name="job_postings")
    op.drop_index("ix_job_postings_seniority", table_name="job_postings")
    op.drop_index("ix_job_postings_role_family", table_name="job_postings")
    op.drop_index("ix_job_postings_company_name", table_name="job_postings")
    op.drop_index("ix_job_postings_published_at", table_name="job_postings")
    op.drop_index("ix_job_postings_last_seen_at", table_name="job_postings")
    op.drop_index("ix_job_postings_status", table_name="job_postings")
    op.drop_table("job_postings")
    op.drop_table("collection_runs")
    op.drop_table("sources")
