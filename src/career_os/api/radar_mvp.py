"""Read-only HTTP routes for the local Career Radar MVP."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from career_os.application.operations import CollectionOperationalPolicy
from career_os.application.radar_mvp import (
    RadarMvpFilters,
    RadarMvpQueryError,
    RadarMvpService,
    radar_mvp_methodology,
)
from career_os.config.skills import SkillTaxonomy

SessionFactoryProvider = Callable[[], sessionmaker[Session]]
TaxonomyProvider = Callable[[], SkillTaxonomy]
PolicyProvider = Callable[[], CollectionOperationalPolicy]


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


def create_radar_mvp_router(
    *,
    session_factory_provider: SessionFactoryProvider,
    taxonomy_provider: TaxonomyProvider,
    policy_provider: PolicyProvider,
) -> APIRouter:
    """Create lazy routes using the existing app dependency seams."""
    router = APIRouter(prefix="/radar/api", tags=["career-radar"])

    @router.get("/snapshot", summary="Explore the current saved job inventory")
    def snapshot(
        q: Annotated[str | None, Query(max_length=200)] = None,
        occupation: Annotated[str | None, Query(pattern=r"^[a-z][a-z0-9_]*$")] = None,
        specialty: Annotated[str | None, Query(pattern=r"^[a-z][a-z0-9_]*$")] = None,
        skill: Annotated[str | None, Query(pattern=r"^[a-z][a-z0-9_]*$")] = None,
        source: Annotated[str | None, Query(min_length=1, max_length=160)] = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 25,
        include_inactive: bool = False,
        enabled_only: bool = True,
        period_days: int = 90,
    ) -> dict[str, object]:
        try:
            taxonomy = taxonomy_provider()
        except (OSError, ValueError):
            raise HTTPException(status_code=503, detail="taxonomy unavailable") from None
        try:
            filters = RadarMvpFilters(
                q=q,
                occupation=occupation,
                specialty=specialty,
                skill=skill,
                source=source,
                page=page,
                page_size=page_size,
                include_inactive=include_inactive,
                enabled_only=enabled_only,
                period_days=period_days,
            )
            filters.validate()
            if skill is not None and skill not in {item.id for item in taxonomy.skills}:
                raise RadarMvpQueryError("unknown skill ID")
            with _read_session(session_factory_provider) as session:
                return RadarMvpService(
                    session=session,
                    taxonomy=taxonomy,
                    freshness_target_seconds=policy_provider().freshness_target_seconds,
                ).snapshot(filters, now=datetime.now(UTC))
        except RadarMvpQueryError:
            raise HTTPException(status_code=422, detail="invalid Radar filters") from None

    @router.get("/jobs/{job_id}", summary="Inspect one saved posting and exact evidence")
    def job_detail(job_id: UUID) -> dict[str, object]:
        try:
            taxonomy = taxonomy_provider()
        except (OSError, ValueError):
            raise HTTPException(status_code=503, detail="taxonomy unavailable") from None
        with _read_session(session_factory_provider) as session:
            result = RadarMvpService(
                session=session,
                taxonomy=taxonomy,
                freshness_target_seconds=policy_provider().freshness_target_seconds,
            ).detail(job_id)
        if result is None:
            raise HTTPException(status_code=404, detail="job not found")
        return result

    @router.get("/methodology", summary="Read metric definitions and limitations")
    def methodology() -> dict[str, object]:
        try:
            taxonomy = taxonomy_provider()
        except (OSError, ValueError):
            raise HTTPException(status_code=503, detail="taxonomy unavailable") from None
        return radar_mvp_methodology(taxonomy)

    return router
