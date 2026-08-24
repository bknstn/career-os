"""Opt-in PostgreSQL coverage for durable deterministic skill enrichment."""

from __future__ import annotations

import json
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
from career_os.application.job_enrichment import JobEnrichmentService
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
from career_os.persistence.models import (
    JobCompensation,
    JobLocation,
    JobPosting,
    JobSkill,
    JobVersion,
    Source,
)
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
        assert job.taxonomy_version == "1.0.2"
        evidence = list(session.scalars(select(JobSkill).where(JobSkill.job_id == job_id)))
        assert sorted(
            (row.skill_id, row.matched_text, row.section, row.method, row.taxonomy_version)
            for row in evidence
        ) == [
            ("go", "Go", "other", "alias", "1.0.2"),
            ("python", "Python", "other", "alias", "1.0.2"),
            ("python", "Python", "title", "alias", "1.0.2"),
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
        assert job.taxonomy_version == "1.0.2"
        rows = list(session.scalars(select(JobSkill).where(JobSkill.job_id == job_id)))
        assert {(row.skill_id, row.method, row.taxonomy_version) for row in rows} == {
            ("go", "alias", "1.0.2"),
            ("python", "alias", "1.0.2"),
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
        assert job.taxonomy_version == "1.0.2"
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
        title_raw="Staff Data Engineer",
        description_text="Build Python services as a Go developer using PostgreSQL.",
    )

    with session_scope(session_factory) as session:
        result = CanonicalJobRepository(session).upsert(changed)

        assert result.action is JobUpsertAction.UPDATED
        assert result.changed_fields == (
            "title_raw",
            "title_normalized",
            "description_text",
            "taxonomy_version",
        )
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
        assert versions[-1].changed_fields == [
            "title_raw",
            "title_normalized",
            "description_text",
            "taxonomy_version",
        ]
        assert {row.taxonomy_version for row in session.scalars(select(JobSkill))} == {"1.0.2"}

    reenriched = _job_service(session_factory).enrich_stale()
    assert (reenriched.selected, reenriched.enriched, reenriched.failed_ids) == (1, 1, ())
    assert _job_service(session_factory).enrich_stale().selected == 0
    with session_scope(session_factory) as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        assert (job.enrichment_normalizer_version, job.taxonomy_version) == (
            "job-normalization-v4",
            "1.0.2",
        )
        assert (job.title_normalized, job.role_family, job.seniority) == (
            "Staff Data Engineer",
            "data_engineering",
            "staff",
        )
        assert {
            row.skill_id
            for row in session.scalars(select(JobSkill).where(JobSkill.job_id == job_id))
        } == {"go", "python", "postgresql"}


def test_raw_only_reingestion_marks_job_enrichment_stale_without_source_version_churn(
    session_factory: sessionmaker[Session],
) -> None:
    job_id = _seed_job(session_factory)
    service = _job_service(session_factory)

    first = service.enrich_stale()
    second = service.enrich_stale()
    assert (first.selected, first.enriched, first.failed_ids) == (1, 1, ())
    assert (second.selected, second.enriched, second.unchanged, second.failed_ids) == (0, 0, 0, ())

    reobservation = _candidate(
        observed_at=datetime(2026, 8, 21, 11, tzinfo=UTC),
        raw_payload={"id": "job-123", "location": {"name": "Warsaw, Poland"}},
    )
    with session_scope(session_factory) as session:
        result = CanonicalJobRepository(session).upsert(reobservation)
        job = session.get(JobPosting, job_id)
        assert result.action is JobUpsertAction.UNCHANGED
        assert job is not None
        assert job.normalizer_version == "source-v1"
        assert job.enrichment_normalizer_version == "none-v1"
        assert job.taxonomy_version == "none-v1"

    repaired = _job_service(session_factory).enrich_stale()
    assert (repaired.selected, repaired.enriched, repaired.failed_ids) == (1, 1, ())
    assert _job_service(session_factory).enrich_stale().selected == 0
    with session_scope(session_factory) as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        assert (job.enrichment_normalizer_version, job.taxonomy_version) == (
            "job-normalization-v4",
            "1.0.2",
        )
        assert [
            (row.raw, row.country_code)
            for row in session.scalars(select(JobLocation).where(JobLocation.job_id == job_id))
        ] == [("Warsaw, Poland", "PL")]


def test_bulk_enrichment_persists_ashby_normalization_skills_and_markers_without_source_churn(
    session_factory: sessionmaker[Session],
) -> None:
    job_id = _seed_ashby_job(session_factory)

    first = _job_service(session_factory).enrich_stale()
    second = _job_service(session_factory).enrich_stale()
    assert (first.selected, first.enriched, first.failed_ids) == (1, 1, ())
    assert (second.selected, second.enriched, second.unchanged, second.failed_ids) == (0, 0, 0, ())
    with session_scope(session_factory) as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        assert (
            job.normalizer_version,
            job.enrichment_normalizer_version,
            job.taxonomy_version,
        ) == ("ashby-v1", "job-normalization-v4", "1.0.2")
        assert (
            job.role_family,
            job.seniority,
            job.employment_type,
            job.workplace_type,
            job.remote_scope,
        ) == ("data_engineering", "senior", "full_time", "remote", "Poland")
        assert [
            (row.ordinal, row.raw, row.country_code)
            for row in session.scalars(
                select(JobLocation)
                .where(JobLocation.job_id == job_id)
                .order_by(JobLocation.ordinal)
            )
        ] == [(0, "Warsaw, Poland", "PL"), (1, "Remote - Poland", "PL")]
        assert [
            (
                row.component_type,
                row.min_value,
                row.max_value,
                row.currency,
                row.interval,
                row.location_tier,
            )
            for row in session.scalars(
                select(JobCompensation)
                .where(JobCompensation.job_id == job_id)
                .order_by(JobCompensation.component_type)
            )
        ] == [
            (
                "base_salary",
                Decimal("300000.00"),
                Decimal("420000.00"),
                "PLN",
                "year",
                "Senior Data Engineer",
            ),
            ("equity", Decimal("0.01"), Decimal("0.05"), None, "none", "Senior Data Engineer"),
        ]
        assert any(
            row.skill_id == "python"
            for row in session.scalars(select(JobSkill).where(JobSkill.job_id == job_id))
        )


def test_bulk_failure_isolation_keeps_malformed_job_stale_and_commits_peer(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        source = Source(
            key="batch", kind="greenhouse", company_name="Batch", config={}, enabled=True
        )
        session.add(source)
        session.flush()
        good = (
            CanonicalJobRepository(session)
            .upsert(_batch_candidate("good", {"location": {"name": "Warsaw, Poland"}}))
            .job_id
        )
        bad = (
            CanonicalJobRepository(session)
            .upsert(_batch_candidate("bad", {"location": "malformed"}))
            .job_id
        )
    result = _job_service(session_factory).enrich_stale()
    assert result.selected == 2 and result.enriched == 1 and result.failed_ids == (bad,)
    again = _job_service(session_factory).enrich_stale()
    assert (again.selected, again.enriched, again.failed_ids) == (1, 0, (bad,))
    with session_scope(session_factory) as session:
        valid = session.get(JobPosting, good)
        malformed = session.get(JobPosting, bad)
        assert valid is not None and valid.enrichment_normalizer_version == "job-normalization-v4"
        assert malformed is not None
        assert (malformed.enrichment_normalizer_version, malformed.taxonomy_version) == (
            "none-v1",
            "none-v1",
        )
        assert list(session.scalars(select(JobLocation).where(JobLocation.job_id == bad))) == []
        assert (
            list(session.scalars(select(JobCompensation).where(JobCompensation.job_id == bad)))
            == []
        )
        assert list(session.scalars(select(JobSkill).where(JobSkill.job_id == bad))) == []


def test_taxonomy_only_stale_reprocessing_preserves_normalization_and_manual_rows(
    session_factory: sessionmaker[Session],
) -> None:
    job_id = _seed_ashby_job(session_factory)
    _job_service(session_factory).enrich_stale()
    with session_scope(session_factory) as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        assert job.enrichment_normalizer_version == "job-normalization-v4"
        location_keys = tuple(
            (row.job_id, row.ordinal)
            for row in session.scalars(
                select(JobLocation)
                .where(JobLocation.job_id == job_id)
                .order_by(JobLocation.ordinal)
            )
        )
        compensation_ids = {
            row.id
            for row in session.scalars(
                select(JobCompensation).where(JobCompensation.job_id == job_id)
            )
        }
        job.taxonomy_version = "0.9.0"
        for row in session.scalars(
            select(JobSkill).where(
                JobSkill.job_id == job_id,
                JobSkill.method.in_(("alias", "exact", "pattern")),
            )
        ):
            row.taxonomy_version = "0.9.0"
        session.add_all(
            [
                JobSkill(
                    job_id=job_id,
                    skill_id="manual",
                    matched_text="manual",
                    section="other",
                    method="manual",
                    confidence=Decimal("1"),
                    taxonomy_version="0.9.0",
                ),
                JobSkill(
                    job_id=job_id,
                    skill_id="llm",
                    matched_text="llm",
                    section="other",
                    method="llm",
                    confidence=Decimal("1"),
                    taxonomy_version="0.9.0",
                ),
            ]
        )
        session.flush()
        preserved_ids = {
            row.id
            for row in session.scalars(
                select(JobSkill).where(JobSkill.method.in_(("manual", "llm")))
            )
        }
    result = _job_service(session_factory).enrich_stale()
    assert (result.selected, result.enriched, result.failed_ids) == (1, 1, ())
    with session_scope(session_factory) as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        assert job.enrichment_normalizer_version == "job-normalization-v4"
        assert (
            tuple(
                (row.job_id, row.ordinal)
                for row in session.scalars(
                    select(JobLocation)
                    .where(JobLocation.job_id == job_id)
                    .order_by(JobLocation.ordinal)
                )
            )
            == location_keys
        )
        assert {
            row.id
            for row in session.scalars(
                select(JobCompensation).where(JobCompensation.job_id == job_id)
            )
        } == compensation_ids
        assert {
            row.id
            for row in session.scalars(
                select(JobSkill).where(JobSkill.method.in_(("manual", "llm")))
            )
        } == preserved_ids
        assert {
            row.taxonomy_version
            for row in session.scalars(
                select(JobSkill).where(JobSkill.method.in_(("alias", "exact", "pattern")))
            )
        } == {"1.0.2"}


def test_extractor_failure_rolls_back_normalization_and_markers(
    session_factory: sessionmaker[Session],
) -> None:
    job_id = _seed_ashby_job(session_factory)

    class RaisingExtractor:
        taxonomy_version = "1.0.2"

        def extract(self, *, title: str, description: str) -> tuple[object, ...]:
            raise RuntimeError("extract failed")

    service = JobEnrichmentService(session_factory=session_factory, extractor=RaisingExtractor())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="extract failed"):
        service.enrich_job(job_id=job_id)
    with session_scope(session_factory) as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        assert (
            job.role_family,
            job.seniority,
            job.enrichment_normalizer_version,
            job.taxonomy_version,
        ) == (None, None, "none-v1", "none-v1")
        assert list(session.scalars(select(JobLocation).where(JobLocation.job_id == job_id))) == []
        assert (
            list(session.scalars(select(JobCompensation).where(JobCompensation.job_id == job_id)))
            == []
        )
        assert list(session.scalars(select(JobSkill).where(JobSkill.job_id == job_id))) == []


def _seed_ashby_job(session_factory: sessionmaker[Session]) -> UUID:
    raw = json.loads(
        (
            Path(__file__).resolve().parents[1] / "fixtures" / "ats" / "ashby" / "populated.json"
        ).read_text()
    )["jobs"][0]
    with session_scope(session_factory) as session:
        source = Source(
            key="ashby-example", kind="ashby", company_name="Example Inc.", config={}, enabled=True
        )
        session.add(source)
        session.flush()
        return (
            CanonicalJobRepository(session)
            .upsert(
                CanonicalJobCandidate(
                    source_key="ashby-example",
                    source_kind="ashby",
                    external_id="ashby-1",
                    company_name="Example Inc.",
                    title_raw="Senior Data Engineer",
                    title_normalized="Senior Data Engineer",
                    description_text="Build Python systems.",
                    observed_at=datetime(2026, 8, 21, 10, tzinfo=UTC),
                    raw_payload=raw,
                    normalizer_version="ashby-v1",
                    taxonomy_version="none-v1",
                )
            )
            .job_id
        )


def _service(session_factory: sessionmaker[Session]) -> SkillEnrichmentService:
    taxonomy_path = Path(__file__).resolve().parents[2] / "config" / "taxonomy" / "skills.v1.yaml"
    return SkillEnrichmentService(
        session_factory=session_factory,
        extractor=SkillExtractor(load_skill_taxonomy(taxonomy_path)),
    )


def _job_service(session_factory: sessionmaker[Session]) -> JobEnrichmentService:
    taxonomy_path = Path(__file__).resolve().parents[2] / "config" / "taxonomy" / "skills.v1.yaml"
    return JobEnrichmentService(
        session_factory=session_factory,
        extractor=SkillExtractor(load_skill_taxonomy(taxonomy_path)),
    )


def _batch_candidate(external_id: str, raw_payload: dict[str, object]) -> CanonicalJobCandidate:
    return CanonicalJobCandidate(
        source_key="batch",
        source_kind="greenhouse",
        external_id=external_id,
        company_name="Batch",
        title_raw="Python Engineer",
        title_normalized="Python Engineer",
        description_text="Build Python systems.",
        observed_at=datetime(2026, 8, 21, 10, tzinfo=UTC),
        raw_payload=raw_payload,
        normalizer_version="greenhouse-v1",
        taxonomy_version="none-v1",
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
    raw_payload: dict[str, object] | None = None,
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
        raw_payload=raw_payload or {"id": "job-123"},
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
