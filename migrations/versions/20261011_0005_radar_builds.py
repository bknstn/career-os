"""Persist immutable roles-only Radar build results.

Revision ID: 20261011_0005
Revises: 20261011_0004
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261011_0005"
down_revision: str | Sequence[str] | None = "20261011_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the atomic immutable result store."""
    op.create_table(
        "radar_builds",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotence_key", sa.String(length=64), nullable=False),
        sa.Column("panel_version", sa.String(), nullable=False),
        sa.Column("panel_digest", sa.String(length=64), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("cutoff_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "idempotence_key ~ '^[0-9a-f]{64}$'",
            name="ck_radar_builds_idempotence_key",
        ),
        sa.CheckConstraint(
            "input_digest ~ '^[0-9a-f]{64}$'",
            name="ck_radar_builds_input_digest",
        ),
        sa.ForeignKeyConstraint(
            ["panel_version"],
            ["radar_panel_versions.panel_version"],
            name="fk_radar_builds_panel_version_radar_panel_versions",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_radar_builds"),
        sa.UniqueConstraint("idempotence_key", name="uq_radar_builds_idempotence_key"),
    )
    op.create_index(
        "ix_radar_builds_panel_version_cutoff",
        "radar_builds",
        ["panel_version", "cutoff_utc"],
    )


def downgrade() -> None:
    """Remove persisted build snapshots without changing captured scan evidence."""
    op.drop_index("ix_radar_builds_panel_version_cutoff", table_name="radar_builds")
    op.drop_table("radar_builds")
