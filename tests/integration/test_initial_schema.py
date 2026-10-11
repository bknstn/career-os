"""Opt-in PostgreSQL verification of the initial Alembic revision."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

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
        command.upgrade(alembic_config, "20260821_0003")
        upgraded = True
        legacy_source_id, legacy_run_id = _seed_legacy_source_and_run(engine)
        command.upgrade(alembic_config, "head")

        inspector = inspect(engine)
        assert set(inspector.get_table_names()) >= CAREER_OS_TABLE_NAMES
        assert {
            "radar_panel_versions",
            "radar_scan_observations",
            "radar_builds",
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
            {"radar_panel_versions", "radar_scan_observations", "radar_builds"}
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
        with engine.connect() as connection:
            legacy_run = connection.execute(
                text("SELECT source_id, status FROM collection_runs WHERE id = :run_id"),
                {"run_id": legacy_run_id},
            ).one()
            assert legacy_run.source_id == legacy_source_id
            assert legacy_run.status == "succeeded"
        command.upgrade(alembic_config, "head")
        reupgraded_inspector = inspect(engine)
        assert {"radar_panel_versions", "radar_scan_observations", "radar_builds"} <= set(
            reupgraded_inspector.get_table_names()
        )
        assert {
            "radar_panel_version",
            "radar_manifest_digest",
            "radar_panel_digest",
        } <= {column["name"] for column in reupgraded_inspector.get_columns("collection_runs")}
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM collection_runs WHERE id = :run_id"),
                    {"run_id": legacy_run_id},
                )
                == 1
            )
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


def _seed_legacy_source_and_run(engine, *, now=None):
    """Insert a pre-capture source/run row while the database is at revision 0003."""
    from datetime import UTC, datetime

    source_id = uuid4()
    run_id = uuid4()
    seeded_at = now or datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sources "
                "(id, key, kind, company_name, config, enabled, created_at, updated_at) "
                "VALUES (:id, :key, :kind, :company_name, CAST(:config AS jsonb), "
                ":enabled, :created_at, :updated_at)"
            ),
            {
                "id": source_id,
                "key": "legacy-upgrade-source",
                "kind": "greenhouse",
                "company_name": "Legacy Example",
                "config": '{"key":"legacy-upgrade-source","kind":"greenhouse",'
                '"company_name":"Legacy Example","board_token":"fixture"}',
                "enabled": True,
                "created_at": seeded_at,
                "updated_at": seeded_at,
            },
        )
        connection.execute(
            text(
                "INSERT INTO collection_runs "
                "(id, source_id, status, started_at, finished_at, normalizer_version, "
                "taxonomy_version) VALUES (:id, :source_id, 'succeeded', :started_at, "
                ":finished_at, 'legacy-normalizer', 'legacy-taxonomy')"
            ),
            {
                "id": run_id,
                "source_id": source_id,
                "started_at": seeded_at,
                "finished_at": seeded_at,
            },
        )
    return source_id, run_id
