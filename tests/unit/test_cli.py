"""CLI construction and configuration-validation checks without infrastructure I/O."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from career_os.cli import _job_payload, _run_payload, _selected_sources, app
from career_os.config.sources import ConfigurationError, GreenhouseSourceConfig, SourcesConfig


def test_help_paths_construct_without_database_or_network_io(monkeypatch) -> None:
    def unexpected_io(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("CLI help must not initialize persistence")

    monkeypatch.setattr("career_os.cli.create_database_engine", unexpected_io)
    monkeypatch.setattr("career_os.cli.load_settings", unexpected_io)
    monkeypatch.setattr("career_os.cli._greenhouse_adapter", unexpected_io)
    runner = CliRunner()

    for arguments in (
        ["--help"],
        ["collect", "--help"],
        ["sources", "--help"],
        ["sources", "validate", "--help"],
        ["runs", "--help"],
        ["runs", "list", "--help"],
        ["runs", "show", "--help"],
        ["jobs", "--help"],
        ["jobs", "list", "--help"],
    ):
        assert runner.invoke(app, arguments).exit_code == 0


def test_sources_validate_accepts_greenhouse_config_without_database_io(
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
""",
        encoding="utf-8",
    )

    def unexpected_io(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("sources validate must not initialize persistence")

    monkeypatch.setattr("career_os.cli.create_database_engine", unexpected_io)
    result = CliRunner().invoke(app, ["sources", "validate", "--config", str(config)])

    assert result.exit_code == 0
    assert '"registered_kinds": ["greenhouse"]' in result.output
    assert '"sources": 1' in result.output


def test_sources_validate_rejects_configured_kinds_without_an_available_adapter(
    tmp_path: Path,
) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text(
        """
sources:
  - key: future-source
    kind: lever
    company_name: Future Inc.
    site: future
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["sources", "validate", "--config", str(config)])

    assert result.exit_code == 2
    assert "no adapter factory registered for kind(s): lever" in result.output


def test_database_errors_are_reported_with_a_nonzero_exit_without_a_traceback(monkeypatch) -> None:
    def unavailable_database() -> object:
        raise OperationalError("SELECT 1", {}, RuntimeError("database offline"))

    monkeypatch.setattr("career_os.cli._database_session_factory", unavailable_database)
    result = CliRunner().invoke(app, ["runs", "list"])

    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "Traceback" not in result.output


def test_source_selection_uses_enabled_sources_or_explicit_requested_keys() -> None:
    enabled = _source("enabled", enabled=True)
    disabled = _source("disabled", enabled=False)
    config = SourcesConfig(sources=[enabled, disabled])

    assert _selected_sources(config, ()) == (enabled,)
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
        lambda _path: SourcesConfig(sources=[source_config]),
    )
    monkeypatch.setattr("career_os.cli._database_session_factory", lambda: object())
    monkeypatch.setattr("career_os.cli.session_scope", no_database_session)
    monkeypatch.setattr(
        "career_os.cli.SourceConfigurationRepository", FakeSourceConfigurationRepository
    )
    monkeypatch.setattr(
        "career_os.cli.load_settings",
        lambda: SimpleNamespace(log_level="INFO", request_timeout_seconds=1.0),
    )
    monkeypatch.setattr("career_os.cli.configure_logging", lambda **_kwargs: logging.getLogger())
    monkeypatch.setattr("career_os.cli._collect_selected", fake_collect_selected)

    arguments = ["collect"]
    if strict:
        arguments.append("--strict")
    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == expected_exit
    for outcome in outcomes:
        assert f'"status": "{outcome["status"]}"' in result.output


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
        lambda _path: SourcesConfig(sources=[source_config]),
    )
    monkeypatch.setattr("career_os.cli._database_session_factory", lambda: object())
    monkeypatch.setattr("career_os.cli.session_scope", no_database_session)
    monkeypatch.setattr(
        "career_os.cli.SourceConfigurationRepository",
        lambda _session: SimpleNamespace(sync=lambda _sources: None),
    )
    monkeypatch.setattr(
        "career_os.cli.load_settings",
        lambda: SimpleNamespace(log_level="INFO", request_timeout_seconds=1.0),
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
