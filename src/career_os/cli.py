"""Thin Typer interface for configured Greenhouse, Lever, Ashby, and opt-in Workable boards."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal, cast
from uuid import UUID

import typer
from sqlalchemy import select, text
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
from career_os.application.radar import RadarBuildPersistenceError, RadarBuildService
from career_os.application.report import MarketReportService
from career_os.application.research_collection import ResearchCollectionService
from career_os.application.research_evidence import (
    ResearchEvidenceService,
    SqlResearchEvidenceReadStore,
)
from career_os.application.research_monitor import (
    DEFAULT_RESEARCH_MONITOR_STATE_PATH,
    ResearchMonitor,
    ResearchMonitorError,
)
from career_os.application.research_workflow import (
    ResearchWorkflowPayloadError,
    parse_claim_drafts,
    parse_monthly_review,
)
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
from career_os.config.radar_panel import RadarPanelManifest
from career_os.config.research_sources import ResearchConfigurationError, ResearchSourceRegistry
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
from career_os.domain.research import ResearchClaimReviewState
from career_os.domain.target_universe_results import TargetUniverseResult
from career_os.logging import configure_logging, safe_exception
from career_os.normalization.jobs import JOB_NORMALIZER_VERSION
from career_os.normalization.skills import SkillExtractor
from career_os.persistence import (
    InspectionRepository,
    PersistenceError,
    RadarPanelVersionConflictError,
    RadarPanelVersionRepository,
    SourceConfigurationRepository,
)
from career_os.persistence.models import ResearchAcquisitionAttempt
from career_os.persistence.operations import OperationalStatusRepository
from career_os.persistence.report_queries import ReportEvidenceConsistencyError
from career_os.persistence.repositories import JobInspection, RunInspection
from career_os.persistence.research import ResearchRepository
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
research_app = typer.Typer(
    no_args_is_help=True, help="Collect and inspect research evidence sources."
)
radar_app = typer.Typer(
    no_args_is_help=True, help="Build and inspect persisted role Radar results."
)
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
app.add_typer(radar_app, name="radar")
app.add_typer(research_app, name="research")

DEFAULT_SOURCES_CONFIG = Path("config/sources.yaml")
DEFAULT_SKILL_TAXONOMY_CONFIG = Path("config/taxonomy/skills.v1.yaml")
DEFAULT_GOLDEN_EXTRACTION_CORPUS = Path("tests/fixtures/taxonomy/golden_extraction.v1.yaml")
DEFAULT_RESEARCH_REGISTRY = Path("config/radar/research.sources.v1.yaml")
_CONFIG_OPTION = typer.Option(DEFAULT_SOURCES_CONFIG, "--config", exists=False, dir_okay=False)
_TAXONOMY_CONFIG_OPTION = typer.Option(
    DEFAULT_SKILL_TAXONOMY_CONFIG, "--config", exists=False, dir_okay=False
)
_GOLDEN_CORPUS_OPTION = typer.Option(
    DEFAULT_GOLDEN_EXTRACTION_CORPUS, "--golden", exists=False, dir_okay=False
)
_RESEARCH_REGISTRY_OPTION = typer.Option(
    DEFAULT_RESEARCH_REGISTRY,
    "--config",
    exists=False,
    dir_okay=False,
    help="Strict allowlisted research publisher registry.",
)
_RESEARCH_PLAN_SOURCE_OPTION = typer.Option(
    None, "--source", help="Show only this configured research source key (repeatable)."
)
_RESEARCH_COLLECT_SOURCE_OPTION = typer.Option(
    None, "--source", help="Collect only this enabled research source key (repeatable)."
)
_RESEARCH_TRIGGER_OPTION = typer.Option(
    "manual", "--trigger", help="Durable run purpose recorded with the registry snapshot."
)
_RESEARCH_DRAFT_FILE_ARGUMENT = typer.Argument(
    ...,
    help="Strict claim-draft JSON file; pass - to read the import payload from standard input.",
)
_RESEARCH_CLAIM_ID_ARGUMENT = typer.Argument(..., help="Persisted candidate claim UUID.")
_RESEARCH_REVIEW_STATE_ARGUMENT = typer.Argument(...)
_RESEARCH_DOCUMENT_ID_ARGUMENT = typer.Argument(...)
_RESEARCH_TEXT_DIGEST_ARGUMENT = typer.Argument(...)
_RESEARCH_BOOTSTRAP_OPTION = typer.Option(
    False,
    "--bootstrap",
    help="On a first run only, write initial real-date weekly and monthly products.",
)
_RESEARCH_STATE_PATH_OPTION = typer.Option(
    DEFAULT_RESEARCH_MONITOR_STATE_PATH,
    "--state",
    help="Monitor state path; defaults to the local Radar research state file.",
)
_RESEARCH_DISPOSITION_OPTION = typer.Option(..., "--disposition")
_RESEARCH_REVIEWER_OPTION = typer.Option(..., "--reviewer")
_RESEARCH_REASON_OPTION = typer.Option(..., "--reason")
_RESEARCH_MONTHLY_REVIEW_ARGUMENT = typer.Argument(..., exists=True, dir_okay=False)
_ENRICH_TAXONOMY_OPTION = typer.Option(
    DEFAULT_SKILL_TAXONOMY_CONFIG, "--taxonomy", exists=False, dir_okay=False
)
_SOURCE_OPTION = typer.Option([], "--source", help="Collect only this configured source key.")
_RADAR_PANEL_MANIFEST_OPTION = typer.Option(
    None,
    "--radar-panel-manifest",
    exists=False,
    dir_okay=False,
    help="Opt in to immutable Career Radar panel scan evidence.",
)
_STRICT_OPTION = typer.Option(
    False,
    "--strict",
    help="Fail if any selected scan is partial or failed.",
)
_RADAR_MANIFEST_OPTION = typer.Option(
    ...,
    "--panel-manifest",
    exists=False,
    dir_okay=False,
    help="Registered semantic panel manifest used to capture the fixed role sample.",
)
_RADAR_CUTOFF_OPTION = typer.Option(..., "--cutoff-utc")
_RADAR_RUN_ID_OPTION = typer.Option(..., "--run-id")
_RFC3339_CUTOFF = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$"
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


def _research_snapshot(session_factory: sessionmaker[Session]) -> dict[str, object]:
    """Read every research document and claim from one read-only database snapshot."""
    with session_scope(session_factory) as session:
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        session.execute(text("SET TRANSACTION READ ONLY"))
        service = ResearchEvidenceService(SqlResearchEvidenceReadStore(session))
        documents: list[dict[str, object]] = []
        first_documents = service.documents(page=1, page_size=100)
        documents.extend(cast(list[dict[str, object]], first_documents["items"]))
        document_total = cast(int, first_documents["total"])
        for page in range(2, (document_total + 99) // 100 + 1):
            response = service.documents(page=page, page_size=100)
            documents.extend(cast(list[dict[str, object]], response["items"]))

        claims: list[dict[str, object]] = []
        first_claims = service.claims(question=None, page=1, page_size=100)
        claims.extend(cast(list[dict[str, object]], first_claims["items"]))
        claim_total = cast(int, first_claims["total"])
        for page in range(2, (claim_total + 99) // 100 + 1):
            response = service.claims(question=None, page=page, page_size=100)
            claims.extend(cast(list[dict[str, object]], response["items"]))
        if len(documents) != document_total or len(claims) != claim_total:
            raise RuntimeError("research snapshot pagination returned an incomplete result")
    return {"documents": documents, "claims": claims}


def _research_source_failures(
    session_factory: sessionmaker[Session], run_id: UUID
) -> list[dict[str, object]]:
    """Return deduplicated safe failure facets for one collector run."""
    with session_scope(session_factory) as session:
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        session.execute(text("SET TRANSACTION READ ONLY"))
        rows = session.execute(
            select(
                ResearchAcquisitionAttempt.source_key,
                ResearchAcquisitionAttempt.outcome,
                ResearchAcquisitionAttempt.error_code,
                ResearchAcquisitionAttempt.http_status,
            )
            .where(
                ResearchAcquisitionAttempt.run_id == run_id,
                ResearchAcquisitionAttempt.outcome.notin_(("discovered", "acquired")),
            )
            .distinct()
            .order_by(
                ResearchAcquisitionAttempt.source_key,
                ResearchAcquisitionAttempt.outcome,
                ResearchAcquisitionAttempt.error_code,
                ResearchAcquisitionAttempt.http_status,
            )
        ).mappings()
        return [
            {
                "source_key": row["source_key"],
                "outcome": row["outcome"],
                "error_code": row["error_code"],
                "http_status": row["http_status"],
            }
            for row in rows
        ]


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
    radar_panel_manifest: Path | None = _RADAR_PANEL_MANIFEST_OPTION,
) -> None:
    """Collect all enabled sources, or explicitly named sources, into PostgreSQL."""
    try:
        settings = load_settings()
        sources_config = _load_and_validate_sources(
            config, enable_workable_public=settings.enable_workable_public
        )
        selected_sources = _selected_sources(sources_config, source)
        radar_panel = (
            RadarPanelManifest.load(radar_panel_manifest, sources_config)
            if radar_panel_manifest is not None
            else None
        )
        if radar_panel is not None:
            radar_panel.require_sources(selected_sources)
        session_factory = _database_session_factory(settings)
        with session_scope(session_factory) as session:
            if radar_panel is not None:
                RadarPanelVersionRepository(session).register(radar_panel)
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
                radar_panel=radar_panel,
            )
        )
    except (ConfigurationError, AdapterRegistryError) as error:
        _emit_error(error, exit_code=2)
    except RadarPanelVersionConflictError as error:
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


@research_app.command("plan")
def research_plan(
    config: Path = _RESEARCH_REGISTRY_OPTION,
    source: list[str] | None = _RESEARCH_PLAN_SOURCE_OPTION,
) -> None:
    """Print bounded source URLs and limits without database or network access."""
    try:
        registry = ResearchSourceRegistry.load(config)
        selected_keys = tuple(dict.fromkeys(source or ()))
        if selected_keys:
            selected = []
            for key in selected_keys:
                selected_source = registry.get(key)
                if not selected_source.enabled:
                    raise ResearchConfigurationError(f"disabled source cannot be planned: {key}")
                selected.append(selected_source)
            sources = tuple(selected)
        else:
            sources = registry.enabled_sources
    except ResearchConfigurationError as error:
        _emit_error(error, exit_code=2)
    _json_echo(
        {
            "registry_version": registry.version,
            "registry_sha256": registry.digest,
            "network_requests_performed": 0,
            "database_reads_or_writes_performed": 0,
            "limits": {
                "max_total_download_bytes": 64 * 1024 * 1024,
                "max_wire_requests": 96,
                "max_daily_document_bodies": 12,
                "max_backfill_document_bodies": 32,
                "max_index_candidates_per_source": 8,
                "unfetched_retry_interval_hours": 24,
                "acquired_document_revisit_interval_days": 7,
                "max_redirects": 4,
                "retries": 1,
                "minimum_host_interval_seconds": 0.2,
                "request_timeout_seconds": 12,
                "run_deadline_seconds": 15 * 60,
                "robots_policy": (
                    "robots policy fetched once per exact host and evaluated per path for "
                    "CareerOSResearch; supports REP groups, *, terminal $, longest-match allow "
                    "ties, and URI octet normalization; malformed or unavailable policy "
                    "fails closed"
                ),
            },
            "sources": [
                {
                    "key": selected_source.key,
                    "publisher": selected_source.publisher,
                    "max_document_bytes": selected_source.max_response_bytes,
                    "seed_documents": list(selected_source.candidate_document_urls),
                    "discovery_urls": list(selected_source.discovery_urls),
                    "document_aliases": [
                        alias.model_dump(mode="json") for alias in selected_source.document_aliases
                    ],
                }
                for selected_source in sources
            ],
        }
    )


@research_app.command("seed")
def research_seed(
    source_key: str = typer.Argument(..., help="Enabled research source registry key."),
    url: str = typer.Argument(..., help="Exact official HTTPS document URL."),
    title: str | None = typer.Option(
        None, "--title", help="Visible title supplied by the operator."
    ),
    config: Path = _RESEARCH_REGISTRY_OPTION,
) -> None:
    """Store an operator-selected official URL as a candidate; fetch it on a later run."""
    try:
        registry = ResearchSourceRegistry.load(config)
        settings = load_settings()
        engine = create_database_engine(settings)
        try:
            session_factory = create_session_factory(engine)
            candidate_id = ResearchCollectionService(
                session_factory, registry
            ).add_manual_candidate(source_key, url, title)
        finally:
            engine.dispose()
    except (ResearchConfigurationError, ConfigurationError, ValueError) as error:
        _emit_error(error, exit_code=2)
    except (DatabaseConfigurationError, SQLAlchemyError) as error:
        _emit_error(error, exit_code=1)
    _json_echo(
        {
            "candidate_id": candidate_id,
            "source_key": source_key,
            "status": "discovered",
            "next_step": "career-os research collect --trigger manual",
        }
    )


@research_app.command("collect")
def research_collect(
    trigger: Literal["daily", "manual", "backfill"] = _RESEARCH_TRIGGER_OPTION,
    source: list[str] | None = _RESEARCH_COLLECT_SOURCE_OPTION,
    config: Path = _RESEARCH_REGISTRY_OPTION,
) -> None:
    """Acquire allowlisted source documents and persist exact bodies and run outcomes."""
    try:
        registry = ResearchSourceRegistry.load(config)
        settings = load_settings()
        engine = create_database_engine(settings)
        try:
            session_factory = create_session_factory(engine)
            result = ResearchCollectionService(session_factory, registry).collect(
                trigger=trigger,
                source_keys=source,
            )
        finally:
            engine.dispose()
    except (ResearchConfigurationError, ConfigurationError, ValueError) as error:
        _emit_error(error, exit_code=2)
    except (DatabaseConfigurationError, SQLAlchemyError) as error:
        _emit_error(error, exit_code=1)
    _json_echo(asdict(result))
    if result.status != "succeeded":
        raise typer.Exit(code=1)


@research_app.command("import-claims")
def research_import_claims(
    draft_file: Path = _RESEARCH_DRAFT_FILE_ARGUMENT,
) -> None:
    """Import exact-quote annotations as unreviewed candidates in one transaction."""
    try:
        if str(draft_file) == "-":
            binary_stream = getattr(sys.stdin, "buffer", None)
            payload = (
                binary_stream.read(8 * 1024 * 1024 + 1)
                if binary_stream is not None
                else sys.stdin.read(8 * 1024 * 1024 + 1).encode("utf-8")
            )
        else:
            with draft_file.open("rb") as stream:
                payload = stream.read(8 * 1024 * 1024 + 1)
        drafts = parse_claim_drafts(payload)
        registry = ResearchSourceRegistry.load(DEFAULT_RESEARCH_REGISTRY)
        settings = load_settings()
        engine = create_database_engine(settings)
        try:
            session_factory = create_session_factory(engine)
            with session_scope(session_factory) as session:
                repository = ResearchRepository(session, registry)
                claims = tuple(repository.insert_claim_candidate(draft) for draft in drafts)
        finally:
            engine.dispose()
    except (DatabaseConfigurationError, SQLAlchemyError) as error:
        _emit_error(error, exit_code=1)
    except (OSError, ResearchWorkflowPayloadError, ResearchConfigurationError, ValueError) as error:
        _emit_error(error, exit_code=2)
    _json_echo(
        {
            "status": "candidate_only",
            "candidate_count": len(claims),
            "claim_ids": [str(claim.id) for claim in claims],
            "accepted_claims_created": 0,
            "next_step": "career-os research review-claim --help",
        }
    )


@research_app.command("review-claim")
def research_review_claim(
    claim_id: UUID = _RESEARCH_CLAIM_ID_ARGUMENT,
    state: Literal[
        "accepted", "rejected", "superseded", "retracted"
    ] = _RESEARCH_REVIEW_STATE_ARGUMENT,
    reviewer: str = _RESEARCH_REVIEWER_OPTION,
    reason: str = _RESEARCH_REASON_OPTION,
) -> None:
    """Append a review event; acceptance revalidates the stored quote and current version."""
    if not reviewer.strip() or not reason.strip():
        _emit_error(ValueError("reviewer and reason must be non-blank"), exit_code=2)
    try:
        registry = ResearchSourceRegistry.load(DEFAULT_RESEARCH_REGISTRY)
        settings = load_settings()
        engine = create_database_engine(settings)
        try:
            session_factory = create_session_factory(engine)
            with session_scope(session_factory) as session:
                review = ResearchRepository(session, registry).append_claim_review(
                    claim_id=claim_id,
                    state=ResearchClaimReviewState(state),
                    reviewer=reviewer,
                    reason=reason,
                )
        finally:
            engine.dispose()
    except (DatabaseConfigurationError, SQLAlchemyError) as error:
        _emit_error(error, exit_code=1)
    except (ResearchConfigurationError, ValueError) as error:
        _emit_error(error, exit_code=2)
    _json_echo(
        {
            "claim_id": str(claim_id),
            "review_id": str(review.id),
            "state": review.state,
            "reviewer": review.reviewer,
            "reason": review.reason,
        }
    )


@research_app.command("questions")
def research_questions() -> None:
    """Show the three research questions and current reviewed evidence posture."""
    try:
        settings = load_settings()
        engine = create_database_engine(settings)
        try:
            session_factory = create_session_factory(engine)
            with session_scope(session_factory) as session:
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                session.execute(text("SET TRANSACTION READ ONLY"))
                result = ResearchEvidenceService(SqlResearchEvidenceReadStore(session)).questions()
        finally:
            engine.dispose()
    except (DatabaseConfigurationError, SQLAlchemyError) as error:
        _emit_error(error, exit_code=1)
    _json_echo(result)


@research_app.command("monitor")
def research_monitor(
    bootstrap: bool = _RESEARCH_BOOTSTRAP_OPTION,
    state_path: Path = _RESEARCH_STATE_PATH_OPTION,
) -> None:
    """Collect once, refresh the review queue, and emit due research products."""
    try:
        registry = ResearchSourceRegistry.load(DEFAULT_RESEARCH_REGISTRY)
        settings = load_settings()
        engine = create_database_engine(settings)
        try:
            session_factory = create_session_factory(engine)

            def collect_daily() -> object:
                result = ResearchCollectionService(session_factory, registry).collect(
                    trigger="daily"
                )
                summary = asdict(result)
                summary["source_failures"] = _research_source_failures(
                    session_factory, result.run_id
                )
                return summary

            result = ResearchMonitor(state_path).run(
                collector=collect_daily,
                snapshot_provider=lambda: _research_snapshot(session_factory),
                bootstrap=bootstrap,
            )
        finally:
            engine.dispose()
    except (DatabaseConfigurationError, SQLAlchemyError) as error:
        _emit_error(error, exit_code=1)
    except (ResearchConfigurationError, ResearchMonitorError, ValueError) as error:
        _emit_error(error, exit_code=2)
    _json_echo(result.to_dict())


@research_app.command("pending")
def research_pending(
    state_path: Path = _RESEARCH_STATE_PATH_OPTION,
) -> None:
    """List current extracted document versions that need an explicit disposition."""
    try:
        settings = load_settings()
        engine = create_database_engine(settings)
        try:
            session_factory = create_session_factory(engine)
            pending = ResearchMonitor(state_path).pending_versions(
                snapshot_provider=lambda: _research_snapshot(session_factory)
            )
        finally:
            engine.dispose()
    except (DatabaseConfigurationError, SQLAlchemyError) as error:
        _emit_error(error, exit_code=1)
    except (ResearchMonitorError, ValueError) as error:
        _emit_error(error, exit_code=2)
    _json_echo({"count": len(pending), "items": list(pending)})


@research_app.command("review-version")
def research_review_version(
    document_id: UUID = _RESEARCH_DOCUMENT_ID_ARGUMENT,
    normalized_text_sha256: str = _RESEARCH_TEXT_DIGEST_ARGUMENT,
    disposition: Literal["reviewed", "no_claims"] = _RESEARCH_DISPOSITION_OPTION,
    reviewer: str = _RESEARCH_REVIEWER_OPTION,
    reason: str = _RESEARCH_REASON_OPTION,
    state_path: Path = _RESEARCH_STATE_PATH_OPTION,
) -> None:
    """Record that one current text digest was inspected, including an explicit no-claim result."""
    try:
        settings = load_settings()
        engine = create_database_engine(settings)
        try:
            session_factory = create_session_factory(engine)
            snapshot = _research_snapshot(session_factory)
            current = next(
                (
                    item
                    for item in cast(list[dict[str, object]], snapshot["documents"])
                    if item.get("id") == str(document_id)
                    and cast(dict[str, object], item.get("latest_version") or {}).get(
                        "normalized_text_sha256"
                    )
                    == normalized_text_sha256
                ),
                None,
            )
            if current is None:
                raise ResearchMonitorError(
                    "the document and digest do not identify its current normalized text"
                )
            record = ResearchMonitor(state_path).record_version_disposition(
                document_id=str(document_id),
                normalized_text_sha256=normalized_text_sha256,
                disposition=disposition,
                reviewer=reviewer,
                reason=reason,
            )
        finally:
            engine.dispose()
    except (DatabaseConfigurationError, SQLAlchemyError) as error:
        _emit_error(error, exit_code=1)
    except (ResearchMonitorError, ValueError) as error:
        _emit_error(error, exit_code=2)
    _json_echo({"document_id": str(document_id), "record": record})


@research_app.command("monthly-review")
def research_monthly_review(
    review_file: Path = _RESEARCH_MONTHLY_REVIEW_ARGUMENT,
    state_path: Path = _RESEARCH_STATE_PATH_OPTION,
) -> None:
    """Publish an explicit review of all three hypotheses against current accepted claims."""
    try:
        with review_file.open("rb") as stream:
            payload = stream.read(1024 * 1024 + 1)
        review = parse_monthly_review(payload)
        settings = load_settings()
        engine = create_database_engine(settings)
        try:
            session_factory = create_session_factory(engine)
            output = ResearchMonitor(state_path).publish_monthly_review(
                review,
                snapshot_provider=lambda: _research_snapshot(session_factory),
            )
        finally:
            engine.dispose()
    except (DatabaseConfigurationError, SQLAlchemyError) as error:
        _emit_error(error, exit_code=1)
    except (OSError, ResearchWorkflowPayloadError, ResearchMonitorError, ValueError) as error:
        _emit_error(error, exit_code=2)
    _json_echo({"status": "reviewed", "output": output})


@radar_app.command("build")
def radar_build(
    panel_manifest: Path = _RADAR_MANIFEST_OPTION,
    cutoff_utc: str = _RADAR_CUTOFF_OPTION,
) -> None:
    """Build or return the immutable roles-only result for a saved panel."""
    try:
        cutoff = _parse_radar_cutoff(cutoff_utc)
    except ValueError as error:
        _emit_error(error, exit_code=2)
    try:
        session_factory = _database_session_factory(load_settings())
        result = RadarBuildService(session_factory).build(str(panel_manifest), cutoff)
    except (ConfigurationError, RadarPanelVersionConflictError) as error:
        _emit_error(error, exit_code=2)
    except (
        DatabaseConfigurationError,
        RadarBuildPersistenceError,
        PersistenceError,
        SQLAlchemyError,
        OverflowError,
        ValueError,
    ) as error:
        _emit_error(error, exit_code=1)
    _json_echo(result)


@radar_app.command("show")
def radar_show(run_id: UUID = _RADAR_RUN_ID_OPTION) -> None:
    """Print a saved Radar JSON result without consulting mutable job rows."""
    try:
        session_factory = _database_session_factory(load_settings())
        result = RadarBuildService(session_factory).show(run_id)
    except (DatabaseConfigurationError, SQLAlchemyError, PersistenceError) as error:
        _emit_error(error, exit_code=1)
    if result is None:
        _emit_error(ValueError(f"Radar run was not found: {run_id}"), exit_code=4)
    _json_echo(result)


def _parse_radar_cutoff(value: str) -> datetime:
    """Parse an RFC3339 instant with explicit offset and normalize it to UTC."""
    if not _RFC3339_CUTOFF.fullmatch(value):
        raise ValueError("cutoff must be RFC3339 with an explicit UTC offset")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError) as error:
        raise ValueError("cutoff is not a valid RFC3339 timestamp") from error


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
    radar_panel: RadarPanelManifest | None = None,
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
                    radar_panel=radar_panel,
                ).collect(source_config=source_config, adapter=adapter)
            elif isinstance(source_config, LeverSourceConfig):
                result = await LeverIngestionService(
                    session_factory=session_factory,
                    logger=logger,
                    request_timeout_seconds=request_timeout_seconds,
                    operational_policy=policy,
                    radar_panel=radar_panel,
                ).collect(source_config=source_config, adapter=adapter)
            elif isinstance(source_config, AshbySourceConfig):
                result = await AshbyIngestionService(
                    session_factory=session_factory,
                    logger=logger,
                    request_timeout_seconds=request_timeout_seconds,
                    operational_policy=policy,
                    radar_panel=radar_panel,
                ).collect(source_config=source_config, adapter=adapter)
            elif isinstance(source_config, WorkablePublicSourceConfig):
                result = await WorkablePublicIngestionService(
                    session_factory=session_factory,
                    logger=logger,
                    request_timeout_seconds=request_timeout_seconds,
                    operational_policy=policy,
                    radar_panel=radar_panel,
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
