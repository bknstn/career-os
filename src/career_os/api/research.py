"""Read-only routes for the source-grounded research evidence stream."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from career_os.application.research_evidence import (
    ResearchEvidenceService,
    SqlResearchEvidenceReadStore,
)
from career_os.domain.research import ResearchQuestion

SessionFactoryProvider = Callable[[], sessionmaker[Session]]


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


def create_research_router(*, session_factory_provider: SessionFactoryProvider) -> APIRouter:
    """Create lazy read routes; no connection is opened until a request arrives."""
    router = APIRouter(prefix="/radar/api/research", tags=["career-radar-research"])

    @router.get("/overview", summary="Review the stored research corpus at a glance")
    def overview() -> dict[str, object]:
        with _read_session(session_factory_provider) as session:
            return ResearchEvidenceService(SqlResearchEvidenceReadStore(session)).overview()

    @router.get("/questions", summary="Inspect the three research questions and evidence posture")
    def questions() -> dict[str, object]:
        with _read_session(session_factory_provider) as session:
            return ResearchEvidenceService(SqlResearchEvidenceReadStore(session)).questions()

    @router.get("/documents", summary="List canonical research publications")
    def documents(
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> dict[str, object]:
        with _read_session(session_factory_provider) as session:
            return ResearchEvidenceService(SqlResearchEvidenceReadStore(session)).documents(
                page=page,
                page_size=page_size,
            )

    @router.get(
        "/documents/{document_id}/versions/{version_id}",
        summary="Inspect extracted text, citations, claims, and retrieval provenance",
    )
    def version_evidence(document_id: UUID, version_id: UUID) -> dict[str, object]:
        with _read_session(session_factory_provider) as session:
            result = ResearchEvidenceService(
                SqlResearchEvidenceReadStore(session)
            ).version_evidence(
                document_id,
                version_id,
            )
        if result is None:
            raise HTTPException(status_code=404, detail="research version not found")
        return result

    @router.get("/claims", summary="Review candidate and current research claims")
    def claims(
        question: ResearchQuestion | None = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> dict[str, object]:
        with _read_session(session_factory_provider) as session:
            return ResearchEvidenceService(SqlResearchEvidenceReadStore(session)).claims(
                question=question,
                page=page,
                page_size=page_size,
            )

    return router
