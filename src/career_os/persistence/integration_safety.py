"""Fail-closed guards for explicitly enabled destructive schema test flows."""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy.engine import make_url

CAREER_OS_TABLE_NAMES = frozenset(
    {
        "sources",
        "collection_runs",
        "job_postings",
        "job_versions",
        "job_locations",
        "job_skills",
        "job_compensation",
    }
)


class UnsafeIntegrationDatabaseError(ValueError):
    """Raised before an integration test could mutate an unsafe database."""


def assert_disposable_test_database(database_url: str) -> None:
    """Require a dedicated database whose name explicitly ends in ``_test``."""
    database_name = make_url(database_url).database
    if database_name is None or not database_name.endswith("_test"):
        raise UnsafeIntegrationDatabaseError(
            "PostgreSQL integration tests require a dedicated database name ending in '_test'."
        )


def assert_initial_schema_is_absent(table_names: Collection[str]) -> None:
    """Refuse to mutate a target that already contains Career OS schema state."""
    protected_tables = CAREER_OS_TABLE_NAMES | {"alembic_version"}
    existing_tables = protected_tables.intersection(table_names)
    if existing_tables:
        joined_tables = ", ".join(sorted(existing_tables))
        raise UnsafeIntegrationDatabaseError(
            f"Refusing integration migration against an existing Career OS schema: {joined_tables}."
        )
