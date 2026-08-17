"""Pure fail-closed tests for the opt-in PostgreSQL integration target guard."""

from __future__ import annotations

import pytest

from career_os.persistence.integration_safety import (
    UnsafeIntegrationDatabaseError,
    assert_disposable_test_database,
    assert_initial_schema_is_absent,
)


def test_accepts_only_database_names_ending_in_test() -> None:
    assert_disposable_test_database(
        "postgresql+psycopg://career_os:dev-only@localhost:5432/career_os_test"
    )

    with pytest.raises(UnsafeIntegrationDatabaseError, match="ending in '_test'"):
        assert_disposable_test_database(
            "postgresql+psycopg://career_os:dev-only@localhost:5432/career_os"
        )


def test_rejects_existing_career_os_or_alembic_tables() -> None:
    assert_initial_schema_is_absent({"unrelated_table"})

    with pytest.raises(UnsafeIntegrationDatabaseError, match="sources"):
        assert_initial_schema_is_absent({"sources"})

    with pytest.raises(UnsafeIntegrationDatabaseError, match="alembic_version"):
        assert_initial_schema_is_absent({"alembic_version"})
