"""Opt-in PostgreSQL checks for complete-scan lifecycle reconciliation."""

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

from career_os.application import (
    CollectionFailedError,
    CollectionResult,
    CollectionRun,
    CollectionRunStatus,
    LifecycleAction,
    LifecycleService,
)
from career_os.db import (
    create_database_engine,
    create_session_factory,
    database_url_from_settings,
    session_scope,
)
from career_os.domain import (
    CanonicalJobCandidate,
    RawJobPosting,
    RunCounts,
    ScanOutcome,
    ScanStatus,
)
from career_os.persistence.integration_safety import (
    CAREER_OS_TABLE_NAMES,
    assert_disposable_test_database,
    assert_initial_schema_is_absent,
)
from career_os.persistence.models import CollectionRun as CollectionRunModel
from career_os.persistence.models import JobPosting, JobVersion, Source
from career_os.persistence.repositories import (
    CanonicalJobRepository,
    JobUpsertAction,
    LifecycleReconciliationConflictError,
    SourceLifecycleRepository,
    SqlAlchemyCollectionRunStore,
    StaleObservationError,
)
from career_os.settings import load_settings

pytestmark = pytest.mark.integration

_INITIAL_OBSERVED_AT = datetime(2026, 8, 18, 9, tzinfo=UTC)
_RUN_STARTED_AT = datetime(2026, 8, 18, 10, tzinfo=UTC)
_RUN_FINISHED_AT = datetime(2026, 8, 18, 11, tzinfo=UTC)


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


def test_complete_scan_inactivates_only_missing_jobs_for_its_source(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_source(session_factory, key="example", company_name="Example Inc.")
    _seed_source(session_factory, key="other", company_name="Other Inc.")
    missing = _candidate(external_id="missing")
    seen = _candidate(external_id="seen")
    other_source = _candidate(
        source_key="other", company_name="Other Inc.", external_id="other-job"
    )
    _upsert_committed(session_factory, missing)
    _upsert_committed(session_factory, seen)
    _upsert_committed(session_factory, other_source)
    collection = _complete_result(session_factory, postings=(_posting("seen"),))

    result = _reconcile_committed(session_factory, collection)

    assert result.action is LifecycleAction.INACTIVATED
    assert result.inactivated_count == 1
    with session_scope(session_factory) as session:
        jobs = {job.external_id: job for job in session.scalars(select(JobPosting))}
        assert jobs["missing"].status == "inactive"
        assert jobs["missing"].inactive_at == _RUN_FINISHED_AT
        assert jobs["seen"].status == "active"
        assert jobs["other-job"].status == "active"
        assert len(list(session.scalars(select(JobVersion)))) == 3
        persisted_run = session.get(CollectionRunModel, UUID(collection.run.id))
        assert persisted_run is not None
        assert persisted_run.inactivated == 1


def test_empty_complete_scan_inactivates_all_eligible_active_jobs(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_source(session_factory, key="example", company_name="Example Inc.")
    _upsert_committed(session_factory, _candidate(external_id="first"))
    _upsert_committed(session_factory, _candidate(external_id="second"))
    collection = _complete_result(session_factory, postings=())

    result = _reconcile_committed(session_factory, collection)

    assert result.inactivated_count == 2
    with session_scope(session_factory) as session:
        assert {job.status for job in session.scalars(select(JobPosting))} == {"inactive"}


@pytest.mark.parametrize("kind", ["partial", "failed", "unpersisted_complete"])
def test_unsafe_collection_results_do_not_mutate_jobs(
    session_factory: sessionmaker[Session], kind: str
) -> None:
    _seed_source(session_factory, key="example", company_name="Example Inc.")
    _upsert_committed(session_factory, _candidate(external_id="still-active"))
    collection = _unsafe_result(kind)

    with session_scope(session_factory) as session:
        result = LifecycleService(SourceLifecycleRepository(session)).reconcile(collection)

    assert result.action is LifecycleAction.SKIPPED_UNSAFE
    assert result.inactivated_count == 0
    with session_scope(session_factory) as session:
        job = session.scalar(select(JobPosting))
        assert job is not None
        assert job.status == "active"
        assert job.inactive_at is None


def test_older_complete_scan_cannot_inactivate_job_observed_after_scan_started(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_source(session_factory, key="example", company_name="Example Inc.")
    newer_observation = _candidate(
        external_id="newer-job", observed_at=_RUN_STARTED_AT + timedelta(minutes=1)
    )
    _upsert_committed(session_factory, newer_observation)
    collection = _complete_result(session_factory, postings=())

    result = _reconcile_committed(session_factory, collection)

    assert result.inactivated_count == 0
    with session_scope(session_factory) as session:
        job = session.scalar(select(JobPosting))
        assert job is not None
        assert job.status == "active"


def test_persisted_partial_run_is_rejected_by_repository_without_mutation(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_source(session_factory, key="example", company_name="Example Inc.")
    _upsert_committed(session_factory, _candidate(external_id="still-active"))
    partial_run = _partial_run(session_factory)

    with (
        session_scope(session_factory) as session,
        pytest.raises(LifecycleReconciliationConflictError, match="succeeded"),
    ):
        SourceLifecycleRepository(session).inactivate_unseen(
            run=partial_run,
            seen_external_ids=(),
        )

    with session_scope(session_factory) as session:
        job = session.scalar(select(JobPosting))
        assert job is not None
        assert job.status == "active"
        persisted_run = session.get(CollectionRunModel, UUID(partial_run.id))
        assert persisted_run is not None
        assert persisted_run.inactivated == 0


def test_complete_run_replay_is_idempotent_and_preserves_prior_inactivated_count(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_source(session_factory, key="example", company_name="Example Inc.")
    _upsert_committed(session_factory, _candidate(external_id="missing"))
    collection = _complete_result(session_factory, postings=())

    first = _reconcile_committed(session_factory, collection)
    second = _reconcile_committed(session_factory, collection)

    assert (first.inactivated_count, second.inactivated_count) == (1, 0)
    with session_scope(session_factory) as session:
        persisted_run = session.get(CollectionRunModel, UUID(collection.run.id))
        assert persisted_run is not None
        assert persisted_run.inactivated == 1


def test_lifecycle_repository_does_not_commit_the_caller_owned_transaction(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_source(session_factory, key="example", company_name="Example Inc.")
    _upsert_committed(session_factory, _candidate(external_id="missing"))
    collection = _complete_result(session_factory, postings=())
    session = session_factory()
    try:
        result = LifecycleService(SourceLifecycleRepository(session)).reconcile(collection)
        assert result.inactivated_count == 1
        session.rollback()
    finally:
        session.close()

    with session_scope(session_factory) as verification_session:
        job = verification_session.scalar(select(JobPosting))
        assert job is not None
        assert job.status == "active"
        persisted_run = verification_session.get(CollectionRunModel, UUID(collection.run.id))
        assert persisted_run is not None
        assert persisted_run.inactivated == 0


def test_identical_newer_observation_reactivates_without_a_new_version(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_source(session_factory, key="example", company_name="Example Inc.")
    first = _candidate(external_id="returning")
    _upsert_committed(session_factory, first)
    _reconcile_committed(session_factory, _complete_result(session_factory, postings=()))
    returning = _candidate(
        external_id="returning",
        observed_at=_RUN_FINISHED_AT + timedelta(minutes=1),
        raw_payload={"id": "returning", "revision": 2},
    )

    result = _upsert_committed(session_factory, returning)

    assert result.action is JobUpsertAction.UNCHANGED
    assert result.reactivated is True
    with session_scope(session_factory) as session:
        job = session.scalar(select(JobPosting))
        assert job is not None
        assert job.status == "active"
        assert job.inactive_at is None
        assert len(list(session.scalars(select(JobVersion)))) == 1


def test_delayed_identical_observation_does_not_reactivate_before_absence_confirmation(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_source(session_factory, key="example", company_name="Example Inc.")
    first = _candidate(external_id="delayed")
    _upsert_committed(session_factory, first)
    _reconcile_committed(session_factory, _complete_result(session_factory, postings=()))
    delayed = _candidate(
        external_id="delayed",
        observed_at=_RUN_STARTED_AT,
        raw_payload={"id": "delayed", "revision": "late"},
    )

    with (
        session_scope(session_factory) as session,
        pytest.raises(StaleObservationError, match="absence confirmation"),
    ):
        CanonicalJobRepository(session).upsert(delayed)

    with session_scope(session_factory) as session:
        job = session.scalar(select(JobPosting))
        assert job is not None
        assert job.status == "inactive"
        assert job.inactive_at == _RUN_FINISHED_AT
        assert job.last_seen_at == first.observed_at
        assert job.raw_latest == {"id": "delayed"}
        assert len(list(session.scalars(select(JobVersion)))) == 1


def test_delayed_changed_observation_does_not_mutate_inactive_current_content(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_source(session_factory, key="example", company_name="Example Inc.")
    first = _candidate(external_id="delayed")
    _upsert_committed(session_factory, first)
    _reconcile_committed(session_factory, _complete_result(session_factory, postings=()))
    delayed = _candidate(
        external_id="delayed",
        observed_at=_RUN_STARTED_AT,
        title_raw="Staff Platform Engineer",
        title_normalized="Staff Platform Engineer",
    )

    with (
        session_scope(session_factory) as session,
        pytest.raises(StaleObservationError, match="absence confirmation"),
    ):
        CanonicalJobRepository(session).upsert(delayed)

    with session_scope(session_factory) as session:
        job = session.scalar(select(JobPosting))
        assert job is not None
        assert job.status == "inactive"
        assert job.inactive_at == _RUN_FINISHED_AT
        assert job.title_raw == first.title_raw
        assert job.content_hash == first.content_hash
        assert len(list(session.scalars(select(JobVersion)))) == 1


def test_inactive_row_without_absence_timestamp_fails_closed(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_source(session_factory, key="example", company_name="Example Inc.")
    first = _candidate(external_id="inconsistent")
    _upsert_committed(session_factory, first)
    with session_scope(session_factory) as session:
        job = session.scalar(select(JobPosting))
        assert job is not None
        job.status = "inactive"
        job.inactive_at = None

    with (
        session_scope(session_factory) as session,
        pytest.raises(LifecycleReconciliationConflictError, match="absence confirmation"),
    ):
        CanonicalJobRepository(session).upsert(
            _candidate(external_id="inconsistent", observed_at=_RUN_FINISHED_AT)
        )

    with session_scope(session_factory) as session:
        job = session.scalar(select(JobPosting))
        assert job is not None
        assert job.status == "inactive"
        assert job.inactive_at is None
        assert len(list(session.scalars(select(JobVersion)))) == 1


def test_changed_newer_observation_reactivates_and_creates_one_new_content_version(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_source(session_factory, key="example", company_name="Example Inc.")
    first = _candidate(external_id="returning")
    _upsert_committed(session_factory, first)
    _reconcile_committed(session_factory, _complete_result(session_factory, postings=()))
    changed = _candidate(
        external_id="returning",
        observed_at=_RUN_FINISHED_AT + timedelta(minutes=1),
        title_raw="Staff Platform Engineer",
        title_normalized="Staff Platform Engineer",
    )

    result = _upsert_committed(session_factory, changed)

    assert result.action is JobUpsertAction.UPDATED
    assert result.reactivated is True
    with session_scope(session_factory) as session:
        job = session.scalar(select(JobPosting))
        assert job is not None
        assert job.status == "active"
        assert job.inactive_at is None
        versions = list(session.scalars(select(JobVersion).order_by(JobVersion.observed_at)))
        assert len(versions) == 2
        assert versions[-1].content_hash == changed.content_hash


def _complete_result(
    session_factory: sessionmaker[Session], *, postings: tuple[RawJobPosting, ...]
) -> CollectionResult:
    running = CollectionRun.start(
        run_id=str(uuid4()),
        source_key="example",
        source_kind="greenhouse",
        started_at=_RUN_STARTED_AT,
    )
    store = SqlAlchemyCollectionRunStore(
        session_factory=session_factory,
        normalizer_version="1.0.0",
        taxonomy_version="1.0.0",
    )
    store.create(running)
    terminal = running.finish(
        status=CollectionRunStatus.SUCCEEDED,
        finished_at=_RUN_FINISHED_AT,
        counts=RunCounts(items_fetched=len(postings)),
    )
    store.update(terminal)
    return CollectionResult(
        run=terminal,
        postings=postings,
        outcome=ScanOutcome(ScanStatus.COMPLETE),
        terminal_state_persisted=True,
    )


def _partial_run(session_factory: sessionmaker[Session]) -> CollectionRun:
    running = CollectionRun.start(
        run_id=str(uuid4()),
        source_key="example",
        source_kind="greenhouse",
        started_at=_RUN_STARTED_AT,
    )
    store = SqlAlchemyCollectionRunStore(
        session_factory=session_factory,
        normalizer_version="1.0.0",
        taxonomy_version="1.0.0",
    )
    store.create(running)
    partial = running.finish(
        status=CollectionRunStatus.PARTIAL,
        finished_at=_RUN_FINISHED_AT,
        counts=RunCounts(),
    )
    store.update(partial)
    return partial


def _unsafe_result(kind: str) -> CollectionResult | CollectionFailedError:
    running = CollectionRun.start(
        run_id=str(uuid4()),
        source_key="example",
        source_kind="greenhouse",
        started_at=_RUN_STARTED_AT,
    )
    if kind == "partial":
        terminal = running.finish(
            status=CollectionRunStatus.PARTIAL,
            finished_at=_RUN_FINISHED_AT,
            counts=RunCounts(),
        )
        return CollectionResult(
            run=terminal,
            postings=(),
            outcome=ScanOutcome(ScanStatus.PARTIAL, incomplete_reason="page timed out"),
            terminal_state_persisted=True,
        )
    if kind == "failed":
        terminal = running.finish(
            status=CollectionRunStatus.FAILED,
            finished_at=_RUN_FINISHED_AT,
            counts=RunCounts(),
            error_summary="adapter failed",
        )
        return CollectionFailedError(
            CollectionResult(
                run=terminal,
                postings=(),
                outcome=None,
                terminal_state_persisted=True,
            )
        )
    if kind == "unpersisted_complete":
        terminal = running.finish(
            status=CollectionRunStatus.SUCCEEDED,
            finished_at=_RUN_FINISHED_AT,
            counts=RunCounts(),
        )
        return CollectionResult(
            run=terminal,
            postings=(),
            outcome=ScanOutcome(ScanStatus.COMPLETE),
            terminal_state_persisted=False,
        )
    raise AssertionError(f"unsupported unsafe result kind: {kind}")


def _reconcile_committed(session_factory: sessionmaker[Session], collection: CollectionResult):
    with session_scope(session_factory) as session:
        return LifecycleService(SourceLifecycleRepository(session)).reconcile(collection)


def _candidate(
    *,
    external_id: str,
    source_key: str = "example",
    company_name: str = "Example Inc.",
    observed_at: datetime = _INITIAL_OBSERVED_AT,
    **overrides: object,
) -> CanonicalJobCandidate:
    values: dict[str, object] = {
        "source_key": source_key,
        "source_kind": "greenhouse",
        "external_id": external_id,
        "company_name": company_name,
        "title_raw": "Senior Platform Engineer",
        "title_normalized": "Senior Platform Engineer",
        "description_text": "Build reliable systems.",
        "observed_at": observed_at,
        "raw_payload": {"id": external_id},
        "normalizer_version": "1.0.0",
        "taxonomy_version": "1.0.0",
    }
    values.update(overrides)
    return CanonicalJobCandidate(**values)  # type: ignore[arg-type]


def _posting(external_id: str) -> RawJobPosting:
    return RawJobPosting(
        source_key="example",
        source_kind="greenhouse",
        external_id=external_id,
        source_url=None,
        apply_url=None,
        payload={"id": external_id},
        fetched_at=_RUN_FINISHED_AT,
    )


def _seed_source(session_factory: sessionmaker[Session], *, key: str, company_name: str) -> None:
    with session_scope(session_factory) as session:
        session.add(
            Source(
                key=key,
                kind="greenhouse",
                company_name=company_name,
                config={"board_token": key},
                enabled=True,
            )
        )


def _upsert_committed(session_factory: sessionmaker[Session], candidate: CanonicalJobCandidate):
    with session_scope(session_factory) as session:
        return CanonicalJobRepository(session).upsert(candidate)


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
