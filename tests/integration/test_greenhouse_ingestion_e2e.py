"""Opt-in deterministic Greenhouse collection from recorded evidence through lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Generator, Mapping
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from career_os.adapters.greenhouse import (
    GreenhouseAdapter,
    GreenhouseHttpResponse,
    GreenhouseTransportError,
)
from career_os.application import GreenhouseIngestionService, IngestionError
from career_os.config.sources import GreenhouseSourceConfig, LeverSourceConfig, SourcesConfig
from career_os.db import (
    create_database_engine,
    create_session_factory,
    database_url_from_settings,
    session_scope,
)
from career_os.logging import configure_logging
from career_os.persistence import (
    InspectionRepository,
    SourceConfigurationConflictError,
    SourceConfigurationRepository,
)
from career_os.persistence.integration_safety import (
    CAREER_OS_TABLE_NAMES,
    assert_disposable_test_database,
    assert_initial_schema_is_absent,
)
from career_os.persistence.models import CollectionRun, JobPosting, JobVersion, Source
from career_os.settings import load_settings

pytestmark = pytest.mark.integration


class _FixtureHttpClient:
    def __init__(self, responses: list[GreenhouseHttpResponse | Exception]) -> None:
        self._responses = responses

    async def get(
        self, *, url: str, headers: Mapping[str, str], timeout_seconds: float
    ) -> GreenhouseHttpResponse:
        del url, headers, timeout_seconds
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


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


def test_fixture_greenhouse_replay_is_idempotent_and_unsafe_scans_never_inactivate(
    session_factory: sessionmaker[Session],
) -> None:
    source_config = GreenhouseSourceConfig(
        key="example-greenhouse",
        kind="greenhouse",
        company_name="Example Inc.",
        board_token="example",
    )
    with session_scope(session_factory) as session:
        SourceConfigurationRepository(session).sync(SourcesConfig(sources=[source_config]))

    service = GreenhouseIngestionService(
        session_factory=session_factory,
        logger=configure_logging(logger_name="career_os.tests.e2e", stream=StringIO()),
        request_timeout_seconds=1.0,
    )
    first = asyncio.run(
        service.collect(source_config=source_config, adapter=_adapter("populated.json"))
    )
    replay = asyncio.run(
        service.collect(source_config=source_config, adapter=_adapter("populated.json"))
    )
    partial = asyncio.run(service.collect(source_config=source_config, adapter=_partial_adapter()))
    failed = asyncio.run(
        service.collect(
            source_config=source_config,
            adapter=GreenhouseAdapter(
                config=source_config,
                http_client=_FixtureHttpClient(
                    [GreenhouseTransportError("fixture transport failed")]
                ),
                max_attempts=1,
            ),
        )
    )

    assert first.collection.run.status.value == "succeeded"
    assert first.counts.inserted == 2
    assert replay.collection.run.status.value == "succeeded"
    assert replay.counts.unchanged == 2
    assert partial.collection.run.status.value == "partial"
    assert partial.counts.http_requests == 1
    assert partial.collection.run.incomplete_reason is not None
    assert partial.lifecycle.action.value == "skipped_unsafe"
    assert failed.collection.run.status.value == "failed"
    assert failed.counts.http_requests == 1
    assert failed.collection_failed is True
    assert failed.lifecycle.action.value == "skipped_unsafe"

    with session_scope(session_factory) as session:
        jobs = {job.external_id: job for job in session.scalars(select(JobPosting))}
        assert set(jobs) == {"12345", "67890"}
        assert all(job.status == "active" for job in jobs.values())
        assert len(list(session.scalars(select(JobVersion)))) == 2
        runs = list(session.scalars(select(CollectionRun).order_by(CollectionRun.started_at)))
        assert [run.status for run in runs] == ["succeeded", "succeeded", "partial", "failed"]
        assert (runs[0].inserted, runs[1].unchanged) == (2, 2)
        assert runs[2].inactivated == 0
        assert runs[2].incomplete_reason is not None
        assert runs[2].http_requests == 1
        assert runs[3].inactivated == 0
        assert runs[3].http_requests == 1
        assert all(job.team_raw is None and job.apply_url is None for job in jobs.values())


def test_complete_ingestion_failure_is_durably_failed_and_never_runs_lifecycle(
    session_factory: sessionmaker[Session],
) -> None:
    source_config = GreenhouseSourceConfig(
        key="example-greenhouse",
        kind="greenhouse",
        company_name="Example Inc.",
        board_token="example",
    )
    with session_scope(session_factory) as session:
        SourceConfigurationRepository(session).sync(SourcesConfig(sources=[source_config]))
    logger = configure_logging(logger_name="career_os.tests.e2e.failure", stream=StringIO())
    successful_service = GreenhouseIngestionService(
        session_factory=session_factory,
        logger=logger,
        request_timeout_seconds=1.0,
    )
    asyncio.run(
        successful_service.collect(source_config=source_config, adapter=_adapter("populated.json"))
    )

    def reject_candidate(*_args: object) -> object:
        raise IngestionError("fixture canonical persistence failed")

    failing_service = GreenhouseIngestionService(
        session_factory=session_factory,
        logger=logger,
        request_timeout_seconds=1.0,
        candidate_factory=reject_candidate,  # type: ignore[arg-type]
    )
    failed = asyncio.run(
        failing_service.collect(source_config=source_config, adapter=_adapter("populated.json"))
    )
    assert failed.collection_failed is True
    assert failed.collection.run.counts.normalization_errors == 1

    with session_scope(session_factory) as session:
        jobs = list(session.scalars(select(JobPosting)))
        assert len(jobs) == 2
        assert all(job.status == "active" for job in jobs)
        runs = list(session.scalars(select(CollectionRun).order_by(CollectionRun.started_at)))
        assert [run.status for run in runs] == ["succeeded", "failed"]
        assert runs[-1].inactivated == 0
        assert runs[-1].normalization_errors == 1


def test_source_configuration_sync_updates_mutable_config_and_preserves_omitted_sources(
    session_factory: sessionmaker[Session],
) -> None:
    first = _source_config("first", company_name="First Inc.", enabled=True, board_token="one")
    omitted = _source_config(
        "omitted", company_name="Omitted Inc.", enabled=True, board_token="keep"
    )
    with session_scope(session_factory) as session:
        SourceConfigurationRepository(session).sync(SourcesConfig(sources=[first, omitted]))

    updated_first = _source_config(
        "first", company_name="First Inc.", enabled=False, board_token="two"
    )
    with session_scope(session_factory) as session:
        SourceConfigurationRepository(session).sync(SourcesConfig(sources=[updated_first]))

    with session_scope(session_factory) as session:
        sources = {source.key: source for source in session.scalars(select(Source))}
        assert sources["first"].enabled is False
        assert sources["first"].config["board_token"] == "two"
        assert sources["omitted"].enabled is True
        assert sources["omitted"].config["board_token"] == "keep"

    with (
        session_scope(session_factory) as session,
        pytest.raises(SourceConfigurationConflictError, match="cannot change kind or company"),
    ):
        SourceConfigurationRepository(session).sync(
            SourcesConfig(
                sources=[
                    LeverSourceConfig(
                        key="first",
                        kind="lever",
                        company_name="First Inc.",
                        site="first",
                    )
                ]
            )
        )
    with (
        session_scope(session_factory) as session,
        pytest.raises(SourceConfigurationConflictError, match="cannot change kind or company"),
    ):
        SourceConfigurationRepository(session).sync(
            SourcesConfig(sources=[_source_config("first", company_name="Other Inc.")])
        )

    with session_scope(session_factory) as session:
        source = session.scalar(select(Source).where(Source.key == "first"))
        assert source is not None
        assert source.kind == "greenhouse"
        assert source.company_name == "First Inc."


def test_inspection_repository_filters_runs_and_jobs(
    session_factory: sessionmaker[Session],
) -> None:
    alpha = _source_config("alpha", company_name="Alpha Inc.")
    beta = _source_config("beta", company_name="Beta Inc.")
    started_at = datetime(2026, 8, 19, 9, tzinfo=UTC)
    failed_id = uuid4()
    succeeded_id = uuid4()
    with session_scope(session_factory) as session:
        SourceConfigurationRepository(session).sync(SourcesConfig(sources=[alpha, beta]))
        sources = {source.key: source for source in session.scalars(select(Source))}
        session.add_all(
            [
                _run_model(
                    run_id=failed_id,
                    source_id=sources["alpha"].id,
                    status="failed",
                    started_at=started_at,
                ),
                _run_model(
                    run_id=succeeded_id,
                    source_id=sources["beta"].id,
                    status="succeeded",
                    started_at=started_at + timedelta(minutes=1),
                ),
                _job_model(
                    source_id=sources["alpha"].id,
                    external_id="alpha-active",
                    company_name="Alpha Inc.",
                    status="active",
                    observed_at=started_at,
                ),
                _job_model(
                    source_id=sources["alpha"].id,
                    external_id="alpha-inactive",
                    company_name="Alpha Inc.",
                    status="inactive",
                    observed_at=started_at,
                ),
                _job_model(
                    source_id=sources["beta"].id,
                    external_id="beta-active",
                    company_name="Beta Inc.",
                    status="active",
                    observed_at=started_at,
                ),
            ]
        )

    with session_scope(session_factory) as session:
        repository = InspectionRepository(session)
        failed_runs = repository.list_runs(failed_only=True)
        assert [run.run.id for run in failed_runs] == [failed_id]
        assert failed_runs[0].source_key == "alpha"
        assert failed_runs[0].source_kind == "greenhouse"
        shown = repository.get_run(succeeded_id)
        assert shown is not None
        assert shown.run.status == "succeeded"
        assert shown.source_key == "beta"
        assert repository.get_run(uuid4()) is None
        assert {
            job.job.external_id for job in repository.list_jobs(active_only=True, company_name=None)
        } == {
            "alpha-active",
            "beta-active",
        }
        assert {
            job.job.external_id
            for job in repository.list_jobs(active_only=True, company_name="Alpha Inc.")
        } == {"alpha-active"}
        assert {
            job.job.external_id
            for job in repository.list_jobs(active_only=False, company_name="Alpha Inc.")
        } == {"alpha-active", "alpha-inactive"}


def _source_config(
    key: str,
    *,
    company_name: str,
    enabled: bool = True,
    board_token: str = "board",
) -> GreenhouseSourceConfig:
    return GreenhouseSourceConfig(
        key=key,
        kind="greenhouse",
        company_name=company_name,
        board_token=board_token,
        enabled=enabled,
    )


def _run_model(
    *,
    run_id: UUID,
    source_id: UUID,
    status: str,
    started_at: datetime,
) -> CollectionRun:
    return CollectionRun(
        id=run_id,
        source_id=source_id,
        status=status,
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=1),
        normalizer_version="greenhouse-v1",
        taxonomy_version="none-v1",
        error_summary="fixture failed" if status == "failed" else None,
    )


def _job_model(
    *,
    source_id: UUID,
    external_id: str,
    company_name: str,
    status: str,
    observed_at: datetime,
) -> JobPosting:
    return JobPosting(
        source_id=source_id,
        external_id=external_id,
        company_name=company_name,
        title_raw=f"{external_id} title",
        title_normalized=f"{external_id} title",
        description_html=None,
        description_text="Fixture description",
        department_raw=None,
        team_raw=None,
        source_url=None,
        apply_url=None,
        published_at=None,
        first_seen_at=observed_at,
        last_seen_at=observed_at,
        inactive_at=observed_at if status == "inactive" else None,
        status=status,
        content_hash=f"hash-{external_id}",
        raw_latest={"id": external_id},
        normalizer_version="greenhouse-v1",
        taxonomy_version="none-v1",
    )


def _adapter(fixture_name: str) -> GreenhouseAdapter:
    return GreenhouseAdapter(
        config=GreenhouseSourceConfig(
            key="example-greenhouse",
            kind="greenhouse",
            company_name="Example Inc.",
            board_token="example",
        ),
        http_client=_FixtureHttpClient([_response(fixture_name)]),
    )


def _partial_adapter() -> GreenhouseAdapter:
    populated = _response("populated.json").json_body
    assert isinstance(populated, dict)
    jobs = populated["jobs"]
    assert isinstance(jobs, list)
    return GreenhouseAdapter(
        config=GreenhouseSourceConfig(
            key="example-greenhouse",
            kind="greenhouse",
            company_name="Example Inc.",
            board_token="example",
        ),
        http_client=_FixtureHttpClient(
            [
                GreenhouseHttpResponse(
                    status_code=200,
                    headers={},
                    json_body={"jobs": [jobs[0]], "meta": {"total": 2}},
                )
            ]
        ),
    )


def _response(fixture_name: str) -> GreenhouseHttpResponse:
    fixture_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "ats" / "greenhouse" / fixture_name
    )
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    return GreenhouseHttpResponse(status_code=200, headers={}, json_body=payload)


def _return_database_to_empty(engine: Engine, alembic_config: Config, upgraded: bool) -> None:
    try:
        if upgraded:
            command.downgrade(alembic_config, "base")
        with engine.begin() as connection:
            for table_name in CAREER_OS_TABLE_NAMES:
                connection.execute(text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))
            connection.execute(text("DROP TABLE IF EXISTS alembic_version CASCADE"))
    finally:
        engine.dispose()
