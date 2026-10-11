"""Add immutable Radar panel identities and per-complete-scan inputs.

Revision ID: 20261011_0004
Revises: 20260821_0003
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261011_0004"
down_revision: str | Sequence[str] | None = "20260821_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Freeze panel identity and attach immutable inputs to successful scans."""
    op.create_table(
        "radar_panel_versions",
        sa.Column("panel_version", sa.String(), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("panel_digest", sa.String(length=64), nullable=False),
        sa.Column("canonical_manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_config_digests", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "manifest_digest ~ '^[0-9a-f]{64}$'",
            name="ck_radar_panel_versions_manifest_digest",
        ),
        sa.CheckConstraint(
            "panel_digest ~ '^[0-9a-f]{64}$'",
            name="ck_radar_panel_versions_panel_digest",
        ),
        sa.PrimaryKeyConstraint("panel_version", name="pk_radar_panel_versions"),
    )
    op.add_column("collection_runs", sa.Column("radar_panel_version", sa.String(), nullable=True))
    op.add_column(
        "collection_runs", sa.Column("radar_manifest_digest", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "collection_runs", sa.Column("radar_panel_digest", sa.String(length=64), nullable=True)
    )
    op.create_check_constraint(
        "ck_collection_runs_radar_panel_provenance_complete",
        "collection_runs",
        "(radar_panel_version IS NULL AND radar_manifest_digest IS NULL "
        "AND radar_panel_digest IS NULL) OR "
        "(radar_panel_version IS NOT NULL AND radar_manifest_digest IS NOT NULL "
        "AND radar_panel_digest IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_collection_runs_radar_panel_version_radar_panel_versions",
        "collection_runs",
        "radar_panel_versions",
        ["radar_panel_version"],
        ["panel_version"],
    )
    op.create_index(
        "ix_collection_runs_radar_panel_version_finished_at",
        "collection_runs",
        ["radar_panel_version", "finished_at"],
    )
    op.create_table(
        "radar_scan_observations",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(external_id) > 0", name="ck_radar_scan_observations_external_id"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["collection_runs.id"]),
        sa.PrimaryKeyConstraint("run_id", "external_id", name="pk_radar_scan_observations"),
    )


def downgrade() -> None:
    """Remove capture tables and provenance without touching source evidence."""
    op.drop_table("radar_scan_observations")
    op.drop_index(
        "ix_collection_runs_radar_panel_version_finished_at", table_name="collection_runs"
    )
    op.drop_constraint(
        "fk_collection_runs_radar_panel_version_radar_panel_versions",
        "collection_runs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_collection_runs_radar_panel_provenance_complete",
        "collection_runs",
        type_="check",
    )
    op.drop_column("collection_runs", "radar_panel_digest")
    op.drop_column("collection_runs", "radar_manifest_digest")
    op.drop_column("collection_runs", "radar_panel_version")
    op.drop_table("radar_panel_versions")
