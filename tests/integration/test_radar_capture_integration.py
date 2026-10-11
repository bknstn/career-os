"""Opt-in PostgreSQL tests for transaction-bound Radar scan capture."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from career_os.application import GreenhouseIngestionService, IngestionResult
from career_os.application.collect import CollectionRun, CollectionRunStatus
from career_os.application.operations import CollectionOperationalPolicy
from career_os.cli import app
from career_os.config.radar_panel import RadarPanelManifest
from career_os.config.sources import GreenhouseSourceConfig, SourcesConfig
from career_os.db import (
    create_database_engine,
    create_session_factory,
    database_url_from_settings,
    session_scope,
)
from career_os.domain import FetchContext, RawJobPosting, ScanFailedError, ScanOutcome, ScanStatus
from career_os.domain.canonical import CanonicalJobCandidate
from career_os.logging import configure_logging
from career_os.persistence import (
    CanonicalJobRepository,
    CanonicalVersionConflictError,
    PersistenceError,
    RadarPanelVersionConflictError,
    RadarPanelVersionRepository,
    RadarScanObservationRepository,
    SourceConfigurationRepository,
    SqlAlchemyCollectionRunStore,
)
from career_os.persistence.integration_safety import (
    CAREER_OS_TABLE_NAMES,
    assert_disposable_test_database,
    assert_initial_schema_is_absent,
)
from career_os.persistence.models import (
    CollectionRun as CollectionRunModel,
)
from career_os.persistence.models import (
    JobPosting,
    JobVersion,
    RadarScanObservation,
)
from career_os.settings import load_settings

pytestmark = pytest.mark.integration

type PostingSpec = tuple[str, str, str]
type TerminalSpec = ScanOutcome | ScanFailedError


@dataclass(slots=True)
class _ScriptedGreenhouseAdapter:
    """Offline adapter stream for deterministic persistence assertions."""

    source_config: GreenhouseSourceConfig
    postings: tuple[PostingSpec, ...]
    terminal: TerminalSpec
    validate_calls: int = 0
    fetch_calls: int = 0

    @property
    def kind(self) -> str:
        return "greenhouse"

    async def validate(self) -> None:
        self.validate_calls += 1

    async def fetch(self, context: FetchContext) -> AsyncIterator[RawJobPosting | ScanOutcome]:
        self.fetch_calls += 1
        for index, (external_id, title, description) in enumerate(self.postings):
            yield RawJobPosting(
                source_key=self.source_config.key,
                source_kind=self.source_config.kind,
                external_id=external_id,
                source_url=f"https://example.test/jobs/{external_id}",
                apply_url=None,
                payload={"title": title, "content": f"<p>{description}</p>"},
                fetched_at=context.fetched_at + timedelta(microseconds=index),
            )
        if isinstance(self.terminal, ScanFailedError):
            raise self.terminal
        yield self.terminal


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
        try:
            if upgraded:
                command.downgrade(alembic_config, "base")
                with engine.begin() as connection:
                    for table_name in CAREER_OS_TABLE_NAMES:
                        connection.execute(text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))
                    connection.execute(text("DROP TABLE IF EXISTS alembic_version CASCADE"))
                assert not (
                    set(inspect(engine).get_table_names())
                    & (CAREER_OS_TABLE_NAMES | {"alembic_version"})
                )
        finally:
            engine.dispose()


def test_empty_and_a_b_a_complete_scans_publish_exact_immutable_membership(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    source_config, panel = _register_panel(session_factory, tmp_path)
    empty = _collect(
        session_factory,
        source_config,
        panel,
        postings=(),
        terminal=ScanOutcome(status=ScanStatus.COMPLETE),
    )
    assert empty.collection.run.status is CollectionRunStatus.SUCCEEDED

    first_a = _collect_one(session_factory, source_config, panel, "Platform Engineer", "Version A")
    version_b = _collect_one(session_factory, source_config, panel, "Data Engineer", "Version B")
    repeated_a = _collect_one(
        session_factory, source_config, panel, "Platform Engineer", "Version A"
    )
    assert all(
        result.collection.run.status is CollectionRunStatus.SUCCEEDED
        for result in (first_a, version_b, repeated_a)
    )

    with session_scope(session_factory) as session:
        runs = list(
            session.scalars(select(CollectionRunModel).order_by(CollectionRunModel.started_at))
        )
        observations = list(
            session.scalars(
                select(RadarScanObservation).order_by(
                    RadarScanObservation.observed_at, RadarScanObservation.run_id
                )
            )
        )
        versions = list(session.scalars(select(JobVersion)))
        current_job = session.scalar(
            select(JobPosting).where(JobPosting.external_id == "posting-1")
        )
        assert current_job is not None
        assert current_job.title_raw == "Platform Engineer"
        assert len(versions) == 2
        assert [(observation.title, observation.description) for observation in observations] == [
            ("Platform Engineer", "Version A"),
            ("Data Engineer", "Version B"),
            ("Platform Engineer", "Version A"),
        ]
        assert [observation.observed_at for observation in observations] == [
            run.finished_at for run in runs[1:]
        ]
        assert all(run.status == "succeeded" for run in runs)
        assert all(run.radar_panel_version == panel.panel_version for run in runs)
        assert len(observations) == 3

        changed_replay = _candidate(
            source_config,
            external_id="posting-1",
            title="Overwritten replay",
            description="This must not replace immutable observation inputs.",
            observed_at=datetime.now(UTC),
        )
        with pytest.raises(PersistenceError):
            RadarScanObservationRepository(session).capture_complete(
                run=repeated_a.collection.run, candidates=(changed_replay,)
            )

    with session_scope(session_factory) as session:
        observed_again = list(
            session.scalars(
                select(RadarScanObservation).where(
                    RadarScanObservation.run_id == repeated_a.collection.run.id
                )
            )
        )
        assert [(item.title, item.description) for item in observed_again] == [
            ("Platform Engineer", "Version A")
        ]
        with pytest.raises(CanonicalVersionConflictError):
            CanonicalJobRepository(session).upsert(
                _candidate(
                    source_config,
                    external_id="posting-1",
                    title="Data Engineer",
                    description="Version B",
                    observed_at=datetime.now(UTC),
                )
            )


def test_partial_and_failed_panel_scans_keep_provenance_without_membership(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    source_config, panel = _register_panel(session_factory, tmp_path)
    partial = _collect_one(
        session_factory,
        source_config,
        panel,
        "Partial role",
        "Partial content",
        terminal=ScanOutcome(status=ScanStatus.PARTIAL, incomplete_reason="fixture partial page"),
    )
    failed = _collect(
        session_factory,
        source_config,
        panel,
        postings=(("posting-1", "Failed role", "Collected before failure"),),
        terminal=ScanFailedError("fixture transport failure"),
    )
    assert partial.collection.run.status is CollectionRunStatus.PARTIAL
    assert failed.collection_failed is True
    assert failed.collection.run.status is CollectionRunStatus.FAILED

    with session_scope(session_factory) as session:
        runs = list(
            session.scalars(select(CollectionRunModel).order_by(CollectionRunModel.started_at))
        )
        assert [run.status for run in runs] == ["partial", "failed"]
        assert all(run.radar_panel_version == panel.panel_version for run in runs)
        assert list(session.scalars(select(RadarScanObservation))) == []


def test_duplicate_ids_and_failure_after_successful_capture_never_publish_evidence(
    session_factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_config, panel = _register_panel(session_factory, tmp_path)
    duplicate = _collect(
        session_factory,
        source_config,
        panel,
        postings=(
            ("duplicate-id", "Same role", "Same content"),
            ("duplicate-id", "Same role", "Same content"),
        ),
        terminal=ScanOutcome(status=ScanStatus.COMPLETE),
    )
    assert duplicate.collection_failed is True
    assert duplicate.collection.run.status is CollectionRunStatus.FAILED

    original_update = SqlAlchemyCollectionRunStore.update_in_session
    injected_after_durable_writes: list[str] = []

    def update_then_raise(
        run_store: SqlAlchemyCollectionRunStore,
        session: Session,
        run: CollectionRun,
    ) -> None:
        original_update(run_store, session, run)
        if run.status is CollectionRunStatus.SUCCEEDED:
            persisted_status = session.scalar(
                select(CollectionRunModel.status).where(CollectionRunModel.id == UUID(run.id))
            )
            observation_count = session.scalar(
                select(func.count())
                .select_from(RadarScanObservation)
                .where(RadarScanObservation.run_id == UUID(run.id))
            )
            assert persisted_status == "succeeded"
            assert observation_count == 1
            injected_after_durable_writes.append(run.id)
            raise RuntimeError("injected failure after capture and succeeded status update")

    monkeypatch.setattr(SqlAlchemyCollectionRunStore, "update_in_session", update_then_raise)
    injected = _collect_one(session_factory, source_config, panel, "Atomic role", "Atomic content")
    assert injected.collection_failed is True
    assert injected.collection.run.status is CollectionRunStatus.FAILED
    assert injected_after_durable_writes == [injected.collection.run.id]

    with session_scope(session_factory) as session:
        runs = list(
            session.scalars(select(CollectionRunModel).order_by(CollectionRunModel.started_at))
        )
        assert [run.status for run in runs] == ["failed", "failed"]
        assert all(run.radar_panel_version == panel.panel_version for run in runs)
        assert list(session.scalars(select(RadarScanObservation))) == []


def test_same_version_config_drift_is_rejected_before_adapter_or_source_sync(
    session_factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_config, panel = _register_panel(session_factory, tmp_path)
    changed_config = source_config.model_copy(update={"board_token": "different-board"})
    changed_manifest_path = tmp_path / "drifted-panel.yaml"
    changed_manifest_path.write_text(
        "panel_version: capture-v1\nsources:\n  - source_key: capture-board\n"
        "    company_key: capture-co\n    source_kind: greenhouse\n",
        encoding="utf-8",
    )
    changed_panel = RadarPanelManifest.load(
        changed_manifest_path, SourcesConfig(sources=[changed_config])
    )
    assert changed_panel.panel_version == panel.panel_version
    assert changed_panel.panel_digest != panel.panel_digest
    with session_scope(session_factory) as session:
        with pytest.raises(RadarPanelVersionConflictError):
            RadarPanelVersionRepository(session).register(changed_panel)
        persisted_source_token = session.execute(
            text("SELECT config ->> 'board_token' FROM sources WHERE key = :key"),
            {"key": "capture-board"},
        )
        assert persisted_source_token.scalar_one() == "capture-token"

    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        "sources:\n  - key: capture-board\n    kind: greenhouse\n"
        "    company_name: Capture Company\n    board_token: different-board\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "career_os.cli._collect_selected",
        lambda **_kwargs: pytest.fail("panel config drift must stop before adapter collection"),
    )
    result = CliRunner().invoke(
        app,
        [
            "collect",
            "--config",
            str(sources_path),
            "--radar-panel-manifest",
            str(changed_manifest_path),
        ],
    )
    assert result.exit_code == 2
    assert "already frozen" in result.output
    with session_scope(session_factory) as session:
        persisted_source_token = session.execute(
            text("SELECT config ->> 'board_token' FROM sources WHERE key = :key"),
            {"key": "capture-board"},
        )
        assert persisted_source_token.scalar_one() == "capture-token"
        assert list(session.scalars(select(CollectionRunModel))) == []


def _register_panel(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> tuple[GreenhouseSourceConfig, RadarPanelManifest]:
    source_config = GreenhouseSourceConfig(
        key="capture-board",
        kind="greenhouse",
        company_name="Capture Company",
        board_token="capture-token",
    )
    manifest_path = tmp_path / "panel.yaml"
    manifest_path.write_text(
        "panel_version: capture-v1\nsources:\n  - source_key: capture-board\n"
        "    company_key: capture-co\n    source_kind: greenhouse\n",
        encoding="utf-8",
    )
    panel = RadarPanelManifest.load(manifest_path, SourcesConfig(sources=[source_config]))
    with session_scope(session_factory) as session:
        SourceConfigurationRepository(session).sync(SourcesConfig(sources=[source_config]))
        RadarPanelVersionRepository(session).register(panel)
    return source_config, panel


def _collect_one(
    session_factory: sessionmaker[Session],
    source_config: GreenhouseSourceConfig,
    panel: RadarPanelManifest,
    title: str,
    description: str,
    *,
    terminal: TerminalSpec | None = None,
) -> IngestionResult:
    return _collect(
        session_factory,
        source_config,
        panel,
        postings=(("posting-1", title, description),),
        terminal=terminal or ScanOutcome(status=ScanStatus.COMPLETE),
    )


def _collect(
    session_factory: sessionmaker[Session],
    source_config: GreenhouseSourceConfig,
    panel: RadarPanelManifest,
    *,
    postings: tuple[PostingSpec, ...],
    terminal: TerminalSpec,
    adapter: _ScriptedGreenhouseAdapter | None = None,
) -> IngestionResult:
    service = GreenhouseIngestionService(
        session_factory=session_factory,
        logger=configure_logging(logger_name="career_os.tests.radar.capture"),
        request_timeout_seconds=1.0,
        operational_policy=CollectionOperationalPolicy(
            max_fetch_drop_ratio=0.99,
            circuit_failure_threshold=20,
            circuit_cooldown_seconds=1,
            freshness_target_seconds=86_400,
        ),
        radar_panel=panel,
    )
    scripted_adapter = adapter or _ScriptedGreenhouseAdapter(source_config, postings, terminal)
    return asyncio.run(service.collect(source_config=source_config, adapter=scripted_adapter))


def _candidate(
    source_config: GreenhouseSourceConfig,
    *,
    external_id: str,
    title: str,
    description: str,
    observed_at: datetime,
) -> CanonicalJobCandidate:
    from career_os.application.ingestion import greenhouse_candidate

    return greenhouse_candidate(
        source_config,
        RawJobPosting(
            source_key=source_config.key,
            source_kind=source_config.kind,
            external_id=external_id,
            source_url=f"https://example.test/jobs/{external_id}",
            apply_url=None,
            payload={"title": title, "content": f"<p>{description}</p>"},
            fetched_at=observed_at,
        ),
    )
