"""CLI construction and configuration-validation checks without infrastructure I/O."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from career_os import cli
from career_os.application.market import MarketDataConsistencyError
from career_os.application.operations import SourceOperationalStatus
from career_os.cli import _job_payload, _run_payload, _selected_sources, app
from career_os.config.sources import (
    AshbySourceConfig,
    ConfigurationError,
    GreenhouseSourceConfig,
    LeverSourceConfig,
    SourcesConfig,
    WorkablePublicSourceConfig,
)
from career_os.db import DatabaseConfigurationError
from career_os.persistence import PersistenceError


def test_help_paths_construct_without_database_or_network_io(monkeypatch) -> None:
    def unexpected_io(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("CLI help must not initialize persistence")

    monkeypatch.setattr("career_os.cli.create_database_engine", unexpected_io)
    monkeypatch.setattr("career_os.cli.load_settings", unexpected_io)
    monkeypatch.setattr("career_os.cli._greenhouse_adapter", unexpected_io)
    monkeypatch.setattr("career_os.cli._lever_adapter", unexpected_io)
    monkeypatch.setattr("career_os.cli._workable_public_adapter", unexpected_io)
    runner = CliRunner()

    for arguments in (
        ["--help"],
        ["collect", "--help"],
        ["enrich", "--help"],
        ["sources", "--help"],
        ["sources", "validate", "--help"],
        ["taxonomy", "--help"],
        ["taxonomy", "validate", "--help"],
        ["market", "--help"],
        ["market", "report", "--help"],
        ["operations", "--help"],
        ["operations", "status", "--help"],
        ["runs", "--help"],
        ["runs", "list", "--help"],
        ["runs", "show", "--help"],
        ["jobs", "--help"],
        ["jobs", "list", "--help"],
    ):
        assert runner.invoke(app, arguments).exit_code == 0


def test_operations_status_emits_json_and_strict_fails_for_stale_enabled_source(
    monkeypatch,
) -> None:
    status = SourceOperationalStatus(
        source_key="example",
        source_kind="greenhouse",
        enabled=True,
        active_jobs=3,
        latest_status="failed",
        latest_finished_at=datetime(2026, 8, 23, 10, tzinfo=UTC),
        last_success_at=datetime(2026, 8, 22, 10, tzinfo=UTC),
        last_success_age_seconds=93_600.0,
        stale=True,
        consecutive_unsafe_runs=1,
        circuit_open=False,
        latest_items_fetched=0,
        latest_http_requests=3,
        latest_retry_count=2,
        latest_rate_limit_events=1,
        latest_failed_items=0,
        latest_normalization_errors=0,
    )

    @contextmanager
    def fake_scope(_factory: object):
        yield object()

    class FakeRepository:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def list_source_statuses(self, **_kwargs: object) -> tuple[SourceOperationalStatus, ...]:
            return (status,)

    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "_database_session_factory", lambda _settings: object())
    monkeypatch.setattr(cli, "session_scope", fake_scope)
    monkeypatch.setattr(cli, "OperationalStatusRepository", FakeRepository)

    normal = CliRunner().invoke(app, ["operations", "status"])
    strict = CliRunner().invoke(app, ["operations", "status", "--strict"])

    assert normal.exit_code == 0
    assert '"source_key": "example"' in normal.output
    assert '"latest_http_requests": 3' in normal.output
    assert strict.exit_code == 1


def test_market_report_writes_only_complete_rendered_output(monkeypatch, tmp_path: Path) -> None:
    output = tmp_path / "report.md"
    filters = SimpleNamespace(name="market")
    taxonomy = SimpleNamespace(version="1.0.0")
    profile = SimpleNamespace(name="profile")
    report = object()

    class FakeSession:
        def __enter__(self) -> FakeSession:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def connection(self, **_kwargs: object) -> object:
            return object()

    class FakeReportService:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def calculate(self, **_kwargs: object) -> object:
            return report

    monkeypatch.setattr(cli, "load_market_filters", lambda _path: filters)
    monkeypatch.setattr(cli, "load_skill_taxonomy", lambda _path: taxonomy)
    monkeypatch.setattr(cli, "load_profile", lambda _path, **_kwargs: profile)
    monkeypatch.setattr(cli, "_database_session_factory", lambda _settings: FakeSession)
    monkeypatch.setattr(cli, "MarketReportService", FakeReportService)
    monkeypatch.setattr(cli, "render_markdown_report", lambda value: "# complete\n")

    result = CliRunner().invoke(
        app,
        [
            "market",
            "report",
            "--market",
            "market.yaml",
            "--profile",
            "profile.yaml",
            "--format",
            "markdown",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert output.read_text(encoding="utf-8") == "# complete\n"


def test_market_report_failure_is_nonzero_and_preserves_existing_output(
    monkeypatch, tmp_path: Path
) -> None:
    output = tmp_path / "report.md"
    output.write_text("previous\n", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "load_market_filters",
        lambda _path: (_ for _ in ()).throw(ConfigurationError("invalid market")),
    )

    result = CliRunner().invoke(
        app,
        [
            "market",
            "report",
            "--market",
            "bad.yaml",
            "--profile",
            "profile.yaml",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 2
    assert "Error:" in result.output
    assert output.read_text(encoding="utf-8") == "previous\n"


def test_market_report_invariant_failure_does_not_replace_existing_output(
    monkeypatch, tmp_path: Path
) -> None:
    output = tmp_path / "report.md"
    output.write_text("previous\n", encoding="utf-8")

    class FakeSession:
        def __enter__(self) -> FakeSession:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def connection(self, **_kwargs: object) -> object:
            return object()

    class FailingReportService:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def calculate(self, **_kwargs: object) -> object:
            raise MarketDataConsistencyError("stale report evidence")

    monkeypatch.setattr(cli, "load_market_filters", lambda _path: SimpleNamespace(name="market"))
    monkeypatch.setattr(cli, "load_skill_taxonomy", lambda _path: SimpleNamespace(version="1.0.0"))
    monkeypatch.setattr(
        cli, "load_profile", lambda _path, **_kwargs: SimpleNamespace(name="profile")
    )
    monkeypatch.setattr(cli, "_database_session_factory", lambda _settings: FakeSession)
    monkeypatch.setattr(cli, "MarketReportService", FailingReportService)

    result = CliRunner().invoke(
        app,
        [
            "market",
            "report",
            "--market",
            "market.yaml",
            "--profile",
            "profile.yaml",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 1
    assert "stale report evidence" in result.output
    assert output.read_text(encoding="utf-8") == "previous\n"


def test_sources_validate_accepts_greenhouse_lever_and_ashby_config_without_database_io(
    monkeypatch, tmp_path: Path
) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text(
        """
sources:
  - key: example-greenhouse
    kind: greenhouse
    company_name: Example Inc.
    board_token: example
  - key: example-lever
    kind: lever
    company_name: Example Lever
    site: example
  - key: example-ashby
    kind: ashby
    company_name: Example Ashby
    board_name: example
""",
        encoding="utf-8",
    )

    def unexpected_io(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("sources validate must not initialize persistence")

    monkeypatch.setattr("career_os.cli.create_database_engine", unexpected_io)
    result = CliRunner().invoke(app, ["sources", "validate", "--config", str(config)])

    assert result.exit_code == 0
    assert '"registered_kinds": ["ashby", "greenhouse", "lever"]' in result.output
    assert '"sources": 3' in result.output


def test_taxonomy_validate_has_deterministic_offline_summary(monkeypatch) -> None:
    def unexpected_io(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("taxonomy validate must not initialize settings or persistence")

    monkeypatch.setattr("career_os.cli.load_settings", unexpected_io)
    monkeypatch.setattr("career_os.cli.create_database_engine", unexpected_io)
    result = CliRunner().invoke(app, ["taxonomy", "validate"])

    assert result.exit_code == 0
    assert result.output == (
        '{"cases": 13, "categories": 14, "skills": 113, "taxonomy_version": "1.0.2"}\n'
    )


def test_enrich_cli_emits_deterministic_success_and_partial_failure_json(monkeypatch) -> None:
    taxonomy = SimpleNamespace(version="1.0.0")
    first_id = uuid4()
    second_id = uuid4()
    results = iter(
        (
            SimpleNamespace(selected=2, enriched=2, unchanged=0, failed_ids=()),
            SimpleNamespace(selected=2, enriched=1, unchanged=0, failed_ids=(first_id, second_id)),
        )
    )

    class FakeService:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def enrich_stale(self) -> object:
            return next(results)

    monkeypatch.setattr(cli, "load_skill_taxonomy", lambda _path: taxonomy)
    monkeypatch.setattr(cli, "SkillExtractor", lambda _taxonomy: object())
    monkeypatch.setattr(cli, "_database_session_factory", lambda _settings: object())
    monkeypatch.setattr(cli, "JobEnrichmentService", FakeService)

    runner = CliRunner()
    success = runner.invoke(app, ["enrich"])
    failure = runner.invoke(app, ["enrich"])

    assert success.exit_code == 0
    assert success.output == (
        '{"enriched": 2, "failed_ids": [], "normalizer_version": "job-normalization-v3", '
        '"selected": 2, "taxonomy_version": "1.0.0", "unchanged": 0}\n'
    )
    assert failure.exit_code == 1
    assert failure.output == (
        f'{{"enriched": 1, "failed_ids": ["{first_id}", "{second_id}"], '
        '"normalizer_version": "job-normalization-v3", "selected": 2, '
        '"taxonomy_version": "1.0.0", "unchanged": 0}\n'
    )


def test_enrich_cli_maps_taxonomy_and_database_errors_to_documented_exits(monkeypatch) -> None:
    monkeypatch.setattr(
        cli, "load_skill_taxonomy", lambda _path: (_ for _ in ()).throw(ConfigurationError("bad"))
    )
    invalid = CliRunner().invoke(app, ["enrich"])
    assert invalid.exit_code == 2

    monkeypatch.setattr(cli, "load_skill_taxonomy", lambda _path: SimpleNamespace(version="1.0.0"))
    monkeypatch.setattr(
        cli,
        "_database_session_factory",
        lambda _settings: (_ for _ in ()).throw(DatabaseConfigurationError("offline")),
    )
    database = CliRunner().invoke(app, ["enrich"])
    assert database.exit_code == 1

    class FailingService:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def enrich_stale(self) -> object:
            raise PersistenceError("enrichment failed")

    monkeypatch.setattr(cli, "_database_session_factory", lambda _settings: object())
    monkeypatch.setattr(cli, "SkillExtractor", lambda _taxonomy: object())
    monkeypatch.setattr(cli, "JobEnrichmentService", FailingService)
    service = CliRunner().invoke(app, ["enrich"])
    assert service.exit_code == 1


def test_taxonomy_validate_reports_sanitized_invalid_configuration(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid-skills.yaml"
    invalid.write_text("version: invalid\nskills: []\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["taxonomy", "validate", "--config", str(invalid)])

    assert result.exit_code == 2
    assert "Invalid skill taxonomy" in result.output
    assert "invalid" not in result.output.split("Error: ", maxsplit=1)[-1]


def test_taxonomy_validate_rejects_invalid_golden_corpus(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid-golden.yaml"
    invalid.write_text("taxonomy_version: 1.0.0\ncases: []\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["taxonomy", "validate", "--golden", str(invalid)])

    assert result.exit_code == 2
    assert "Invalid golden extraction corpus" in result.output


def test_sources_validate_rejects_configured_kinds_without_an_available_adapter(
    tmp_path: Path,
) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text(
        """
sources:
  - key: future-source
    kind: workable_public
    company_name: Future Inc.
    account_subdomain: future
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["sources", "validate", "--config", str(config)])

    assert result.exit_code == 2
    assert "no adapter factory registered for kind(s): workable_public" in result.output


def test_workable_validation_is_feature_gated_before_database_or_adapter_io(
    monkeypatch, tmp_path: Path
) -> None:
    config = tmp_path / "workable.yaml"
    config.write_text(
        "sources:\n  - key: work\n    kind: workable_public\n"
        "    company_name: Work\n    account_subdomain: work\n"
    )
    monkeypatch.setattr(
        "career_os.cli.create_database_engine",
        lambda *_args: (_ for _ in ()).throw(AssertionError()),
    )
    monkeypatch.setattr(
        "career_os.cli._workable_public_adapter",
        lambda *_args: (_ for _ in ()).throw(AssertionError()),
    )
    result = CliRunner().invoke(app, ["sources", "validate", "--config", str(config)])
    assert result.exit_code == 2 and "workable_public" in result.output


def test_workable_feature_flag_on_validates_four_source_example_without_database(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CAREER_OS_ENABLE_WORKABLE_PUBLIC", "true")
    monkeypatch.setattr(
        "career_os.cli.create_database_engine",
        lambda *_args: (_ for _ in ()).throw(AssertionError()),
    )
    result = CliRunner().invoke(
        app,
        [
            "sources",
            "validate",
            "--config",
            "config/sources.greenhouse-lever-ashby-workable.example.yaml",
        ],
    )
    assert result.exit_code == 0 and '"workable_public"' in result.output


def test_database_errors_are_reported_with_a_nonzero_exit_without_a_traceback(monkeypatch) -> None:
    def unavailable_database() -> object:
        raise OperationalError("SELECT 1", {}, RuntimeError("database offline"))

    monkeypatch.setattr(
        "career_os.cli._database_session_factory", lambda _settings: unavailable_database()
    )
    result = CliRunner().invoke(app, ["runs", "list"])

    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "Traceback" not in result.output


def test_source_selection_uses_enabled_sources_or_explicit_requested_keys() -> None:
    enabled = _source("enabled", enabled=True)
    disabled = _source("disabled", enabled=False)
    lever = _lever_source("lever", enabled=True)
    config = SourcesConfig(sources=[enabled, disabled, lever])

    assert _selected_sources(config, ()) == (enabled, lever)
    assert _selected_sources(config, ("disabled",)) == (disabled,)
    assert _selected_sources(config, ("disabled", "disabled", "enabled")) == (
        disabled,
        enabled,
    )


def test_source_selection_rejects_unknown_or_empty_enabled_selection() -> None:
    disabled = _source("disabled", enabled=False)
    config = SourcesConfig(sources=[disabled])

    with pytest.raises(ConfigurationError, match="no enabled"):
        _selected_sources(config, ())
    with pytest.raises(ConfigurationError, match="not configured"):
        _selected_sources(config, ("missing",))


def test_mixed_source_dispatch_uses_each_ingestion_composition_and_preserves_results(
    monkeypatch,
) -> None:
    greenhouse = _source("greenhouse", enabled=True)
    lever = _lever_source("lever", enabled=True)
    ashby = _ashby_source("ashby", enabled=True)
    workable = _workable_source("workable", enabled=True)
    calls: list[tuple[str, str, str]] = []

    def result_for(source_key: str, status: str) -> SimpleNamespace:
        return SimpleNamespace(
            collection=SimpleNamespace(
                run=SimpleNamespace(
                    id=f"{source_key}-run",
                    status=SimpleNamespace(value=status),
                    incomplete_reason="fixture partial" if status == "partial" else None,
                    error_summary=None,
                )
            ),
            counts=SimpleNamespace(as_fields=lambda: {"items_fetched": 1}),
            lifecycle=SimpleNamespace(action=SimpleNamespace(value="skipped_unsafe")),
            collection_failed=status == "failed",
        )

    class FakeGreenhouseIngestionService:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def collect(self, *, source_config: GreenhouseSourceConfig, **_kwargs: object):
            calls.append(("greenhouse", source_config.key, "partial"))
            return result_for(source_config.key, "partial")

    class FakeLeverIngestionService:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def collect(self, *, source_config: LeverSourceConfig, **_kwargs: object):
            calls.append(("lever", source_config.key, "succeeded"))
            return result_for(source_config.key, "succeeded")

    class FakeAshbyIngestionService:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def collect(self, *, source_config: AshbySourceConfig, **_kwargs: object):
            calls.append(("ashby", source_config.key, "succeeded"))
            return result_for(source_config.key, "succeeded")

    class FakeWorkablePublicIngestionService:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def collect(
            self, *, source_config: WorkablePublicSourceConfig, **_kwargs: object
        ) -> SimpleNamespace:
            calls.append(("workable_public", source_config.key, "failed"))
            return result_for(source_config.key, "failed")

    monkeypatch.setattr("career_os.cli.GreenhouseIngestionService", FakeGreenhouseIngestionService)
    monkeypatch.setattr("career_os.cli.LeverIngestionService", FakeLeverIngestionService)
    monkeypatch.setattr("career_os.cli.AshbyIngestionService", FakeAshbyIngestionService)
    monkeypatch.setattr(
        "career_os.cli.WorkablePublicIngestionService", FakeWorkablePublicIngestionService
    )

    outcomes = asyncio.run(
        cli._collect_selected(
            selected_sources=(greenhouse, lever, ashby, workable),
            registry=cli.adapter_registry(enable_workable_public=True),
            session_factory=object(),
            logger=logging.getLogger(),
            request_timeout_seconds=1.0,
        )
    )

    assert calls == [
        ("greenhouse", "greenhouse", "partial"),
        ("lever", "lever", "succeeded"),
        ("ashby", "ashby", "succeeded"),
        ("workable_public", "workable", "failed"),
    ]
    assert [(outcome["source_key"], outcome["status"]) for outcome in outcomes] == [
        ("greenhouse", "partial"),
        ("lever", "succeeded"),
        ("ashby", "succeeded"),
        ("workable", "failed"),
    ]


@pytest.mark.parametrize(
    ("outcomes", "strict", "expected_exit"),
    [
        (
            [
                {"source_key": "partial", "status": "partial"},
                {"source_key": "success", "status": "succeeded"},
            ],
            False,
            0,
        ),
        (
            [
                {"source_key": "partial", "status": "partial"},
                {"source_key": "success", "status": "succeeded"},
            ],
            True,
            1,
        ),
        ([{"source_key": "failed", "status": "failed"}], False, 1),
    ],
)
def test_collect_exit_behavior_reports_all_source_statuses(
    monkeypatch,
    outcomes: list[dict[str, object]],
    strict: bool,
    expected_exit: int,
) -> None:
    source_config = _source("example", enabled=True)
    loaded_settings: list[object] = []
    settings = SimpleNamespace(
        log_level="INFO", request_timeout_seconds=1.0, enable_workable_public=False
    )

    @contextmanager
    def no_database_session(_session_factory: object):
        yield object()

    class FakeSourceConfigurationRepository:
        def __init__(self, session: object) -> None:
            del session

        def sync(self, sources_config: SourcesConfig) -> None:
            assert sources_config.sources == [source_config]

    async def fake_collect_selected(**_kwargs: object) -> list[dict[str, object]]:
        return outcomes

    monkeypatch.setattr(
        "career_os.cli._load_and_validate_sources",
        lambda _path, **_kwargs: SourcesConfig(sources=[source_config]),
    )
    monkeypatch.setattr(
        "career_os.cli._database_session_factory",
        lambda passed_settings: (
            object()
            if passed_settings is settings
            else (_ for _ in ()).throw(AssertionError("collect must reuse loaded settings"))
        ),
    )
    monkeypatch.setattr("career_os.cli.session_scope", no_database_session)
    monkeypatch.setattr(
        "career_os.cli.SourceConfigurationRepository", FakeSourceConfigurationRepository
    )

    def load_once() -> SimpleNamespace:
        loaded_settings.append(settings)
        return settings

    monkeypatch.setattr("career_os.cli.load_settings", load_once)
    monkeypatch.setattr("career_os.cli.configure_logging", lambda **_kwargs: logging.getLogger())
    monkeypatch.setattr("career_os.cli._collect_selected", fake_collect_selected)

    arguments = ["collect"]
    if strict:
        arguments.append("--strict")
    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == expected_exit
    for outcome in outcomes:
        assert f'"status": "{outcome["status"]}"' in result.output
    assert loaded_settings == [settings]


def test_collect_output_includes_terminal_diagnostics(monkeypatch) -> None:
    source_config = _source("example", enabled=True)

    @contextmanager
    def no_database_session(_session_factory: object):
        yield object()

    async def fake_collect_selected(**_kwargs: object) -> list[dict[str, object]]:
        return [
            {
                "source_key": "example",
                "status": "partial",
                "incomplete_reason": "malformed item",
                "error_summary": None,
            }
        ]

    monkeypatch.setattr(
        "career_os.cli._load_and_validate_sources",
        lambda _path, **_kwargs: SourcesConfig(sources=[source_config]),
    )
    monkeypatch.setattr("career_os.cli._database_session_factory", lambda _settings: object())
    monkeypatch.setattr("career_os.cli.session_scope", no_database_session)
    monkeypatch.setattr(
        "career_os.cli.SourceConfigurationRepository",
        lambda _session: SimpleNamespace(sync=lambda _sources: None),
    )
    monkeypatch.setattr(
        "career_os.cli.load_settings",
        lambda: SimpleNamespace(
            log_level="INFO", request_timeout_seconds=1.0, enable_workable_public=False
        ),
    )
    monkeypatch.setattr("career_os.cli.configure_logging", lambda **_kwargs: logging.getLogger())
    monkeypatch.setattr("career_os.cli._collect_selected", fake_collect_selected)

    result = CliRunner().invoke(app, ["collect"])

    assert result.exit_code == 0
    assert '"incomplete_reason": "malformed item"' in result.output
    assert '"error_summary": null' in result.output


def test_inspection_payloads_include_joined_source_identity_and_diagnostics() -> None:
    now = datetime(2026, 8, 19, tzinfo=UTC)
    run_payload = _run_payload(
        SimpleNamespace(
            source_key="alpha",
            source_kind="greenhouse",
            run=SimpleNamespace(
                id=uuid4(),
                status="partial",
                started_at=now,
                finished_at=now,
                items_fetched=2,
                inserted=1,
                updated=0,
                unchanged=0,
                inactivated=0,
                failed_items=1,
                http_requests=2,
                retry_count=1,
                rate_limit_events=1,
                normalization_errors=0,
                normalizer_version="test-v1",
                taxonomy_version="none-v1",
                error_summary=None,
                incomplete_reason="one item was malformed",
            ),
        )
    )
    job_payload = _job_payload(
        SimpleNamespace(
            source_key="alpha",
            source_kind="greenhouse",
            job=SimpleNamespace(
                id=uuid4(),
                external_id="job-1",
                company_name="Alpha Inc.",
                title_raw="Engineer",
                status="active",
                source_url=None,
                apply_url=None,
                first_seen_at=now,
                last_seen_at=now,
                inactive_at=None,
            ),
        )
    )

    assert (run_payload["source_key"], run_payload["source_kind"]) == ("alpha", "greenhouse")
    assert run_payload["incomplete_reason"] == "one item was malformed"
    assert run_payload["counts"] == {
        "items_fetched": 2,
        "inserted": 1,
        "updated": 0,
        "unchanged": 0,
        "inactivated": 0,
        "failed_items": 1,
        "http_requests": 2,
        "retry_count": 1,
        "rate_limit_events": 1,
        "normalization_errors": 0,
    }
    assert (job_payload["source_key"], job_payload["source_kind"]) == ("alpha", "greenhouse")


def _source(key: str, *, enabled: bool) -> GreenhouseSourceConfig:
    return GreenhouseSourceConfig(
        key=key,
        kind="greenhouse",
        company_name=f"{key.title()} Inc.",
        board_token=f"{key}-board",
        enabled=enabled,
    )


def _lever_source(key: str, *, enabled: bool) -> LeverSourceConfig:
    return LeverSourceConfig(
        key=key,
        kind="lever",
        company_name=f"{key.title()} Lever",
        site=f"{key}-site",
        enabled=enabled,
    )


def _ashby_source(key: str, *, enabled: bool) -> AshbySourceConfig:
    return AshbySourceConfig(
        key=key,
        kind="ashby",
        company_name=f"{key.title()} Ashby",
        board_name=f"{key}-board",
        enabled=enabled,
    )


def _workable_source(key: str, *, enabled: bool) -> WorkablePublicSourceConfig:
    return WorkablePublicSourceConfig(
        key=key,
        kind="workable_public",
        company_name=f"{key.title()} Workable",
        account_subdomain=f"{key}-account",
        enabled=enabled,
    )
