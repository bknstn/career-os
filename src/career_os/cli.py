"""Thin Typer interface for configured Greenhouse, Lever, Ashby, and opt-in Workable boards."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, cast
from uuid import UUID

import typer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from career_os.adapters import (
    AdapterRegistration,
    AdapterRegistry,
    AdapterRegistryError,
    AshbyAdapter,
    GreenhouseAdapter,
    LeverAdapter,
    WorkablePublicAdapter,
)
from career_os.application import (
    AshbyIngestionService,
    GreenhouseIngestionService,
    IngestionError,
    LeverIngestionService,
    WorkablePublicIngestionService,
)
from career_os.application.career_review import build_career_review, load_review_plan
from career_os.application.compensation import build_compensation_comparison
from career_os.application.eligibility_funnel import (
    EligibilityFunnelConsistencyError,
    EligibilityFunnelService,
)
from career_os.application.job_enrichment import JobEnrichmentService
from career_os.application.market import MarketDataConsistencyError
from career_os.application.operations import (
    CollectionOperationalPolicy,
    SourceCollectionUnavailableError,
)
from career_os.application.profile_fit import ProfileComparisonConsistencyError
from career_os.application.report import MarketReportService
from career_os.config.career_profiles import load_career_profile
from career_os.config.compensation_supplements import (
    append_compensation_record,
    dump_compensation_ledger,
    load_compensation_ledger,
    load_compensation_record,
    new_compensation_ledger,
)
from career_os.config.markets import load_market_filters
from career_os.config.profiles import load_profile
from career_os.config.skills import load_golden_extraction_corpus, load_skill_taxonomy
from career_os.config.sources import (
    AshbySourceConfig,
    ConfigurationError,
    GreenhouseSourceConfig,
    LeverSourceConfig,
    SourceConfig,
    SourcesConfig,
    WorkablePublicSourceConfig,
    load_sources_config,
)
from career_os.config.target_universes import load_target_universe
from career_os.config.vacancy_evidence import load_vacancy_snapshot
from career_os.db import (
    DatabaseConfigurationError,
    create_database_engine,
    create_session_factory,
    session_scope,
)
from career_os.domain import SourceAdapter
from career_os.domain.hybrid_results import HybridResult, QualityReviewInput
from career_os.domain.target_universe_results import TargetUniverseResult
from career_os.logging import configure_logging, safe_exception
from career_os.normalization.jobs import JOB_NORMALIZER_VERSION
from career_os.normalization.skills import SkillExtractor
from career_os.persistence import (
    InspectionRepository,
    PersistenceError,
    SourceConfigurationRepository,
)
from career_os.persistence.operations import OperationalStatusRepository
from career_os.persistence.report_queries import ReportEvidenceConsistencyError
from career_os.persistence.repositories import JobInspection, RunInspection
from career_os.reporting import render_eligibility_funnel, render_markdown_report
from career_os.reporting.career_review import render_career_review
from career_os.reporting.compensation import render_compensation_comparison
from career_os.settings import Settings, load_settings

app = typer.Typer(no_args_is_help=True, help="Collect and inspect Career OS job evidence.")
sources_app = typer.Typer(no_args_is_help=True, help="Validate configured public job boards.")
runs_app = typer.Typer(no_args_is_help=True, help="Inspect persisted collection runs.")
jobs_app = typer.Typer(no_args_is_help=True, help="Inspect persisted canonical jobs.")
taxonomy_app = typer.Typer(no_args_is_help=True, help="Validate versioned skill taxonomy files.")
market_app = typer.Typer(no_args_is_help=True, help="Build evidence-backed market reports.")
operations_app = typer.Typer(no_args_is_help=True, help="Inspect collection health and metrics.")
profile_app = typer.Typer(no_args_is_help=True, help="Validate H3 career profile evidence.")
career_app = typer.Typer(no_args_is_help=True, help="Review one vacancy against H3 evidence.")
compensation_app = typer.Typer(
    no_args_is_help=True, help="Record and compare manual compensation evidence offline."
)
career_app.add_typer(compensation_app, name="compensation")
app.add_typer(career_app, name="career")
app.add_typer(profile_app, name="profile")
app.add_typer(sources_app, name="sources")
app.add_typer(runs_app, name="runs")
app.add_typer(jobs_app, name="jobs")
app.add_typer(taxonomy_app, name="taxonomy")
app.add_typer(market_app, name="market")
app.add_typer(operations_app, name="operations")

DEFAULT_SOURCES_CONFIG = Path("config/sources.yaml")
DEFAULT_SKILL_TAXONOMY_CONFIG = Path("config/taxonomy/skills.v1.yaml")
DEFAULT_GOLDEN_EXTRACTION_CORPUS = Path("tests/fixtures/taxonomy/golden_extraction.v1.yaml")
_CONFIG_OPTION = typer.Option(DEFAULT_SOURCES_CONFIG, "--config", exists=False, dir_okay=False)
_TAXONOMY_CONFIG_OPTION = typer.Option(
    DEFAULT_SKILL_TAXONOMY_CONFIG, "--config", exists=False, dir_okay=False
)
_GOLDEN_CORPUS_OPTION = typer.Option(
    DEFAULT_GOLDEN_EXTRACTION_CORPUS, "--golden", exists=False, dir_okay=False
)
_ENRICH_TAXONOMY_OPTION = typer.Option(
    DEFAULT_SKILL_TAXONOMY_CONFIG, "--taxonomy", exists=False, dir_okay=False
)
_SOURCE_OPTION = typer.Option([], "--source", help="Collect only this configured source key.")
_STRICT_OPTION = typer.Option(
    False,
    "--strict",
    help="Fail if any selected scan is partial or failed.",
)


class ReportFormat(StrEnum):
    """Output formats supported by the Phase 4 report command."""

    MARKDOWN = "markdown"


_REPORT_MARKET_OPTION = typer.Option(None, "--market", exists=False, dir_okay=False)
_REPORT_TARGET_UNIVERSE_OPTION = typer.Option(
    None,
    "--target-universe",
    exists=False,
    dir_okay=False,
    help="Explicit H2 Target Universe v1 YAML configuration.",
)
_REPORT_PROFILE_OPTION = typer.Option(..., "--profile", exists=False, dir_okay=False)
_REPORT_FORMAT_OPTION = typer.Option(ReportFormat.MARKDOWN, "--format")
_REPORT_OUTPUT_OPTION = typer.Option(..., "--output", exists=False, dir_okay=False)
_REPORT_QUALITY_REVIEW_OPTION = typer.Option(
    None,
    "--quality-review",
    exists=False,
    dir_okay=False,
    help="Optional strict JSON review artifact for H1 quality gates.",
)


def adapter_registry(*, enable_workable_public: bool = False) -> AdapterRegistry:
    """Return the explicit available adapter registration set without I/O."""
    registrations = [
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
        AdapterRegistration.for_config(
            kind="ashby",
            config_type=AshbySourceConfig,
            factory=_ashby_adapter,
        ),
    ]
    if enable_workable_public:
        registrations.append(
            AdapterRegistration.for_config(
                kind="workable_public",
                config_type=WorkablePublicSourceConfig,
                factory=_workable_public_adapter,
            )
        )
    return AdapterRegistry(registrations)


def _greenhouse_adapter(source_config: GreenhouseSourceConfig) -> SourceAdapter:
    return cast(SourceAdapter, GreenhouseAdapter(config=source_config))


def _lever_adapter(source_config: LeverSourceConfig) -> SourceAdapter:
    return cast(SourceAdapter, LeverAdapter(config=source_config))


def _ashby_adapter(source_config: AshbySourceConfig) -> SourceAdapter:
    return cast(SourceAdapter, AshbyAdapter(config=source_config))


def _workable_public_adapter(source_config: WorkablePublicSourceConfig) -> SourceAdapter:
    return cast(SourceAdapter, WorkablePublicAdapter(config=source_config))


def _load_and_validate_sources(
    config: Path, *, enable_workable_public: bool = False
) -> SourcesConfig:
    sources_config = load_sources_config(config)
    adapter_registry(enable_workable_public=enable_workable_public).validate_sources(sources_config)
    return sources_config


def _database_session_factory(settings: Settings) -> sessionmaker[Session]:
    """Create a persistence factory from settings already loaded by a command."""
    return create_session_factory(create_database_engine(settings))


def _operational_policy(settings: Settings) -> CollectionOperationalPolicy:
    return CollectionOperationalPolicy(
        max_fetch_drop_ratio=getattr(settings, "max_fetch_drop_ratio", 0.5),
        circuit_failure_threshold=getattr(settings, "circuit_failure_threshold", 3),
        circuit_cooldown_seconds=getattr(settings, "circuit_cooldown_seconds", 900),
        freshness_target_seconds=getattr(settings, "freshness_target_seconds", 86_400),
    )


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


def _load_quality_review(path: Path | None) -> QualityReviewInput | None:
    """Load the optional strict review document without persisting it."""
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigurationError(f"Quality review file does not exist: {path}") from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"Quality review file is not valid JSON: {path}") from error
    if not isinstance(payload, dict):
        raise ConfigurationError("Quality review must contain a JSON object")
    allowed = {"ref", "reviewed_job_ids", "false_positive_job_ids"}
    if set(payload) - allowed:
        raise ConfigurationError("Quality review contains unknown keys")
    try:
        reviewed = tuple(UUID(value) for value in payload.get("reviewed_job_ids", ()))
        false_positive = tuple(UUID(value) for value in payload.get("false_positive_job_ids", ()))
        return QualityReviewInput(
            ref=payload.get("ref", ""),
            reviewed_job_ids=reviewed,
            false_positive_job_ids=false_positive,
        )
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"Invalid quality review: {error}") from error


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


_VACANCY_OPTION = typer.Option(..., "--vacancy", dir_okay=False)
_REVIEW_PLAN_OPTION = typer.Option(..., "--review-plan", dir_okay=False)
_CAREER_FORMAT_OPTION = typer.Option("markdown", "--format", help="markdown or json")
_COMPENSATION_LEDGER_OPTION = typer.Option(..., "--ledger", exists=False, dir_okay=False)
_COMPENSATION_RECORD_OPTION = typer.Option(..., "--record", exists=False, dir_okay=False)
_COMPENSATION_CURRENT_ROLE_OPTION = typer.Option(..., "--current-role")
_COMPENSATION_OPPORTUNITY_OPTION = typer.Option(..., "--opportunity")


@career_app.command("review")
def career_review(
    profile: Path = _REPORT_PROFILE_OPTION,
    vacancy: Path = _VACANCY_OPTION,
    review_plan: Path = _REVIEW_PLAN_OPTION,
    taxonomy: Path = _ENRICH_TAXONOMY_OPTION,
    output: Path = _REPORT_OUTPUT_OPTION,
    output_format: str = _CAREER_FORMAT_OPTION,
) -> None:
    """Assemble a digest-bound, manually mapped single-vacancy review offline."""
    try:
        if output_format not in ("markdown", "json"):
            raise ConfigurationError("format must be markdown or json")
        if output.resolve() in {p.resolve() for p in (profile, vacancy, review_plan, taxonomy)}:
            raise ConfigurationError("output must not overwrite an input")
        result = build_career_review(
            load_career_profile(profile, taxonomy=load_skill_taxonomy(taxonomy)),
            load_vacancy_snapshot(vacancy),
            load_review_plan(review_plan),
        )
        rendered = (
            result.model_dump_json(indent=2) + "\n"
            if output_format == "json"
            else render_career_review(result)
        )
        _atomic_write_text(output, rendered)
    except (ConfigurationError, OSError) as error:
        _emit_error(error, exit_code=2)
    _json_echo({"output": str(output), "constraint_status": result.constraint_status})


@compensation_app.command("validate")
def compensation_validate(
    ledger: Path = _COMPENSATION_LEDGER_OPTION,
) -> None:
    """Emit a strict compensation ledger without touching H3/H4 inputs or a database."""
    try:
        result = load_compensation_ledger(ledger)
    except ConfigurationError as error:
        _emit_error(error, exit_code=2)
    _json_echo(result.model_dump(mode="json"))


@compensation_app.command("record")
def compensation_record(
    ledger: Path = _COMPENSATION_LEDGER_OPTION,
    record: Path = _COMPENSATION_RECORD_OPTION,
) -> None:
    """Atomically append one manual observation; corrections must use explicit supersedes."""
    try:
        if ledger.resolve() == record.resolve():
            raise ConfigurationError("ledger and record must be different files")
        new_record = load_compensation_record(record)
        result = (
            append_compensation_record(load_compensation_ledger(ledger), new_record)
            if ledger.exists()
            else new_compensation_ledger(new_record)
        )
        _atomic_write_text(ledger, dump_compensation_ledger(result))
    except (ConfigurationError, OSError) as error:
        _emit_error(error, exit_code=2)
    _json_echo({"ledger": str(ledger), "observations": len(result.observations)})


@compensation_app.command("compare")
def compensation_compare(
    ledger: Path = _COMPENSATION_LEDGER_OPTION,
    profile: Path = _REPORT_PROFILE_OPTION,
    current_role: str = _COMPENSATION_CURRENT_ROLE_OPTION,
    opportunity: str = _COMPENSATION_OPPORTUNITY_OPTION,
    output: Path = _REPORT_OUTPUT_OPTION,
    taxonomy: Path = _ENRICH_TAXONOMY_OPTION,
    output_format: str = _CAREER_FORMAT_OPTION,
) -> None:
    """Write a Decimal-only compensation comparison from manual supplemental evidence."""
    try:
        if output_format not in ("markdown", "json"):
            raise ConfigurationError("format must be markdown or json")
        if output.resolve() in {path.resolve() for path in (ledger, profile, taxonomy)}:
            raise ConfigurationError("output must not overwrite an input")
        result = build_compensation_comparison(
            load_compensation_ledger(ledger),
            load_career_profile(profile, taxonomy=load_skill_taxonomy(taxonomy)),
            current_role_id=current_role,
            opportunity_id=opportunity,
        )
        rendered = (
            result.model_dump_json(indent=2) + "\n"
            if output_format == "json"
            else render_compensation_comparison(result)
        )
        _atomic_write_text(output, rendered)
    except (ConfigurationError, OSError) as error:
        _emit_error(error, exit_code=2)
    _json_echo(
        {
            "output": str(output),
            "comparable": result.comparable,
            "floor_status": result.floor.status,
        }
    )


@profile_app.command("validate")
def profile_validate(
    config: Path = _REPORT_PROFILE_OPTION,
    taxonomy: Path = _ENRICH_TAXONOMY_OPTION,
) -> None:
    """Emit the validated H3 profile as JSON without database or network access."""
    try:
        profile = load_career_profile(config, taxonomy=load_skill_taxonomy(taxonomy))
    except ConfigurationError as error:
        _emit_error(error, exit_code=2)
    _json_echo(profile.model_dump(mode="json"))


@taxonomy_app.command("validate")
def taxonomy_validate(
    config: Path = _TAXONOMY_CONFIG_OPTION,
    golden: Path = _GOLDEN_CORPUS_OPTION,
) -> None:
    """Validate taxonomy and golden-case structure without extraction or infrastructure I/O."""
    try:
        taxonomy = load_skill_taxonomy(config)
        corpus = load_golden_extraction_corpus(golden, taxonomy=taxonomy)
    except ConfigurationError as error:
        _emit_error(error, exit_code=2)
    _json_echo(
        {
            "taxonomy_version": taxonomy.version,
            "skills": len(taxonomy.skills),
            "categories": len(taxonomy.categories),
            "cases": len(corpus.cases),
        }
    )


@sources_app.command("validate")
def sources_validate(
    config: Path = _CONFIG_OPTION,
) -> None:
    """Validate source YAML and confirm that every kind has an available adapter."""
    try:
        settings = load_settings()
        sources_config = _load_and_validate_sources(
            config, enable_workable_public=settings.enable_workable_public
        )
    except (ConfigurationError, AdapterRegistryError) as error:
        _emit_error(error, exit_code=2)
    _json_echo(
        {
            "config": str(config),
            "registered_kinds": sorted(
                adapter_registry(
                    enable_workable_public=settings.enable_workable_public
                ).registered_kinds
            ),
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
        settings = load_settings()
        sources_config = _load_and_validate_sources(
            config, enable_workable_public=settings.enable_workable_public
        )
        selected_sources = _selected_sources(sources_config, source)
        session_factory = _database_session_factory(settings)
        with session_scope(session_factory) as session:
            SourceConfigurationRepository(session).sync(sources_config)
        logger = configure_logging(level=settings.log_level)
        registry = adapter_registry(enable_workable_public=settings.enable_workable_public)
        outcomes = asyncio.run(
            _collect_selected(
                selected_sources=selected_sources,
                registry=registry,
                session_factory=session_factory,
                logger=logger,
                request_timeout_seconds=settings.request_timeout_seconds,
                operational_policy=_operational_policy(settings),
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


@app.command("enrich")
def enrich(taxonomy: Path = _ENRICH_TAXONOMY_OPTION) -> None:
    """Enrich only stale stored jobs, without source fetches or network access."""
    try:
        skill_taxonomy = load_skill_taxonomy(taxonomy)
        session_factory = _database_session_factory(load_settings())
        result = JobEnrichmentService(
            session_factory=session_factory,
            extractor=SkillExtractor(skill_taxonomy),
        ).enrich_stale()
    except ConfigurationError as error:
        _emit_error(error, exit_code=2)
    except (DatabaseConfigurationError, PersistenceError, SQLAlchemyError) as error:
        _emit_error(error, exit_code=1)
    _json_echo(
        {
            "selected": result.selected,
            "enriched": result.enriched,
            "unchanged": result.unchanged,
            "failed_ids": [str(job_id) for job_id in result.failed_ids],
            "normalizer_version": JOB_NORMALIZER_VERSION,
            "taxonomy_version": skill_taxonomy.version,
        }
    )
    if result.failed_ids:
        raise typer.Exit(code=1)


@market_app.command("report")
def market_report(
    market: Path | None = _REPORT_MARKET_OPTION,
    target_universe: Path | None = _REPORT_TARGET_UNIVERSE_OPTION,
    profile: Path = _REPORT_PROFILE_OPTION,
    output_format: ReportFormat = _REPORT_FORMAT_OPTION,
    output: Path = _REPORT_OUTPUT_OPTION,
    taxonomy: Path = _ENRICH_TAXONOMY_OPTION,
    quality_review: Path | None = _REPORT_QUALITY_REVIEW_OPTION,
) -> None:
    """Write one reconciled market/profile report from local config files."""
    try:
        if (market is None) == (target_universe is None):
            raise ConfigurationError("provide exactly one of --market or --target-universe")
        filters = load_market_filters(market) if market is not None else None
        universe = load_target_universe(target_universe) if target_universe is not None else None
        skill_taxonomy = load_skill_taxonomy(taxonomy)
        loaded_profile = load_profile(profile, taxonomy=skill_taxonomy)
        review = _load_quality_review(quality_review)
        report: HybridResult | TargetUniverseResult
        session_factory = _database_session_factory(load_settings())
        with session_factory() as session:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            service = MarketReportService(
                session=session,
                taxonomy=skill_taxonomy,
                normalizer_version=JOB_NORMALIZER_VERSION,
            )
            if market is not None:
                assert filters is not None
                if review is None:
                    report = service.calculate(filters=filters, profile=loaded_profile)
                else:
                    report = service.calculate(
                        filters=filters, profile=loaded_profile, quality_review=review
                    )
            else:
                assert universe is not None
                if review is None:
                    report = service.calculate_target_universe(
                        target_universe=universe, profile=loaded_profile
                    )
                else:
                    report = service.calculate_target_universe(
                        target_universe=universe,
                        profile=loaded_profile,
                        quality_review=review,
                    )
        if output_format is not ReportFormat.MARKDOWN:  # pragma: no cover - enum is exhaustive.
            raise ConfigurationError(f"unsupported report format: {output_format}")
        rendered = render_markdown_report(report)
        _atomic_write_text(output, rendered)
    except ConfigurationError as error:
        _emit_error(error, exit_code=2)
    except (
        DatabaseConfigurationError,
        MarketDataConsistencyError,
        ProfileComparisonConsistencyError,
        ReportEvidenceConsistencyError,
        PersistenceError,
        SQLAlchemyError,
        OSError,
        ValueError,
    ) as error:
        _emit_error(error, exit_code=1)


@market_app.command("audit")
def market_audit(
    market: Path = _REPORT_MARKET_OPTION,
    output: Path = _REPORT_OUTPUT_OPTION,
) -> None:
    """Write one reconciled eligibility-funnel audit from current stored jobs."""
    try:
        filters = load_market_filters(market)
        session_factory = _database_session_factory(load_settings())
        with session_factory() as session:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            audit = EligibilityFunnelService(session=session).calculate(filters)
        _atomic_write_text(output, render_eligibility_funnel(audit))
    except ConfigurationError as error:
        _emit_error(error, exit_code=2)
    except (
        DatabaseConfigurationError,
        EligibilityFunnelConsistencyError,
        PersistenceError,
        SQLAlchemyError,
        OSError,
        ValueError,
    ) as error:
        _emit_error(error, exit_code=1)


def _atomic_write_text(path: Path, value: str) -> None:
    """Atomically replace a report only after complete UTF-8 content is flushed."""
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(value)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


async def _collect_selected(
    *,
    selected_sources: Iterable[SourceConfig],
    registry: AdapterRegistry,
    session_factory: sessionmaker[Session],
    logger: Any,
    request_timeout_seconds: float,
    operational_policy: CollectionOperationalPolicy | None = None,
) -> list[dict[str, object]]:
    policy = operational_policy or CollectionOperationalPolicy()
    outcomes: list[dict[str, object]] = []
    for source_config in selected_sources:
        adapter = registry.build(source_config)
        try:
            if isinstance(source_config, GreenhouseSourceConfig):
                result = await GreenhouseIngestionService(
                    session_factory=session_factory,
                    logger=logger,
                    request_timeout_seconds=request_timeout_seconds,
                    operational_policy=policy,
                ).collect(source_config=source_config, adapter=adapter)
            elif isinstance(source_config, LeverSourceConfig):
                result = await LeverIngestionService(
                    session_factory=session_factory,
                    logger=logger,
                    request_timeout_seconds=request_timeout_seconds,
                    operational_policy=policy,
                ).collect(source_config=source_config, adapter=adapter)
            elif isinstance(source_config, AshbySourceConfig):
                result = await AshbyIngestionService(
                    session_factory=session_factory,
                    logger=logger,
                    request_timeout_seconds=request_timeout_seconds,
                    operational_policy=policy,
                ).collect(source_config=source_config, adapter=adapter)
            elif isinstance(source_config, WorkablePublicSourceConfig):
                result = await WorkablePublicIngestionService(
                    session_factory=session_factory,
                    logger=logger,
                    request_timeout_seconds=request_timeout_seconds,
                    operational_policy=policy,
                ).collect(source_config=source_config, adapter=adapter)
            else:  # Registry coverage is validated before selection.
                raise IngestionError(
                    f"no ingestion composition for source kind: {source_config.kind}"
                )
        except SourceCollectionUnavailableError as error:
            outcomes.append(
                {
                    "source_key": source_config.key,
                    "run_id": None,
                    "status": "failed",
                    "counts": {},
                    "lifecycle": "not_started",
                    "collection_failed": True,
                    "incomplete_reason": None,
                    "error_summary": str(error),
                }
            )
            continue
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


@operations_app.command("status")
def operations_status(
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Exit non-zero when any enabled source is stale or circuit-open.",
    ),
) -> None:
    """Print collection freshness and low-cardinality HTTP metrics as JSON."""
    try:
        settings = load_settings()
        session_factory = _database_session_factory(settings)
        with session_scope(session_factory) as session:
            statuses = OperationalStatusRepository(
                session=session,
                policy=_operational_policy(settings),
            ).list_source_statuses(now=datetime.now(UTC))
    except ConfigurationError as error:
        _emit_error(error, exit_code=2)
    except (DatabaseConfigurationError, PersistenceError, SQLAlchemyError) as error:
        _emit_error(error, exit_code=1)
    _json_echo([asdict(status) for status in statuses])
    if strict and any(
        status.enabled and (status.stale or status.circuit_open) for status in statuses
    ):
        raise typer.Exit(code=1)


@runs_app.command("list")
def runs_list(
    failed: bool = typer.Option(False, "--failed", help="Show only failed source runs."),
) -> None:
    """List persisted runs, newest first."""
    try:
        session_factory = _database_session_factory(load_settings())
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
        session_factory = _database_session_factory(load_settings())
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
        session_factory = _database_session_factory(load_settings())
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
