"""Opt-in PostgreSQL verification of the initial Alembic revision."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from career_os.db import create_database_engine, database_url_from_settings
from career_os.persistence.integration_safety import (
    CAREER_OS_TABLE_NAMES,
    assert_disposable_test_database,
    assert_initial_schema_is_absent,
)
from career_os.settings import load_settings

pytestmark = pytest.mark.integration


def test_initial_revision_upgrades_inspects_and_downgrades() -> None:
    if os.environ.get("RUN_DB_INTEGRATION") != "1":
        pytest.skip("set RUN_DB_INTEGRATION=1 to run PostgreSQL integration tests")

    settings = load_settings()
    if settings.database_url is None:
        pytest.skip("set CAREER_OS_DATABASE_URL to run PostgreSQL integration tests")

    project_root = Path(__file__).resolve().parents[2]
    alembic_config = Config(str(project_root / "alembic.ini"))
    engine = create_database_engine(settings)
    upgraded = False

    try:
        assert_disposable_test_database(database_url_from_settings(settings))
        assert_initial_schema_is_absent(inspect(engine).get_table_names())
        command.upgrade(alembic_config, "head")
        upgraded = True

        inspector = inspect(engine)
        assert set(inspector.get_table_names()) >= CAREER_OS_TABLE_NAMES
        assert {
            "radar_panel_versions",
            "radar_scan_observations",
        } <= set(inspector.get_table_names())
        radar_run_columns = {column["name"] for column in inspector.get_columns("collection_runs")}
        assert {
            "radar_panel_version",
            "radar_manifest_digest",
            "radar_panel_digest",
        } <= radar_run_columns
        enrichment_marker = next(
            column
            for column in inspector.get_columns("job_postings")
            if column["name"] == "enrichment_normalizer_version"
        )
        assert enrichment_marker["nullable"] is False
        assert enrichment_marker["default"] is not None
        assert _nonunique_index_names(inspector.get_indexes("job_postings")) == {
            "ix_job_postings_status",
            "ix_job_postings_last_seen_at",
            "ix_job_postings_published_at",
            "ix_job_postings_company_name",
            "ix_job_postings_role_family",
            "ix_job_postings_seniority",
            "ix_job_postings_workplace_type",
        }
        assert ("uq_job_postings_source_external_id", ("source_id", "external_id")) in {
            (constraint["name"], tuple(constraint["column_names"]))
            for constraint in inspector.get_unique_constraints("job_postings")
        }
        assert _nonunique_index_names(inspector.get_indexes("job_skills")) == {
            "ix_job_skills_job_id_skill_id"
        }
        job_skills_primary_key = inspector.get_pk_constraint("job_skills")
        assert job_skills_primary_key["name"] == "pk_job_skills"
        assert job_skills_primary_key["constrained_columns"] == ["id"]

        command.downgrade(alembic_config, "20260821_0003")
        downgraded_inspector = inspect(engine)
        assert not (
            {"radar_panel_versions", "radar_scan_observations"}
            & set(downgraded_inspector.get_table_names())
        )
        assert not (
            {
                "radar_panel_version",
                "radar_manifest_digest",
                "radar_panel_digest",
            }
            & {column["name"] for column in downgraded_inspector.get_columns("collection_runs")}
        )
        command.upgrade(alembic_config, "head")
        reupgraded_inspector = inspect(engine)
        assert {"radar_panel_versions", "radar_scan_observations"} <= set(
            reupgraded_inspector.get_table_names()
        )
        assert {
            "radar_panel_version",
            "radar_manifest_digest",
            "radar_panel_digest",
        } <= {column["name"] for column in reupgraded_inspector.get_columns("collection_runs")}
    finally:
        try:
            if upgraded:
                command.downgrade(alembic_config, "base")
                # Alembic keeps an empty version table at base; remove that
                # framework artifact to return the dedicated test DB to empty.
                with engine.begin() as connection:
                    connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
                assert not (
                    set(inspect(engine).get_table_names())
                    & (CAREER_OS_TABLE_NAMES | {"alembic_version"})
                )
        finally:
            engine.dispose()


def _nonunique_index_names(indexes: list[dict[str, object]]) -> set[str]:
    """Exclude reflected unique constraints and their duplicate index entries."""
    return {
        str(index["name"])
        for index in indexes
        if not index.get("unique", False) and not index.get("duplicates_constraint")
    }
