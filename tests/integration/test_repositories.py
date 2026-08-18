"""Opt-in PostgreSQL tests for canonical upserts and durable collection runs."""

from __future__ import annotations

import os
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from career_os.application import CollectionRun, CollectionRunStatus
from career_os.db import (
    create_database_engine,
    create_session_factory,
    database_url_from_settings,
    session_scope,
)
from career_os.domain import CANONICAL_CONTENT_FIELDS, CanonicalJobCandidate
from career_os.persistence.integration_safety import (
    CAREER_OS_TABLE_NAMES,
    assert_disposable_test_database,
    assert_initial_schema_is_absent,
)
from career_os.persistence.models import CollectionRun as CollectionRunModel
from career_os.persistence.models import JobPosting, JobVersion, Source
from career_os.persistence.repositories import (
    CanonicalJobRepository,
    CanonicalVersionConflictError,
    CollectionRunPersistenceConflictError,
    JobUpsertAction,
    SourceIdentityConflictError,
    SourceNotFoundError,
    SqlAlchemyCollectionRunStore,
    StaleObservationError,
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


def test_initial_candidate_creates_active_job_and_immutable_version(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_source(session_factory)
    candidate = _candidate()

    with session_scope(session_factory) as session:
        result = CanonicalJobRepository(session).upsert(candidate)

        assert result.action is JobUpsertAction.INSERTED
        assert result.changed_fields == CANONICAL_CONTENT_FIELDS
        job = session.get(JobPosting, result.job_id)
        assert job is not None
        assert job.status == "active"
        assert job.first_seen_at == candidate.observed_at
        assert job.last_seen_at == candidate.observed_at
        assert job.raw_latest == {"id": "job-123", "unknown": {"values": ["one", "two"]}}
        versions = list(session.scalars(select(JobVersion).where(JobVersion.job_id == job.id)))
        assert len(versions) == 1
        assert versions[0].content_hash == candidate.content_hash
        assert versions[0].changed_fields == list(CANONICAL_CONTENT_FIELDS)
        assert versions[0].raw_payload == job.raw_latest


def test_identical_content_updates_only_observation_and_latest_raw_state(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_source(session_factory)
    first = _candidate()
    later = _candidate(
        observed_at=first.observed_at + timedelta(minutes=5),
        raw_payload={"id": "job-123", "upstream_revision": 2},
    )
    _upsert_committed(session_factory, first)

    with session_scope(session_factory) as session:
        result = CanonicalJobRepository(session).upsert(later)

        assert result.action is JobUpsertAction.UNCHANGED
        assert result.changed_fields == ()

    with session_scope(session_factory) as session:
        job = session.scalar(select(JobPosting))
        assert job is not None
        assert job.first_seen_at == first.observed_at
        assert job.last_seen_at == later.observed_at
        assert job.raw_latest == {"id": "job-123", "upstream_revision": 2}
        versions = list(session.scalars(select(JobVersion).where(JobVersion.job_id == job.id)))
        assert len(versions) == 1
        assert versions[0].raw_payload == {"id": "job-123", "unknown": {"values": ["one", "two"]}}


def test_changed_content_updates_current_job_and_creates_one_new_version(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_source(session_factory)
    first = _candidate()
    changed = _candidate(
        observed_at=first.observed_at + timedelta(minutes=5),
        title_raw="Staff Platform Engineer",
        title_normalized="Staff Platform Engineer",
        raw_payload={"id": "job-123", "upstream_revision": 2},
    )
    _upsert_committed(session_factory, first)

    with session_scope(session_factory) as session:
        result = CanonicalJobRepository(session).upsert(changed)

        assert result.action is JobUpsertAction.UPDATED
        assert result.changed_fields == ("title_raw", "title_normalized")

    with session_scope(session_factory) as session:
        job = session.scalar(select(JobPosting))
        assert job is not None
        assert job.id == result.job_id
        assert job.external_id == "job-123"
        assert job.title_raw == "Staff Platform Engineer"
        assert job.content_hash == changed.content_hash
        versions = list(
            session.scalars(
                select(JobVersion)
                .where(JobVersion.job_id == job.id)
                .order_by(JobVersion.observed_at)
            )
        )
        assert [(version.content_hash, version.changed_fields) for version in versions] == [
            (first.content_hash, list(CANONICAL_CONTENT_FIELDS)),
            (changed.content_hash, ["title_raw", "title_normalized"]),
        ]
        assert versions[1].canonical_snapshot == changed.canonical_content()
        assert versions[1].raw_payload == {"id": "job-123", "upstream_revision": 2}
        assert versions[1].observed_at == changed.observed_at


def test_conflicting_company_or_stale_or_reverted_evidence_fails_closed(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_source(session_factory)
    first = _candidate()
    changed = _candidate(
        observed_at=first.observed_at + timedelta(minutes=5),
        title_raw="Staff Platform Engineer",
        title_normalized="Staff Platform Engineer",
    )
    _upsert_committed(session_factory, first)
    _upsert_committed(session_factory, changed)

    with session_scope(session_factory) as session:
        repository = CanonicalJobRepository(session)
        with pytest.raises(SourceNotFoundError, match="not found"):
            repository.upsert(_candidate(source_key="missing"))
        with pytest.raises(SourceIdentityConflictError, match="kind"):
            repository.upsert(_candidate(source_kind="lever"))
        with pytest.raises(SourceIdentityConflictError, match="company"):
            repository.upsert(_candidate(company_name="Different Inc."))
        with pytest.raises(StaleObservationError, match="strictly newer"):
            repository.upsert(_candidate(observed_at=first.observed_at))
        with pytest.raises(CanonicalVersionConflictError, match="already-recorded"):
            repository.upsert(_candidate(observed_at=changed.observed_at + timedelta(minutes=5)))

    with session_scope(session_factory) as session:
        job = session.scalar(select(JobPosting))
        assert job is not None
        assert job.title_raw == "Staff Platform Engineer"
        assert job.last_seen_at == changed.observed_at
        assert (
            len(list(session.scalars(select(JobVersion).where(JobVersion.job_id == job.id)))) == 2
        )


def test_job_repository_does_not_commit_the_caller_owned_transaction(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_source(session_factory)
    session = session_factory()
    try:
        CanonicalJobRepository(session).upsert(_candidate())
        session.rollback()
    finally:
        session.close()

    with session_scope(session_factory) as verification_session:
        assert verification_session.scalar(select(JobPosting)) is None
        assert verification_session.scalar(select(JobVersion)) is None


def test_collection_run_store_commits_short_state_transitions_and_rejects_replays(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_source(session_factory)
    run = CollectionRun.start(
        run_id=str(uuid4()),
        source_key="example",
        source_kind="greenhouse",
        started_at=datetime(2026, 8, 18, 10, tzinfo=UTC),
    )
    store = SqlAlchemyCollectionRunStore(
        session_factory=session_factory,
        normalizer_version="1.0.0",
        taxonomy_version="1.0.0",
    )

    store.create(run)
    terminal = run.finish(
        status=CollectionRunStatus.SUCCEEDED,
        finished_at=run.started_at + timedelta(minutes=1),
        counts=run.counts,
    )
    store.update(terminal)
    with pytest.raises(CollectionRunPersistenceConflictError, match="expected running"):
        store.update(terminal)

    with session_scope(session_factory) as session:
        persisted = session.get(CollectionRunModel, uuid4())
        assert persisted is None
        persisted = session.get(CollectionRunModel, UUID(run.id))
        assert persisted is not None
        assert persisted.status == "succeeded"
        assert persisted.finished_at == terminal.finished_at


def test_collection_run_store_translates_duplicate_run_id_without_mutating_original(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_source(session_factory)
    run = CollectionRun.start(
        run_id=str(uuid4()),
        source_key="example",
        source_kind="greenhouse",
        started_at=datetime(2026, 8, 18, 10, tzinfo=UTC),
    )
    store = SqlAlchemyCollectionRunStore(
        session_factory=session_factory,
        normalizer_version="1.0.0",
        taxonomy_version="1.0.0",
    )

    store.create(run)
    with pytest.raises(CollectionRunPersistenceConflictError, match="already exists"):
        store.create(run)

    with session_scope(session_factory) as session:
        persisted = session.get(CollectionRunModel, UUID(run.id))
        assert persisted is not None
        assert persisted.status == "running"
        assert persisted.started_at == run.started_at
        assert persisted.finished_at is None
        assert persisted.items_fetched == 0


def _candidate(**overrides: object) -> CanonicalJobCandidate:
    values: dict[str, object] = {
        "source_key": "example",
        "source_kind": "greenhouse",
        "external_id": "job-123",
        "company_name": "Example Inc.",
        "title_raw": "Senior Platform Engineer",
        "title_normalized": "Senior Platform Engineer",
        "description_text": "Build reliable systems.",
        "description_html": "<p>Build reliable systems.</p>",
        "observed_at": datetime(2026, 8, 18, 10, tzinfo=UTC),
        "raw_payload": {"id": "job-123", "unknown": {"values": ("one", "two")}},
        "normalizer_version": "1.0.0",
        "taxonomy_version": "1.0.0",
        "source_url": "https://example.com/jobs/123",
        "apply_url": "https://example.com/jobs/123/apply",
        "published_at": datetime(2026, 8, 1, 12, tzinfo=UTC),
    }
    values.update(overrides)
    return CanonicalJobCandidate(**values)  # type: ignore[arg-type]


def _seed_source(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        session.add(
            Source(
                key="example",
                kind="greenhouse",
                company_name="Example Inc.",
                config={"board_token": "example"},
                enabled=True,
            )
        )


def _upsert_committed(
    session_factory: sessionmaker[Session], candidate: CanonicalJobCandidate
) -> None:
    with session_scope(session_factory) as session:
        CanonicalJobRepository(session).upsert(candidate)


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
