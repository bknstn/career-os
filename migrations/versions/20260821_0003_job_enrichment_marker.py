"""Separate source mapper and deterministic enrichment version ownership.

Revision ID: 20260821_0003
Revises: 20260819_0002
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260821_0003"
down_revision: str | Sequence[str] | None = "20260819_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Mark existing postings stale for the first deterministic job enrichment."""
    op.add_column(
        "job_postings",
        sa.Column(
            "enrichment_normalizer_version",
            sa.String(),
            server_default=sa.text("'none-v1'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Remove the post-ingestion marker without touching immutable job history."""
    op.drop_column("job_postings", "enrichment_normalizer_version")
