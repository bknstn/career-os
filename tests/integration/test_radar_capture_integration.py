"""Opt-in PostgreSQL tests for transaction-bound Radar scan capture."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Lock
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

import career_os.radar.aggregate as radar_aggregate
from career_os.application import GreenhouseIngestionService, IngestionResult
from career_os.application.collect import CollectionRun, CollectionRunStatus
from career_os.application.operations import CollectionOperationalPolicy
from career_os.application.radar import RadarBuildService
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
    RadarBuild,
    RadarScanObservation,
)
from career_os.persistence.radar_builds import RadarBuildRepository
from career_os.radar.aggregate import RadarPanelInput
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


def test_radar_build_persists_empty_history_and_uses_registered_manifest_only(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _, panel = _register_panel(session_factory, tmp_path)
    manifest_path = tmp_path / "panel.yaml"
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)

    first = RadarBuildService(session_factory).build(str(manifest_path), cutoff)
    repeated = RadarBuildService(session_factory).build(str(manifest_path), cutoff)

    assert first["status"] == "insufficient"
    assert first["matched_pair_count"] == 0
    assert first["input_digest"] == repeated["input_digest"]
    assert first["run_id"] == repeated["run_id"]
    assert first["panel"]["source_config_digests"] == dict(panel.source_config_digests)
    for window in first["windows"].values():
        assert window["denominator"] is None
        assert window["unclassified_share"] is None
    assert not next(gate for gate in first["gates"]["run"] if gate["code"] == "missing_history")[
        "passed"
    ]

    run_id = UUID(str(first["run_id"]))
    assert RadarBuildService(session_factory).show(run_id) == first
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(RadarBuild)) == 1


def test_radar_build_replays_frozen_inputs_and_ignores_later_redundant_scan(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_config, _ = _register_panel(session_factory, tmp_path)
    panel_manifest = str(tmp_path / "panel.yaml")
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    previous_time = cutoff - timedelta(days=175)
    current_time = cutoff - timedelta(days=85)

    previous = _collect_one(
        session_factory,
        source_config,
        _registered_panel(tmp_path),
        "Platform Engineer",
        "previous frozen description",
    )
    current = _collect_one(
        session_factory,
        source_config,
        _registered_panel(tmp_path),
        "Platform Engineer",
        "current frozen description",
    )
    _set_capture_time(session_factory, previous.collection.run.id, previous_time)
    _set_capture_time(session_factory, current.collection.run.id, current_time)

    service = RadarBuildService(session_factory)
    first = service.build(panel_manifest, cutoff)
    first_run_id = UUID(str(first["run_id"]))
    persisted_copy = service.show(first_run_id)

    with session_factory.begin() as session:
        posting = session.scalar(select(JobPosting).where(JobPosting.external_id == "posting-1"))
        assert posting is not None
        posting.title_raw = "Mutable projection changed after capture"

    redundant = _collect_one(
        session_factory,
        source_config,
        _registered_panel(tmp_path),
        "Redundant later title",
        "this observation is not selected",
    )
    _set_capture_time(
        session_factory, redundant.collection.run.id, previous_time + timedelta(days=1)
    )
    unchanged = service.build(panel_manifest, cutoff)
    assert unchanged["run_id"] == first["run_id"]
    assert unchanged["input_digest"] == first["input_digest"]
    assert service.show(first_run_id) == persisted_copy
    assert persisted_copy is not None
    previous_classifications = persisted_copy["windows"]["previous"]["classifications"]
    assert previous_classifications[0]["title"] == "Platform Engineer"
    assert "previous frozen description" in previous_classifications[0]["description"]

    monkeypatch.setattr(radar_aggregate, "AGGREGATION_ALGORITHM_VERSION", "fixture-next")
    new_version = service.build(panel_manifest, cutoff)
    assert new_version["input_digest"] == first["input_digest"]
    assert new_version["run_id"] != first["run_id"]
    assert new_version["versions"]["aggregation_algorithm"] == "fixture-next"
    assert service.show(first_run_id) == persisted_copy

    newly_filled = _collect_one(
        session_factory,
        source_config,
        _registered_panel(tmp_path),
        "Backend Engineer",
        "new selected slot",
    )
    _set_capture_time(
        session_factory, newly_filled.collection.run.id, previous_time + timedelta(days=8)
    )
    changed = service.build(panel_manifest, cutoff)
    assert changed["input_digest"] != new_version["input_digest"]
    assert changed["run_id"] != new_version["run_id"]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(RadarBuild)) == 3


def test_radar_cli_build_show_and_manifest_errors_against_postgres(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _register_panel(session_factory, tmp_path)
    manifest_path = tmp_path / "panel.yaml"
    cutoff = "2026-01-01T00:00:00Z"
    runner = CliRunner()
    build_args = [
        "radar",
        "build",
        "--panel-manifest",
        str(manifest_path),
        "--cutoff-utc",
        cutoff,
    ]

    first = runner.invoke(app, build_args)
    assert first.exit_code == 0
    assert '"status": "insufficient"' in first.stdout
    persisted_payload = first.stdout
    run_id = str(json.loads(first.stdout)["run_id"])

    repeated = runner.invoke(app, build_args)
    assert repeated.exit_code == 0
    assert repeated.stdout == persisted_payload
    shown = runner.invoke(app, ["radar", "show", "--run-id", run_id])
    assert shown.exit_code == 0
    assert shown.stdout == persisted_payload

    unknown = runner.invoke(app, ["radar", "show", "--run-id", str(UUID(int=999))])
    assert unknown.exit_code == 4

    unregistered_path = tmp_path / "unregistered.yaml"
    unregistered_path.write_text(
        "panel_version: not-registered\nsources:\n  - source_key: capture-board\n"
        "    company_key: capture-co\n    source_kind: greenhouse\n",
        encoding="utf-8",
    )
    unregistered = runner.invoke(
        app,
        [
            "radar",
            "build",
            "--panel-manifest",
            str(unregistered_path),
            "--cutoff-utc",
            cutoff,
        ],
    )
    assert unregistered.exit_code == 2

    changed_path = tmp_path / "changed.yaml"
    changed_path.write_text(
        "panel_version: capture-v1\nsources:\n  - source_key: capture-board\n"
        "    company_key: changed-company\n    source_kind: greenhouse\n",
        encoding="utf-8",
    )
    changed = runner.invoke(
        app,
        [
            "radar",
            "build",
            "--panel-manifest",
            str(changed_path),
            "--cutoff-utc",
            cutoff,
        ],
    )
    assert changed.exit_code == 2


def test_radar_build_rolls_back_a_failure_after_staging_immutable_result(
    session_factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _ = _register_panel(session_factory, tmp_path)
    manifest_path = str(tmp_path / "panel.yaml")
    original_insert = RadarBuildRepository.insert

    def insert_then_raise(
        repository: RadarBuildRepository,
        *,
        key: str,
        panel: RadarPanelInput,
        cutoff: datetime,
        input_digest: str,
        payload: dict[str, object],
    ) -> RadarBuild:
        original_insert(
            repository,
            key=key,
            panel=panel,
            cutoff=cutoff,
            input_digest=input_digest,
            payload=payload,
        )
        raise RuntimeError("injected after JSON result flush")

    monkeypatch.setattr(RadarBuildRepository, "insert", insert_then_raise)
    with pytest.raises(RuntimeError, match="injected after JSON result flush"):
        RadarBuildService(session_factory).build(manifest_path, datetime(2026, 1, 1, tzinfo=UTC))

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(RadarBuild)) == 0


def test_concurrent_identical_radar_builds_return_one_committed_result(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _ = _register_panel(session_factory, tmp_path)
    manifest_path = str(tmp_path / "panel.yaml")
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    original_insert = RadarBuildRepository.insert
    barrier = Barrier(2)
    lock = Lock()
    staged_count = 0

    def synchronized_insert(
        repository: RadarBuildRepository,
        *,
        key: str,
        panel: RadarPanelInput,
        cutoff: datetime,
        input_digest: str,
        payload: dict[str, object],
    ) -> RadarBuild:
        nonlocal staged_count
        with lock:
            should_wait = staged_count < 2
            staged_count += 1
        if should_wait:
            barrier.wait(timeout=15)
        return original_insert(
            repository,
            key=key,
            panel=panel,
            cutoff=cutoff,
            input_digest=input_digest,
            payload=payload,
        )

    monkeypatch.setattr(RadarBuildRepository, "insert", synchronized_insert)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(RadarBuildService(session_factory).build, manifest_path, cutoff)
            for _ in range(2)
        ]
        results = [future.result(timeout=45) for future in futures]

    assert results[0] == results[1]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(RadarBuild)) == 1


def _registered_panel(tmp_path: Path) -> RadarPanelManifest:
    return RadarPanelManifest.load(
        tmp_path / "panel.yaml",
        SourcesConfig(
            sources=[
                GreenhouseSourceConfig(
                    key="capture-board",
                    kind="greenhouse",
                    company_name="Capture Company",
                    board_token="capture-token",
                )
            ]
        ),
    )


def _set_capture_time(
    session_factory: sessionmaker[Session], run_id: str, finished_at: datetime
) -> None:
    with session_factory.begin() as session:
        run = session.get(CollectionRunModel, UUID(run_id))
        assert run is not None
        run.started_at = finished_at - timedelta(seconds=1)
        run.finished_at = finished_at
        for observation in session.scalars(
            select(RadarScanObservation).where(RadarScanObservation.run_id == UUID(run_id))
        ):
            observation.observed_at = finished_at


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
