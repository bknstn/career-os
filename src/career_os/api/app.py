"""FastAPI construction and read-only routes with lazy infrastructure."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from career_os.api.cursors import InvalidCursorError, decode_job_cursor, encode_job_cursor
from career_os.api.radar_mvp import create_radar_mvp_router
from career_os.api.research import create_research_router
from career_os.api.research_products import create_research_products_router
from career_os.api.schemas import (
    ErrorResponse,
    JobCompensationResponse,
    JobDetailResponse,
    JobFilters,
    JobLocationResponse,
    JobPageResponse,
    JobSkillEvidenceResponse,
    JobSummaryResponse,
    JobVersionResponse,
    LiveResponse,
    MarketReportRequest,
    MarketReportResponse,
    ReadyResponse,
    RunCountsResponse,
    RunResponse,
    SourceResponse,
    TargetUniverseReportRequest,
    TargetUniverseReportResponse,
)
from career_os.application.market import MarketDataConsistencyError
from career_os.application.operations import (
    CollectionOperationalPolicy,
    SourceOperationalStatus,
)
from career_os.application.report import MarketReportService
from career_os.config.profiles import validate_profile_taxonomy
from career_os.config.skills import SkillTaxonomy, load_skill_taxonomy
from career_os.config.sources import ConfigurationError
from career_os.db import (
    DatabaseConfigurationError,
    create_database_engine,
    create_session_factory,
)
from career_os.domain.hybrid_results import HybridResult, QualityReviewInput
from career_os.domain.normalization import EmploymentType, RoleFamily, Seniority
from career_os.domain.target_universe_results import TargetUniverseResult
from career_os.normalization.jobs import JOB_NORMALIZER_VERSION
from career_os.persistence.api_queries import (
    ApiReadRepository,
    JobCursorKey,
    JobDetail,
    JobQueryFilters,
)
from career_os.persistence.operations import OperationalStatusRepository
from career_os.persistence.report_queries import ReportEvidenceConsistencyError
from career_os.persistence.repositories import JobInspection, RunInspection
from career_os.settings import load_settings

SessionFactory = sessionmaker[Session]
SessionFactoryProvider = Callable[[], SessionFactory]
TaxonomyProvider = Callable[[], SkillTaxonomy]
OperationalPolicyProvider = Callable[[], CollectionOperationalPolicy]
_DEFAULT_TAXONOMY_PATH = Path(__file__).resolve().parents[3] / "config/taxonomy/skills.v1.yaml"
_RADAR_WEB_PATH = Path(__file__).resolve().parents[1] / "radar_web"


@dataclass(frozen=True, slots=True)
class ApiDependencies:
    """Injectable lazy seams; constructing the app performs no database or file I/O."""

    session_factory_provider: SessionFactoryProvider
    taxonomy_provider: TaxonomyProvider
    operational_policy_provider: OperationalPolicyProvider | None = None


@lru_cache(maxsize=1)
def _default_session_factory_provider() -> SessionFactory:
    return create_session_factory(create_database_engine(load_settings()))


@lru_cache(maxsize=1)
def _default_taxonomy_provider() -> SkillTaxonomy:
    return load_skill_taxonomy(_DEFAULT_TAXONOMY_PATH)


def _default_operational_policy_provider() -> CollectionOperationalPolicy:
    settings = load_settings()
    return CollectionOperationalPolicy(
        max_fetch_drop_ratio=settings.max_fetch_drop_ratio,
        circuit_failure_threshold=settings.circuit_failure_threshold,
        circuit_cooldown_seconds=settings.circuit_cooldown_seconds,
        freshness_target_seconds=settings.freshness_target_seconds,
    )


@contextmanager
def _read_session(provider: SessionFactoryProvider) -> Iterator[Session]:
    session = provider()()
    try:
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        session.execute(text("SET TRANSACTION READ ONLY"))
        yield session
    finally:
        session.rollback()
        session.close()


def create_app(dependencies: ApiDependencies | None = None) -> FastAPI:
    """Build the local read API without connecting to PostgreSQL or the network."""
    deps = dependencies or ApiDependencies(
        session_factory_provider=_default_session_factory_provider,
        taxonomy_provider=_default_taxonomy_provider,
        operational_policy_provider=_default_operational_policy_provider,
    )
    policy_provider = deps.operational_policy_provider or _default_operational_policy_provider
    app = FastAPI(
        title="Career OS read API",
        version="0.1.0",
        description="Localhost-only, read-only Market Explorer API.",
    )

    @app.exception_handler(SQLAlchemyError)
    async def database_failure(_request: Request, _error: SQLAlchemyError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})

    @app.exception_handler(DatabaseConfigurationError)
    async def database_configuration_failure(
        _request: Request, _error: DatabaseConfigurationError
    ) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})

    @app.exception_handler(ConfigurationError)
    async def process_configuration_failure(
        _request: Request, _error: ConfigurationError
    ) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})

    @app.exception_handler(RequestValidationError)
    async def request_validation_failure(
        _request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "request validation failed"})

    @app.get("/health/live", response_model=LiveResponse, tags=["health"])
    def live() -> LiveResponse:
        return LiveResponse()

    @app.get(
        "/metrics",
        response_class=PlainTextResponse,
        responses={503: {"model": ErrorResponse}},
        tags=["operations"],
    )
    def metrics() -> PlainTextResponse:
        """Expose low-cardinality Prometheus text metrics from persisted source state."""
        with _read_session(deps.session_factory_provider) as session:
            statuses = OperationalStatusRepository(
                session=session,
                policy=policy_provider(),
            ).list_source_statuses(now=datetime.now(UTC))
        return PlainTextResponse(
            _prometheus_metrics(statuses),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get(
        "/health/ready",
        response_model=ReadyResponse,
        responses={503: {"model": ErrorResponse}},
        tags=["health"],
    )
    def ready() -> ReadyResponse:
        try:
            with _read_session(deps.session_factory_provider) as session:
                session.execute(text("SELECT 1"))
        except (ConfigurationError, DatabaseConfigurationError, SQLAlchemyError):
            raise HTTPException(status_code=503, detail="database unavailable") from None
        return ReadyResponse()

    @app.get(
        "/v1/sources",
        response_model=tuple[SourceResponse, ...],
        responses={503: {"model": ErrorResponse}},
        tags=["sources"],
    )
    def sources() -> tuple[SourceResponse, ...]:
        with _read_session(deps.session_factory_provider) as session:
            return tuple(
                SourceResponse.model_validate(
                    {
                        "id": item.id,
                        "key": item.key,
                        "kind": item.kind,
                        "company_name": item.company_name,
                        "enabled": item.enabled,
                        "created_at": item.created_at,
                        "updated_at": item.updated_at,
                    }
                )
                for item in ApiReadRepository(session).list_sources()
            )

    @app.get(
        "/v1/runs",
        response_model=tuple[RunResponse, ...],
        responses={503: {"model": ErrorResponse}},
        tags=["runs"],
    )
    def runs() -> tuple[RunResponse, ...]:
        with _read_session(deps.session_factory_provider) as session:
            return tuple(_run_response(item) for item in ApiReadRepository(session).list_runs())

    @app.get(
        "/v1/runs/{run_id}",
        response_model=RunResponse,
        responses={
            404: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
        tags=["runs"],
    )
    def run_detail(run_id: UUID) -> RunResponse:
        with _read_session(deps.session_factory_provider) as session:
            item = ApiReadRepository(session).get_run(run_id)
            if item is None:
                raise HTTPException(status_code=404, detail="run not found")
            return _run_response(item)

    @app.get(
        "/v1/jobs",
        response_model=JobPageResponse,
        responses={
            400: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
        tags=["jobs"],
    )
    def jobs(
        status: Literal["active", "inactive"] | None = None,
        company: str | None = None,
        role_family: RoleFamily | None = None,
        seniority: Seniority | None = None,
        country_code: Annotated[str | None, Query(pattern=r"^[A-Z]{2}$")] = None,
        skill: Annotated[str | None, Query(pattern=r"^[a-z][a-z0-9_]*$")] = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> JobPageResponse:
        try:
            filters = JobFilters.model_validate(
                {
                    "status": status,
                    "company": company,
                    "role_family": role_family,
                    "seniority": seniority,
                    "country_code": country_code,
                    "skill": skill,
                }
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail="invalid job filters") from error
        try:
            after = None if cursor is None else decode_job_cursor(cursor, filters=filters)
        except InvalidCursorError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        with _read_session(deps.session_factory_provider) as session:
            items = ApiReadRepository(session).list_jobs(
                filters=JobQueryFilters(
                    status=filters.status,
                    company=filters.company,
                    role_family=filters.role_family,
                    seniority=filters.seniority,
                    country_code=filters.country_code,
                    skill=filters.skill,
                ),
                after=after,
                limit=limit + 1,
            )
            page = items[:limit]
            next_cursor = None
            if len(items) > limit:
                last = page[-1].job
                next_cursor = encode_job_cursor(
                    JobCursorKey(last_seen_at=last.last_seen_at, job_id=last.id), filters=filters
                )
            return JobPageResponse(
                items=tuple(_job_summary(item) for item in page), next_cursor=next_cursor
            )

    @app.get(
        "/v1/jobs/{job_id}",
        response_model=JobDetailResponse,
        responses={
            404: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
        tags=["jobs"],
    )
    def job_detail(job_id: UUID) -> JobDetailResponse:
        with _read_session(deps.session_factory_provider) as session:
            item = ApiReadRepository(session).get_job(job_id)
            if item is None:
                raise HTTPException(status_code=404, detail="job not found")
            return _job_detail(item)

    @app.post(
        "/v1/market-reports",
        response_model=MarketReportResponse,
        responses={
            409: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
        tags=["markets"],
    )
    def market_report(request: MarketReportRequest) -> MarketReportResponse:
        try:
            taxonomy = deps.taxonomy_provider()
        except (ConfigurationError, OSError, ValueError):
            raise HTTPException(status_code=503, detail="taxonomy unavailable") from None
        try:
            validate_profile_taxonomy(request.profile, taxonomy=taxonomy)
        except ConfigurationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        try:
            with _read_session(deps.session_factory_provider) as session:
                report = MarketReportService(
                    session=session,
                    taxonomy=taxonomy,
                    normalizer_version=JOB_NORMALIZER_VERSION,
                ).calculate(
                    filters=request.market,
                    profile=request.profile,
                    quality_review=(
                        QualityReviewInput(
                            ref=request.quality_review.ref,
                            reviewed_job_ids=request.quality_review.reviewed_job_ids,
                            false_positive_job_ids=request.quality_review.false_positive_job_ids,
                        )
                        if request.quality_review is not None
                        else None
                    ),
                )
                return _report_response(report)
        except (
            MarketDataConsistencyError,
            ReportEvidenceConsistencyError,
            ValueError,
        ) as error:
            if isinstance(error, ValueError):
                raise HTTPException(status_code=422, detail=str(error)) from error
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post(
        "/v1/target-universe-reports",
        response_model=TargetUniverseReportResponse,
        responses={
            409: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
        tags=["target-universes"],
    )
    def target_universe_report(
        request: TargetUniverseReportRequest,
    ) -> TargetUniverseReportResponse:
        try:
            taxonomy = deps.taxonomy_provider()
        except (ConfigurationError, OSError, ValueError):
            raise HTTPException(status_code=503, detail="taxonomy unavailable") from None
        try:
            validate_profile_taxonomy(request.profile, taxonomy=taxonomy)
        except ConfigurationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        try:
            with _read_session(deps.session_factory_provider) as session:
                report = MarketReportService(
                    session=session,
                    taxonomy=taxonomy,
                    normalizer_version=JOB_NORMALIZER_VERSION,
                ).calculate_target_universe(
                    target_universe=request.target_universe,
                    profile=request.profile,
                    quality_review=(
                        QualityReviewInput(
                            ref=request.quality_review.ref,
                            reviewed_job_ids=request.quality_review.reviewed_job_ids,
                            false_positive_job_ids=request.quality_review.false_positive_job_ids,
                        )
                        if request.quality_review is not None
                        else None
                    ),
                )
                return _target_universe_report_response(report)
        except (
            MarketDataConsistencyError,
            ReportEvidenceConsistencyError,
            ValueError,
        ) as error:
            if isinstance(error, ValueError):
                raise HTTPException(status_code=422, detail=str(error)) from error
            raise HTTPException(status_code=409, detail=str(error)) from error

    app.include_router(
        create_radar_mvp_router(
            session_factory_provider=deps.session_factory_provider,
            taxonomy_provider=deps.taxonomy_provider,
            policy_provider=policy_provider,
        )
    )
    app.include_router(
        create_research_router(session_factory_provider=deps.session_factory_provider)
    )
    app.include_router(
        create_research_products_router(session_factory_provider=deps.session_factory_provider)
    )
    app.mount(
        "/radar",
        StaticFiles(directory=_RADAR_WEB_PATH, html=True, check_dir=False),
        name="career-radar-ui",
    )

    return app


def _run_response(item: RunInspection) -> RunResponse:
    run = item.run
    return RunResponse.model_validate(
        {
            "id": run.id,
            "source_id": run.source_id,
            "source_key": item.source_key,
            "source_kind": item.source_kind,
            "status": run.status,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "counts": {name: getattr(run, name) for name in RunCountsResponse.model_fields},
            "normalizer_version": run.normalizer_version,
            "taxonomy_version": run.taxonomy_version,
            "error_summary": run.error_summary,
            "incomplete_reason": run.incomplete_reason,
        }
    )


def _job_summary(item: JobInspection, *, source_id: UUID | None = None) -> JobSummaryResponse:
    job = item.job
    return JobSummaryResponse.model_validate(
        {
            "id": job.id,
            "source_id": job.source_id if source_id is None else source_id,
            "source_key": item.source_key,
            "source_kind": item.source_kind,
            "external_id": job.external_id,
            "company_name": job.company_name,
            "title": job.title_raw,
            "role_family": job.role_family,
            "seniority": job.seniority,
            "workplace_type": job.workplace_type,
            "status": job.status,
            "source_url": job.source_url,
            "apply_url": job.apply_url,
            "published_at": job.published_at,
            "first_seen_at": job.first_seen_at,
            "last_seen_at": job.last_seen_at,
            "inactive_at": job.inactive_at,
        }
    )


def _job_detail(item: JobDetail) -> JobDetailResponse:
    job = item.inspection.job
    summary = _job_summary(item.inspection, source_id=item.source_id).model_dump()
    return JobDetailResponse(
        **summary,
        title_normalized=job.title_normalized,
        seniority_confidence=job.seniority_confidence,
        description_html=job.description_html,
        description_text=job.description_text,
        department_raw=job.department_raw,
        team_raw=job.team_raw,
        employment_type=cast(EmploymentType | None, job.employment_type),
        remote_scope=job.remote_scope,
        content_hash=job.content_hash,
        normalizer_version=job.normalizer_version,
        enrichment_normalizer_version=job.enrichment_normalizer_version,
        taxonomy_version=job.taxonomy_version,
        created_at=job.created_at,
        updated_at=job.updated_at,
        locations=tuple(
            JobLocationResponse.model_validate(row, from_attributes=True) for row in item.locations
        ),
        skill_evidence=tuple(
            JobSkillEvidenceResponse.model_validate(row, from_attributes=True)
            for row in item.skills
        ),
        compensation=tuple(
            JobCompensationResponse.model_validate(row, from_attributes=True)
            for row in item.compensation
        ),
        versions=tuple(
            JobVersionResponse.model_validate(row, from_attributes=True) for row in item.versions
        ),
    )


def _report_response(report: HybridResult) -> MarketReportResponse:
    """Serialize the canonical H1 result without recomputing any evidence."""
    return MarketReportResponse.model_validate(report.to_payload())


def _target_universe_report_response(
    report: TargetUniverseResult,
) -> TargetUniverseReportResponse:
    """Serialize the canonical H2 result without recalculating any evidence."""
    return TargetUniverseReportResponse.model_validate(report.to_payload())


def _prometheus_metrics(statuses: tuple[SourceOperationalStatus, ...]) -> str:
    """Render a deterministic Prometheus exposition without high-cardinality labels."""
    fields = (
        ("career_os_source_active_jobs", "active_jobs"),
        ("career_os_source_stale", "stale"),
        ("career_os_source_circuit_open", "circuit_open"),
        ("career_os_source_consecutive_unsafe_runs", "consecutive_unsafe_runs"),
        ("career_os_collection_latest_items_fetched", "latest_items_fetched"),
        ("career_os_http_latest_requests", "latest_http_requests"),
        ("career_os_http_latest_retries", "latest_retry_count"),
        ("career_os_http_latest_rate_limit_events", "latest_rate_limit_events"),
        ("career_os_collection_latest_failed_items", "latest_failed_items"),
        ("career_os_collection_latest_normalization_errors", "latest_normalization_errors"),
    )
    lines: list[str] = []
    for metric_name, field_name in fields:
        lines.append(f"# TYPE {metric_name} gauge")
        for status in statuses:
            labels = (
                f'source_key="{_prometheus_label(status.source_key)}",'
                f'source_kind="{_prometheus_label(status.source_kind)}"'
            )
            value = getattr(status, field_name)
            lines.append(f"{metric_name}{{{labels}}} {int(value)}")
    lines.append("# TYPE career_os_source_last_success_age_seconds gauge")
    for status in statuses:
        if status.last_success_age_seconds is None:
            continue
        labels = (
            f'source_key="{_prometheus_label(status.source_key)}",'
            f'source_kind="{_prometheus_label(status.source_kind)}"'
        )
        lines.append(
            "career_os_source_last_success_age_seconds"
            f"{{{labels}}} {status.last_success_age_seconds:.3f}"
        )
    return "\n".join(lines) + "\n"


def _prometheus_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


app = create_app()
