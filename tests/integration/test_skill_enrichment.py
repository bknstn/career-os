"""Opt-in PostgreSQL coverage for durable deterministic skill enrichment."""

from __future__ import annotations

import os
from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from career_os.application.enrichment import SkillEnrichmentService
from career_os.config.skills import load_skill_taxonomy
from career_os.db import (
    create_database_engine,
    create_session_factory,
    database_url_from_settings,
    session_scope,
)
from career_os.domain import CanonicalJobCandidate
from career_os.normalization.skills import SkillExtractor
from career_os.persistence.integration_safety import (
    CAREER_OS_TABLE_NAMES,
    assert_disposable_test_database,
    assert_initial_schema_is_absent,
)
from career_os.persistence.models import JobPosting, JobSkill, JobVersion, Source
from career_os.persistence.repositories import (
    CanonicalJobRepository,
    JobNotFoundError,
    JobUpsertAction,
)
from career_os.settings import load_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory() -> Generator[sessionmaker[Session], None, None]:
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
        yield create_session_factory(engine)
    finally:
        _return_database_to_empty(engine, alembic_config, upgraded)


def test_enrichment_stores_multiple_exact_evidence_rows_and_is_idempotent(
    session_factory: sessionmaker[Session],
) -> None:
    job_id = _seed_job(session_factory)
    service = _service(session_factory)

    first = service.enrich_job(job_id=job_id)
    second = service.enrich_job(job_id=job_id)

    assert (first.evidence_count, first.replaced) == (3, True)
    assert (second.evidence_count, second.replaced) == (3, False)
    with session_scope(session_factory) as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        assert job.taxonomy_version == "1.0.0"
        evidence = list(session.scalars(select(JobSkill).where(JobSkill.job_id == job_id)))
        assert sorted(
            (row.skill_id, row.matched_text, row.section, row.method, row.taxonomy_version)
            for row in evidence
        ) == [
            ("go", "Go", "other", "alias", "1.0.0"),
            ("python", "Python", "other", "alias", "1.0.0"),
            ("python", "Python", "title", "alias", "1.0.0"),
        ]
        title_evidence = next(
            row
            for row in evidence
            if row.skill_id == "python" and row.matched_text == "Python" and row.section == "title"
        )
        title_evidence.section = None

    repaired = service.enrich_job(job_id=job_id)

    assert (repaired.evidence_count, repaired.replaced) == (3, True)
    with session_scope(session_factory) as session:
        assert all(
            row.section is not None
            for row in session.scalars(select(JobSkill).where(JobSkill.job_id == job_id))
        )


def test_reenrichment_replaces_stale_deterministic_rows_and_preserves_manual_evidence(
    session_factory: sessionmaker[Session],
) -> None:
    job_id = _seed_job(session_factory)
    service = _service(session_factory)
    service.enrich_job(job_id=job_id)
    with session_scope(session_factory) as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        job.taxonomy_version = "0.9.0"
        session.add(
            JobSkill(
                job_id=job_id,
                skill_id="java",
                matched_text="Java",
                section="other",
                method="alias",
                confidence=Decimal("1.000"),
                taxonomy_version="0.9.0",
            )
        )
        session.add(
            JobSkill(
                job_id=job_id,
                skill_id="python",
                matched_text="reviewed Python evidence",
                section="other",
                method="manual",
                confidence=Decimal("1.000"),
                taxonomy_version="0.9.0",
            )
        )

    result = service.enrich_job(job_id=job_id)

    assert (result.evidence_count, result.replaced) == (3, True)
    with session_scope(session_factory) as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        assert job.taxonomy_version == "1.0.0"
        rows = list(session.scalars(select(JobSkill).where(JobSkill.job_id == job_id)))
        assert {(row.skill_id, row.method, row.taxonomy_version) for row in rows} == {
            ("go", "alias", "1.0.0"),
            ("python", "alias", "1.0.0"),
            ("python", "manual", "0.9.0"),
        }


def test_caller_transaction_rolls_back_evidence_and_taxonomy_version(
    session_factory: sessionmaker[Session],
) -> None:
    job_id = _seed_job(session_factory)
    service = _service(session_factory)
    session = session_factory()
    try:
        result = service.enrich_job_in_session(session=session, job_id=job_id)
        assert result.replaced is True
        session.rollback()
    finally:
        session.close()

    with session_scope(session_factory) as verification_session:
        job = verification_session.get(JobPosting, job_id)
        assert job is not None
        assert job.taxonomy_version == "none-v1"
        assert list(verification_session.scalars(select(JobSkill))) == []


def test_empty_evidence_is_idempotent_and_missing_jobs_fail_closed(
    session_factory: sessionmaker[Session],
) -> None:
    job_id = _seed_job(
        session_factory,
        title_raw="Generalist",
        description_text="Collaborate across teams without named technologies.",
    )
    service = _service(session_factory)

    first = service.enrich_job(job_id=job_id)
    second = service.enrich_job(job_id=job_id)

    assert (first.evidence_count, first.replaced) == (0, True)
    assert (second.evidence_count, second.replaced) == (0, False)
    with session_scope(session_factory) as session:
        assert list(session.scalars(select(JobSkill).where(JobSkill.job_id == job_id))) == []
    with pytest.raises(JobNotFoundError, match="not found"):
        service.enrich_job(job_id=uuid4())


def test_identical_source_reingestion_after_enrichment_keeps_current_taxonomy_marker(
    session_factory: sessionmaker[Session],
) -> None:
    job_id = _seed_job(session_factory)
    service = _service(session_factory)
    service.enrich_job(job_id=job_id)
    reobservation = _candidate(observed_at=datetime(2026, 8, 21, 11, tzinfo=UTC))

    with session_scope(session_factory) as session:
        result = CanonicalJobRepository(session).upsert(reobservation)

        assert result.action is JobUpsertAction.UNCHANGED
        job = session.get(JobPosting, job_id)
        assert job is not None
        assert job.taxonomy_version == "1.0.0"
        evidence_rows = list(session.scalars(select(JobSkill).where(JobSkill.job_id == job_id)))
        version_rows = list(session.scalars(select(JobVersion).where(JobVersion.job_id == job_id)))
        assert len(evidence_rows) == 3
        assert len(version_rows) == 1


def test_source_content_change_resets_taxonomy_marker_and_versions_actual_diff(
    session_factory: sessionmaker[Session],
) -> None:
    job_id = _seed_job(session_factory)
    service = _service(session_factory)
    service.enrich_job(job_id=job_id)
    changed = _candidate(
        observed_at=datetime(2026, 8, 21, 11, tzinfo=UTC),
        description_text="Build Python services as a Go developer using PostgreSQL.",
    )

    with session_scope(session_factory) as session:
        result = CanonicalJobRepository(session).upsert(changed)

        assert result.action is JobUpsertAction.UPDATED
        assert result.changed_fields == ("description_text", "taxonomy_version")
        job = session.get(JobPosting, job_id)
        assert job is not None
        assert job.taxonomy_version == "none-v1"
        versions = list(
            session.scalars(
                select(JobVersion)
                .where(JobVersion.job_id == job_id)
                .order_by(JobVersion.observed_at)
            )
        )
        assert len(versions) == 2
        assert versions[-1].content_hash == changed.content_hash
        assert versions[-1].changed_fields == ["description_text", "taxonomy_version"]
        assert {row.taxonomy_version for row in session.scalars(select(JobSkill))} == {"1.0.0"}


def _service(session_factory: sessionmaker[Session]) -> SkillEnrichmentService:
    taxonomy_path = Path(__file__).resolve().parents[2] / "config" / "taxonomy" / "skills.v1.yaml"
    return SkillEnrichmentService(
        session_factory=session_factory,
        extractor=SkillExtractor(load_skill_taxonomy(taxonomy_path)),
    )


def _seed_job(
    session_factory: sessionmaker[Session],
    *,
    title_raw: str = "Senior Python Engineer",
    description_text: str = "Build Python services as a Go developer.",
) -> UUID:
    with session_scope(session_factory) as session:
        source = Source(
            key="example",
            kind="greenhouse",
            company_name="Example Inc.",
            config={"board_token": "example"},
            enabled=True,
        )
        session.add(source)
        session.flush()
        result = CanonicalJobRepository(session).upsert(
            _candidate(
                observed_at=datetime(2026, 8, 21, 10, tzinfo=UTC),
                title_raw=title_raw,
                description_text=description_text,
            )
        )
        return result.job_id


def _candidate(
    *,
    observed_at: datetime,
    title_raw: str = "Senior Python Engineer",
    description_text: str = "Build Python services as a Go developer.",
    taxonomy_version: str = "none-v1",
) -> CanonicalJobCandidate:
    return CanonicalJobCandidate(
        source_key="example",
        source_kind="greenhouse",
        external_id="job-123",
        company_name="Example Inc.",
        title_raw=title_raw,
        title_normalized=title_raw,
        description_text=description_text,
        description_html=None,
        observed_at=observed_at,
        raw_payload={"id": "job-123"},
        normalizer_version="source-v1",
        taxonomy_version=taxonomy_version,
        source_url=None,
        apply_url=None,
        published_at=None,
    )


def _return_database_to_empty(engine: Engine, alembic_config: Config, upgraded: bool) -> None:
    try:
        if upgraded:
            command.downgrade(alembic_config, "base")
            with engine.begin() as connection:
                connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
            assert not (
                set(inspect(engine).get_table_names())
                & (CAREER_OS_TABLE_NAMES | {"alembic_version"})
            )
    finally:
        engine.dispose()
