"""Retain source provenance and extraction without storing new response bodies.

Revision ID: 20261107_0007
Revises: 20261019_0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261107_0007"
down_revision: str | Sequence[str] | None = "20261019_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "research_document_versions"
_CHECK = "ck_research_document_versions_body_representation"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("original_size_bytes", sa.Integer(), nullable=True))
    op.alter_column(_TABLE, "raw_body", existing_type=sa.LargeBinary(), nullable=True)
    op.create_check_constraint(
        _CHECK,
        _TABLE,
        "(raw_body IS NOT NULL AND original_size_bytes IS NULL) OR "
        "(raw_body IS NULL AND original_size_bytes IS NOT NULL AND "
        "original_size_bytes BETWEEN 1 AND 104857600)",
    )


def downgrade() -> None:
    # This fails atomically if extraction-only rows exist. Fabricating a body
    # or deleting version history to satisfy the previous schema is unsafe.
    op.alter_column(_TABLE, "raw_body", existing_type=sa.LargeBinary(), nullable=False)
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.drop_column(_TABLE, "original_size_bytes")
