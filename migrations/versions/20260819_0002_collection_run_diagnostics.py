"""Persist source-neutral collection diagnostics.

Revision ID: 20260819_0002
Revises: 20260817_0001
Create Date: 2026-08-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0002"
down_revision: str | Sequence[str] | None = "20260817_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COUNTS_CHECK = (
    "items_fetched >= 0 AND inserted >= 0 AND updated >= 0 AND unchanged >= 0 "
    "AND inactivated >= 0 AND failed_items >= 0 AND http_requests >= 0 "
    "AND retry_count >= 0 AND rate_limit_events >= 0 AND normalization_errors >= 0"
)
_OLD_COUNTS_CHECK = (
    "items_fetched >= 0 AND inserted >= 0 AND updated >= 0 AND unchanged >= 0 "
    "AND inactivated >= 0 AND failed_items >= 0 AND http_requests >= 0 AND retry_count >= 0"
)


def upgrade() -> None:
    """Add durable partial reasons and source-neutral scan counters."""
    op.add_column("collection_runs", sa.Column("incomplete_reason", sa.Text(), nullable=True))
    op.add_column(
        "collection_runs",
        sa.Column("rate_limit_events", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "collection_runs",
        sa.Column(
            "normalization_errors", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.drop_constraint("ck_collection_runs_counts_nonnegative", "collection_runs")
    op.create_check_constraint(
        "ck_collection_runs_counts_nonnegative", "collection_runs", _COUNTS_CHECK
    )


def downgrade() -> None:
    """Restore the initial run schema without silently retaining diagnostics."""
    op.drop_constraint("ck_collection_runs_counts_nonnegative", "collection_runs")
    op.drop_column("collection_runs", "normalization_errors")
    op.drop_column("collection_runs", "rate_limit_events")
    op.drop_column("collection_runs", "incomplete_reason")
    op.create_check_constraint(
        "ck_collection_runs_counts_nonnegative", "collection_runs", _OLD_COUNTS_CHECK
    )
