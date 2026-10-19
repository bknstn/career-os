"""Add the append-only research evidence stream.

Revision ID: 20261019_0006
Revises: 20261011_0005
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261019_0006"
down_revision: str | Sequence[str] | None = "20261011_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create research-only tables without altering jobs or Radar tables."""
    op.create_table(
        "research_collection_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("registry_version", sa.String(), nullable=False),
        sa.Column("registry_digest", sa.String(length=64), nullable=False),
        sa.Column("registry_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discovered", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("acquired", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed')",
            name="ck_research_collection_runs_status",
        ),
        sa.CheckConstraint(
            "registry_digest ~ '^[0-9a-f]{64}$'",
            name="ck_research_collection_runs_registry_digest",
        ),
        sa.CheckConstraint(
            "trigger IN ('daily', 'manual', 'backfill')",
            name="ck_research_collection_runs_trigger",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_research_collection_runs_finished_after_started",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_research_collection_runs"),
    )
    op.create_table(
        "research_acquisition_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("requested_url", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("bytes_received", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('discovered', 'acquired', 'http_error', 'network_error', 'blocked', "
            "'oversize', 'unsupported_content', 'parse_error')",
            name="ck_research_acquisition_attempts_outcome",
        ),
        sa.CheckConstraint("bytes_received >= 0", name="ck_research_acquisition_attempts_bytes"),
        sa.CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="ck_research_acquisition_attempts_http_status",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["research_collection_runs.id"],
            name="fk_research_attempt_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_research_acquisition_attempts"),
    )
    op.create_index(
        "ix_research_acquisition_attempts_run_source",
        "research_acquisition_attempts",
        ["run_id", "source_key"],
    )
    op.create_table(
        "research_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("discovered_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("state", sa.String(), server_default="discovered", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('discovered', 'acquired', 'ignored')",
            name="ck_research_candidates_state",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_research_candidates"),
        sa.UniqueConstraint(
            "source_key", "canonical_url", name="uq_research_candidates_source_url"
        ),
    )
    op.create_index(
        "ix_research_candidates_state_seen", "research_candidates", ["state", "last_seen_at"]
    )
    op.create_table(
        "research_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_research_documents"),
        sa.UniqueConstraint("source_key", "canonical_url", name="uq_research_documents_source_url"),
    )
    op.create_table(
        "research_document_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("previous_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("discovered_url", sa.Text(), nullable=False),
        sa.Column("retrieved_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("publication_date", sa.Date(), nullable=True),
        sa.Column("publication_date_state", sa.String(), nullable=False),
        sa.Column("data_period_start", sa.Date(), nullable=True),
        sa.Column("data_period_end", sa.Date(), nullable=True),
        sa.Column("data_period_state", sa.String(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("response_etag", sa.String(), nullable=True),
        sa.Column("response_last_modified", sa.String(), nullable=True),
        sa.Column("media_type", sa.String(), nullable=False),
        sa.Column("raw_body", sa.LargeBinary(), nullable=False),
        sa.Column("raw_sha256", sa.String(length=64), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=True),
        sa.Column("normalized_text_sha256", sa.String(length=64), nullable=True),
        sa.Column("extraction_version", sa.String(), nullable=False),
        sa.Column("extraction_regions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("extraction_error", sa.Text(), nullable=True),
        sa.Column("version_relation", sa.String(), nullable=False),
        sa.CheckConstraint(
            "raw_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_document_versions_raw_digest",
        ),
        sa.CheckConstraint(
            "normalized_text_sha256 IS NULL OR normalized_text_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_document_versions_text_digest",
        ),
        sa.CheckConstraint(
            "media_type IN ('text/html', 'application/pdf')",
            name="ck_research_document_versions_media_type",
        ),
        sa.CheckConstraint(
            "version_relation IN ('initial', 'equivalent_text', 'revision')",
            name="ck_research_document_versions_relation",
        ),
        sa.CheckConstraint(
            "(version_relation = 'initial' AND previous_version_id IS NULL) OR "
            "(version_relation <> 'initial' AND previous_version_id IS NOT NULL)",
            name="ck_research_document_versions_previous_relation",
        ),
        sa.CheckConstraint(
            "octet_length(raw_body) BETWEEN 1 AND 52428800",
            name="ck_research_document_versions_body_size",
        ),
        sa.CheckConstraint(
            "(publication_date_state = 'reported' AND publication_date IS NOT NULL) OR "
            "(publication_date_state <> 'reported' AND publication_date IS NULL)",
            name="ck_research_document_versions_publication_date_disclosure",
        ),
        sa.CheckConstraint(
            "publication_date_state IN ('reported', 'not_reported', 'not_applicable', 'unclear')",
            name="ck_research_document_versions_publication_date_state",
        ),
        sa.CheckConstraint(
            "data_period_state IN ('reported', 'not_reported', 'not_applicable', 'unclear')",
            name="ck_research_document_versions_period_state",
        ),
        sa.CheckConstraint(
            "(data_period_state = 'reported' AND "
            "(data_period_start IS NOT NULL OR data_period_end IS NOT NULL)) OR "
            "(data_period_state <> 'reported' AND "
            "data_period_start IS NULL AND data_period_end IS NULL)",
            name="ck_research_document_versions_period_disclosure",
        ),
        sa.CheckConstraint(
            "data_period_start IS NULL OR data_period_end IS NULL OR "
            "data_period_start <= data_period_end",
            name="ck_research_document_versions_period_order",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["research_documents.id"],
            name="fk_research_version_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["previous_version_id"],
            ["research_document_versions.id"],
            name="fk_research_version_previous",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_research_document_versions"),
        sa.UniqueConstraint(
            "document_id", "version_number", name="uq_research_document_versions_number"
        ),
    )
    op.create_index(
        "ix_research_document_versions_document_digest",
        "research_document_versions",
        ["document_id", "raw_sha256"],
    )
    op.create_index(
        "ix_research_document_versions_document_retrieved",
        "research_document_versions",
        ["document_id", "retrieved_at"],
    )
    op.create_table(
        "research_claims",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotence_key", sa.String(length=64), nullable=False),
        sa.Column("question", sa.String(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("exact_quote", sa.Text(), nullable=False),
        sa.Column("location", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence_kind", sa.String(), nullable=False),
        sa.Column("evidence_details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("lineage_kind", sa.String(), nullable=False),
        sa.Column("upstream_dataset_or_document", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "question IN ('developer_tasks', 'experience_seniority', 'production_ai_skills')",
            name="ck_research_claims_question",
        ),
        sa.CheckConstraint(
            "evidence_kind IN ('observed', 'survey', 'experiment', 'forecast', 'model_estimate', "
            "'case_study', 'secondary_synthesis')",
            name="ck_research_claims_evidence_kind",
        ),
        sa.CheckConstraint(
            "lineage_kind IN ('primary', 'replication', 'derivative', 'unknown')",
            name="ck_research_claims_lineage_kind",
        ),
        sa.CheckConstraint(
            "idempotence_key ~ '^[0-9a-f]{64}$'",
            name="ck_research_claims_idempotence_key",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["research_document_versions.id"],
            name="fk_research_claim_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_research_claims"),
        sa.UniqueConstraint("idempotence_key", name="uq_research_claims_idempotence_key"),
    )
    op.create_index(
        "ix_research_claims_version_question",
        "research_claims",
        ["document_version_id", "question"],
    )
    op.create_table(
        "research_claim_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_sequence", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("reviewer", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("target_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('candidate', 'accepted', 'rejected', 'revision_review_required', "
            "'superseded', 'retracted')",
            name="ck_research_claim_reviews_state",
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["research_claims.id"],
            name="fk_research_review_claim",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_version_id"],
            ["research_document_versions.id"],
            name="fk_research_review_target_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_research_claim_reviews"),
        sa.UniqueConstraint("event_sequence", name="uq_research_claim_reviews_event_sequence"),
    )
    op.create_index(
        "ix_research_claim_reviews_claim_reviewed",
        "research_claim_reviews",
        ["claim_id", "reviewed_at"],
    )
    op.execute(
        "CREATE FUNCTION research_prevent_evidence_mutation() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION 'research evidence records are append-only'; "
        "END; $$"
    )
    for table_name in (
        "research_document_versions",
        "research_claims",
        "research_claim_reviews",
    ):
        op.execute(
            f"CREATE TRIGGER tr_{table_name}_immutable BEFORE UPDATE OR DELETE ON {table_name} "
            "FOR EACH ROW EXECUTE FUNCTION research_prevent_evidence_mutation()"
        )


def downgrade() -> None:
    """Drop research tables in dependency order; leave the jobs schema untouched."""
    op.drop_index("ix_research_claim_reviews_claim_reviewed", table_name="research_claim_reviews")
    op.drop_table("research_claim_reviews")
    op.drop_index("ix_research_claims_version_question", table_name="research_claims")
    op.drop_table("research_claims")
    op.drop_index(
        "ix_research_document_versions_document_retrieved", table_name="research_document_versions"
    )
    op.drop_index(
        "ix_research_document_versions_document_digest", table_name="research_document_versions"
    )
    op.drop_table("research_document_versions")
    op.drop_table("research_documents")
    op.drop_index("ix_research_candidates_state_seen", table_name="research_candidates")
    op.drop_table("research_candidates")
    op.drop_index(
        "ix_research_acquisition_attempts_run_source", table_name="research_acquisition_attempts"
    )
    op.drop_table("research_acquisition_attempts")
    op.drop_table("research_collection_runs")
    op.execute("DROP FUNCTION research_prevent_evidence_mutation()")
