"""Read-only current-eligibility projection for saved research products."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from fastapi import APIRouter

from career_os.api.research import SessionFactoryProvider, _read_session
from career_os.application.research_evidence import (
    ResearchEvidenceService,
    SqlResearchEvidenceReadStore,
)
from career_os.application.research_monitor import (
    DEFAULT_RESEARCH_MONITOR_STATE_PATH,
    ResearchMonitor,
)


def _complete_snapshot(provider: SessionFactoryProvider) -> dict[str, object]:
    """Return every current document and claim from one read-only transaction."""
    with _read_session(provider) as session:
        service = ResearchEvidenceService(SqlResearchEvidenceReadStore(session))
        document_response = service.documents(page=1, page_size=100)
        document_total = cast(int, document_response["total"])
        documents = list(cast(list[dict[str, object]], document_response["items"]))
        for page in range(2, (document_total + 99) // 100 + 1):
            response = service.documents(page=page, page_size=100)
            documents.extend(cast(list[dict[str, object]], response["items"]))

        claim_response = service.claims(question=None, page=1, page_size=100)
        claim_total = cast(int, claim_response["total"])
        claims = list(cast(list[dict[str, object]], claim_response["items"]))
        for page in range(2, (claim_total + 99) // 100 + 1):
            response = service.claims(question=None, page=page, page_size=100)
            claims.extend(cast(list[dict[str, object]], response["items"]))
        if len(documents) != document_total or len(claims) != claim_total:
            raise RuntimeError("research product eligibility snapshot is incomplete")
    return {"documents": documents, "claims": claims}


def create_research_products_router(
    *,
    session_factory_provider: SessionFactoryProvider,
    state_path: Path = DEFAULT_RESEARCH_MONITOR_STATE_PATH,
) -> APIRouter:
    """Create a lazy endpoint that only reads product files and the local DB."""
    router = APIRouter(prefix="/radar/api/research", tags=["career-radar-research"])
    monitor = ResearchMonitor(state_path)

    @router.get("/products", summary="Read current weekly and monthly research products")
    def products() -> dict[str, object]:
        def snapshot_provider() -> dict[str, object]:
            return _complete_snapshot(session_factory_provider)

        return monitor.latest_products(snapshot_provider=snapshot_provider)

    return router
