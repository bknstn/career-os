"""Unit tests for PostgreSQL metadata without a database connection."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID

from career_os.persistence.models import (
    Base,
    CollectionRun,
    JobCompensation,
    JobLocation,
    JobPosting,
    JobSkill,
    RadarPanelVersion,
    RadarScanObservation,
    Source,
)


def test_initial_metadata_contains_only_the_planned_tables() -> None:
    assert set(Base.metadata.tables) == {
        "sources",
        "collection_runs",
        "job_postings",
        "job_versions",
        "job_locations",
        "job_skills",
        "job_compensation",
        "radar_panel_versions",
        "radar_scan_observations",
    }


def test_postgresql_specific_columns_and_uuid_defaults_are_declared() -> None:
    sources = Source.__table__
    job_versions = Base.metadata.tables["job_versions"]

    assert isinstance(sources.c.id.type, PostgreSQLUUID)
    assert sources.c.id.default is not None
    assert isinstance(sources.c.id.default.arg(None), UUID)
    assert sources.primary_key.name == "pk_sources"
    assert isinstance(sources.c.config.type, JSONB)
    assert isinstance(job_versions.c.canonical_snapshot.type, JSONB)
    assert isinstance(job_versions.c.changed_fields.type, ARRAY)


def test_radar_capture_metadata_keeps_panel_identity_and_observations_independent() -> None:
    panel_versions = RadarPanelVersion.__table__
    observations = RadarScanObservation.__table__

    assert panel_versions.primary_key.columns.keys() == ["panel_version"]
    assert isinstance(panel_versions.c.canonical_manifest.type, JSONB)
    assert isinstance(panel_versions.c.source_config_digests.type, JSONB)
    assert CollectionRun.__table__.c.radar_panel_version.nullable is True
    assert {
        (foreign_key.parent.name, foreign_key.target_fullname)
        for foreign_key in observations.foreign_keys
    } == {("run_id", "collection_runs.id")}
    assert list(observations.primary_key.columns.keys()) == ["run_id", "external_id"]
    assert {"title", "description", "observed_at"} <= set(observations.c.keys())


def test_numeric_columns_preserve_decimal_values() -> None:
    numeric_columns = [
        JobPosting.__table__.c.seniority_confidence,
        JobLocation.__table__.c.confidence,
        JobSkill.__table__.c.confidence,
        JobCompensation.__table__.c.min_value,
        JobCompensation.__table__.c.max_value,
        JobCompensation.__table__.c.confidence,
    ]

    assert all(
        isinstance(column.type, Numeric) and column.type.asdecimal for column in numeric_columns
    )
    assert JobPosting.__annotations__["seniority_confidence"] == "Mapped[Decimal | None]"
    assert JobLocation.__annotations__["confidence"] == "Mapped[Decimal]"
    assert JobSkill.__annotations__["confidence"] == "Mapped[Decimal]"
    assert JobCompensation.__annotations__["min_value"] == "Mapped[Decimal | None]"


def test_constraints_capture_lifecycle_and_closed_vocabularies() -> None:
    job_postings = JobPosting.__table__
    constraint_names = {
        constraint.name
        for constraint in job_postings.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert {
        "ck_job_postings_status",
        "ck_job_postings_seniority",
        "ck_job_postings_seniority_confidence",
        "ck_job_postings_employment_type",
        "ck_job_postings_workplace_type",
        "ck_job_postings_last_seen_after_first",
        "ck_job_postings_inactive_after_first",
    } <= constraint_names


def test_job_posting_indexes_match_the_initial_query_plan() -> None:
    index_names = {index.name for index in JobPosting.__table__.indexes}

    assert index_names == {
        "ix_job_postings_status",
        "ix_job_postings_last_seen_at",
        "ix_job_postings_published_at",
        "ix_job_postings_company_name",
        "ix_job_postings_role_family",
        "ix_job_postings_seniority",
        "ix_job_postings_workplace_type",
    }


def test_locations_use_the_required_composite_primary_key_and_safe_foreign_key() -> None:
    locations = JobLocation.__table__
    foreign_key = next(iter(locations.foreign_keys))

    assert list(locations.primary_key.columns.keys()) == ["job_id", "ordinal"]
    assert foreign_key.ondelete is None
    assert locations.c.confidence.nullable is False


def test_skill_evidence_supports_multiple_rows_for_one_job_and_skill() -> None:
    skills = JobSkill.__table__
    primary_key_columns = list(skills.primary_key.columns.keys())
    unique_constraints = {
        tuple(constraint.columns.keys())
        for constraint in skills.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert primary_key_columns == ["id"]
    assert ("job_id", "skill_id") not in unique_constraints
    assert {index.name for index in skills.indexes} == {"ix_job_skills_job_id_skill_id"}
    assert "ck_job_skills_section" in {
        constraint.name
        for constraint in skills.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_audit_and_server_defaults_are_present() -> None:
    source_columns = Source.__table__.c

    assert source_columns.created_at.server_default is not None
    assert source_columns.updated_at.server_default is not None
    assert source_columns.enabled.server_default is not None
    assert "enrichment_normalizer_version" in JobPosting.__table__.c
