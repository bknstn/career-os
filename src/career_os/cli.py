"""Thin Typer interface for configured Greenhouse and Lever public boards."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import typer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from career_os.adapters import (
    AdapterRegistration,
    AdapterRegistry,
    AdapterRegistryError,
    GreenhouseAdapter,
    LeverAdapter,
)
from career_os.application import GreenhouseIngestionService, IngestionError, LeverIngestionService
from career_os.config.sources import (
    ConfigurationError,
    GreenhouseSourceConfig,
    LeverSourceConfig,
    SourceConfig,
    SourcesConfig,
    load_sources_config,
)
from career_os.db import (
    DatabaseConfigurationError,
    create_database_engine,
    create_session_factory,
    session_scope,
)
from career_os.domain import SourceAdapter
from career_os.logging import configure_logging, safe_exception
from career_os.persistence import (
    InspectionRepository,
    PersistenceError,
    SourceConfigurationRepository,
)
from career_os.persistence.repositories import JobInspection, RunInspection
from career_os.settings import load_settings

app = typer.Typer(no_args_is_help=True, help="Collect and inspect Career OS job evidence.")
sources_app = typer.Typer(no_args_is_help=True, help="Validate configured public job boards.")
runs_app = typer.Typer(no_args_is_help=True, help="Inspect persisted collection runs.")
jobs_app = typer.Typer(no_args_is_help=True, help="Inspect persisted canonical jobs.")
app.add_typer(sources_app, name="sources")
app.add_typer(runs_app, name="runs")
app.add_typer(jobs_app, name="jobs")

DEFAULT_SOURCES_CONFIG = Path("config/sources.yaml")
_CONFIG_OPTION = typer.Option(DEFAULT_SOURCES_CONFIG, "--config", exists=False, dir_okay=False)
_SOURCE_OPTION = typer.Option([], "--source", help="Collect only this configured source key.")
_STRICT_OPTION = typer.Option(
    False,
    "--strict",
    help="Fail if any selected scan is partial or failed.",
)


def adapter_registry() -> AdapterRegistry:
    """Return the explicit available adapter registration set without I/O."""
    return AdapterRegistry(
        (
            AdapterRegistration.for_config(
                kind="greenhouse",
                config_type=GreenhouseSourceConfig,
                factory=_greenhouse_adapter,
            ),
            AdapterRegistration.for_config(
                kind="lever",
                config_type=LeverSourceConfig,
                factory=_lever_adapter,
            ),
        )
    )


def _greenhouse_adapter(source_config: GreenhouseSourceConfig) -> SourceAdapter:
    return cast(SourceAdapter, GreenhouseAdapter(config=source_config))


def _lever_adapter(source_config: LeverSourceConfig) -> SourceAdapter:
    return cast(SourceAdapter, LeverAdapter(config=source_config))


def _load_and_validate_sources(config: Path) -> SourcesConfig:
    sources_config = load_sources_config(config)
    adapter_registry().validate_sources(sources_config)
    return sources_config


def _database_session_factory() -> sessionmaker[Session]:
    settings = load_settings()
    return create_session_factory(create_database_engine(settings))


def _emit_error(error: BaseException, *, exit_code: int) -> None:
    typer.echo(f"Error: {safe_exception(error).message}", err=True)
    raise typer.Exit(code=exit_code)


def _json_echo(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default))


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _selected_sources(
    sources_config: SourcesConfig, requested_keys: Iterable[str]
) -> tuple[SourceConfig, ...]:
    requested = tuple(dict.fromkeys(requested_keys))
    sources_by_key = {source.key: source for source in sources_config.sources}
    unknown = sorted(set(requested) - set(sources_by_key))
    if unknown:
        raise ConfigurationError(
            f"requested source key(s) are not configured: {', '.join(unknown)}"
        )
    selected: tuple[SourceConfig, ...]
    if requested:
        selected = tuple(sources_by_key[key] for key in requested)
    else:
        selected = tuple(source for source in sources_config.sources if source.enabled)
    if not selected:
        raise ConfigurationError("no enabled sources were selected")
    return selected


@sources_app.command("validate")
def sources_validate(
    config: Path = _CONFIG_OPTION,
) -> None:
    """Validate source YAML and confirm that every kind has an available adapter."""
    try:
        sources_config = _load_and_validate_sources(config)
    except (ConfigurationError, AdapterRegistryError) as error:
        _emit_error(error, exit_code=2)
    _json_echo(
        {
            "config": str(config),
            "registered_kinds": sorted(adapter_registry().registered_kinds),
            "sources": len(sources_config.sources),
        }
    )


@app.command("collect")
def collect(
    config: Path = _CONFIG_OPTION,
    source: list[str] = _SOURCE_OPTION,
    strict: bool = _STRICT_OPTION,
) -> None:
    """Collect all enabled sources, or explicitly named sources, into PostgreSQL."""
    try:
        sources_config = _load_and_validate_sources(config)
        selected_sources = _selected_sources(sources_config, source)
        session_factory = _database_session_factory()
        with session_scope(session_factory) as session:
            SourceConfigurationRepository(session).sync(sources_config)
        settings = load_settings()
        logger = configure_logging(level=settings.log_level)
        registry = adapter_registry()
        outcomes = asyncio.run(
            _collect_selected(
                selected_sources=selected_sources,
                registry=registry,
                session_factory=session_factory,
                logger=logger,
                request_timeout_seconds=settings.request_timeout_seconds,
            )
        )
    except (ConfigurationError, AdapterRegistryError) as error:
        _emit_error(error, exit_code=2)
    except (DatabaseConfigurationError, IngestionError, PersistenceError, SQLAlchemyError) as error:
        _emit_error(error, exit_code=1)

    for outcome in outcomes:
        _json_echo(outcome)
    unsafe = any(outcome["status"] != "succeeded" for outcome in outcomes)
    all_failed = all(outcome["status"] == "failed" for outcome in outcomes)
    if strict and unsafe:
        raise typer.Exit(code=1)
    if all_failed:
        raise typer.Exit(code=1)


async def _collect_selected(
    *,
    selected_sources: Iterable[SourceConfig],
    registry: AdapterRegistry,
    session_factory: sessionmaker[Session],
    logger: Any,
    request_timeout_seconds: float,
) -> list[dict[str, object]]:
    outcomes: list[dict[str, object]] = []
    for source_config in selected_sources:
        adapter = registry.build(source_config)
        if isinstance(source_config, GreenhouseSourceConfig):
            result = await GreenhouseIngestionService(
                session_factory=session_factory,
                logger=logger,
                request_timeout_seconds=request_timeout_seconds,
            ).collect(source_config=source_config, adapter=adapter)
        elif isinstance(source_config, LeverSourceConfig):
            result = await LeverIngestionService(
                session_factory=session_factory,
                logger=logger,
                request_timeout_seconds=request_timeout_seconds,
            ).collect(source_config=source_config, adapter=adapter)
        else:  # Registry coverage is validated before selection, retained for type-safe isolation.
            raise IngestionError(f"no ingestion composition for source kind: {source_config.kind}")
        outcomes.append(
            {
                "source_key": source_config.key,
                "run_id": result.collection.run.id,
                "status": result.collection.run.status.value,
                "counts": result.counts.as_fields(),
                "lifecycle": result.lifecycle.action.value,
                "collection_failed": result.collection_failed,
                "incomplete_reason": result.collection.run.incomplete_reason,
                "error_summary": result.collection.run.error_summary,
            }
        )
    return outcomes


@runs_app.command("list")
def runs_list(
    failed: bool = typer.Option(False, "--failed", help="Show only failed source runs."),
) -> None:
    """List persisted runs, newest first."""
    try:
        session_factory = _database_session_factory()
        with session_scope(session_factory) as session:
            runs = InspectionRepository(session).list_runs(failed_only=failed)
            payload = [_run_payload(run) for run in runs]
    except (DatabaseConfigurationError, PersistenceError, SQLAlchemyError) as error:
        _emit_error(error, exit_code=1)
    _json_echo(payload)


@runs_app.command("show")
def runs_show(run_id: UUID) -> None:
    """Show one persisted run by UUID."""
    try:
        session_factory = _database_session_factory()
        with session_scope(session_factory) as session:
            run = InspectionRepository(session).get_run(run_id)
    except (DatabaseConfigurationError, PersistenceError, SQLAlchemyError) as error:
        _emit_error(error, exit_code=1)
    if run is None:
        typer.echo(f"Run not found: {run_id}", err=True)
        raise typer.Exit(code=1)
    _json_echo(_run_payload(run))


@jobs_app.command("list")
def jobs_list(
    active: bool = typer.Option(True, "--active/--all", help="Limit to active jobs (the default)."),
    company: str | None = typer.Option(None, "--company", help="Exact company name."),
) -> None:
    """List canonical jobs with optional active and company filters."""
    try:
        session_factory = _database_session_factory()
        with session_scope(session_factory) as session:
            jobs = InspectionRepository(session).list_jobs(
                active_only=active,
                company_name=company,
            )
            payload = [_job_payload(job) for job in jobs]
    except (DatabaseConfigurationError, PersistenceError, SQLAlchemyError) as error:
        _emit_error(error, exit_code=1)
    _json_echo(payload)


def _run_payload(inspection: RunInspection) -> dict[str, object]:
    run = inspection.run
    return {
        "id": run.id,
        "source_key": inspection.source_key,
        "source_kind": inspection.source_kind,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "counts": {
            "items_fetched": run.items_fetched,
            "inserted": run.inserted,
            "updated": run.updated,
            "unchanged": run.unchanged,
            "inactivated": run.inactivated,
            "failed_items": run.failed_items,
            "http_requests": run.http_requests,
            "retry_count": run.retry_count,
            "rate_limit_events": run.rate_limit_events,
            "normalization_errors": run.normalization_errors,
        },
        "normalizer_version": run.normalizer_version,
        "taxonomy_version": run.taxonomy_version,
        "error_summary": run.error_summary,
        "incomplete_reason": run.incomplete_reason,
    }


def _job_payload(inspection: JobInspection) -> dict[str, object]:
    job = inspection.job
    return {
        "id": job.id,
        "source_key": inspection.source_key,
        "source_kind": inspection.source_kind,
        "external_id": job.external_id,
        "company_name": job.company_name,
        "title": job.title_raw,
        "status": job.status,
        "source_url": job.source_url,
        "apply_url": job.apply_url,
        "first_seen_at": job.first_seen_at,
        "last_seen_at": job.last_seen_at,
        "inactive_at": job.inactive_at,
    }


def main() -> None:
    """Run the command-line app; constructing it performs no persistence or HTTP I/O."""
    app()


if __name__ == "__main__":  # pragma: no cover - packaging entry point covers this path.
    main()
