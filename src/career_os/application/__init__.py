"""Application workflows that coordinate domain contracts and infrastructure ports."""

from typing import TYPE_CHECKING

from career_os.application.collect import (
    CollectionError,
    CollectionFailedError,
    CollectionProtocolError,
    CollectionResult,
    CollectionRun,
    CollectionRunStatus,
    CollectionRunStore,
    CollectionService,
    CollectionStateTransitionError,
    RadarPanelIdentity,
)
from career_os.application.lifecycle import (
    LifecycleAction,
    LifecycleMutationPort,
    LifecycleResult,
    LifecycleService,
)

if TYPE_CHECKING:
    from career_os.application.enrichment import SkillEnrichmentService
    from career_os.application.ingestion import (
        ASHBY_NORMALIZER_VERSION,
        GREENHOUSE_NORMALIZER_VERSION,
        LEVER_NORMALIZER_VERSION,
        NO_TAXONOMY_VERSION,
        WORKABLE_PUBLIC_NORMALIZER_VERSION,
        AshbyIngestionService,
        GreenhouseIngestionService,
        IngestionError,
        IngestionResult,
        IngestionService,
        LeverIngestionService,
        WorkablePublicIngestionService,
        ashby_candidate,
        greenhouse_candidate,
        lever_candidate,
        workable_public_candidate,
    )
    from career_os.application.job_enrichment import (
        BulkEnrichmentResult,
        JobEnrichmentError,
        JobEnrichmentService,
    )

__all__ = [
    "ASHBY_NORMALIZER_VERSION",
    "GREENHOUSE_NORMALIZER_VERSION",
    "LEVER_NORMALIZER_VERSION",
    "NO_TAXONOMY_VERSION",
    "WORKABLE_PUBLIC_NORMALIZER_VERSION",
    "AshbyIngestionService",
    "BulkEnrichmentResult",
    "CollectionError",
    "CollectionFailedError",
    "CollectionProtocolError",
    "CollectionResult",
    "CollectionRun",
    "CollectionRunStatus",
    "CollectionRunStore",
    "CollectionService",
    "CollectionStateTransitionError",
    "GreenhouseIngestionService",
    "IngestionError",
    "IngestionResult",
    "IngestionService",
    "JobEnrichmentError",
    "JobEnrichmentService",
    "LeverIngestionService",
    "LifecycleAction",
    "LifecycleMutationPort",
    "LifecycleResult",
    "LifecycleService",
    "RadarPanelIdentity",
    "SkillEnrichmentService",
    "WorkablePublicIngestionService",
    "ashby_candidate",
    "greenhouse_candidate",
    "lever_candidate",
    "workable_public_candidate",
]


def __getattr__(name: str) -> object:
    """Load ingestion composition lazily to keep Alembic metadata importable."""
    if name == "SkillEnrichmentService":
        from career_os.application.enrichment import SkillEnrichmentService

        return SkillEnrichmentService
    if name in {"BulkEnrichmentResult", "JobEnrichmentError", "JobEnrichmentService"}:
        from career_os.application import job_enrichment

        return getattr(job_enrichment, name)
    if name in {
        "ASHBY_NORMALIZER_VERSION",
        "GREENHOUSE_NORMALIZER_VERSION",
        "LEVER_NORMALIZER_VERSION",
        "NO_TAXONOMY_VERSION",
        "WORKABLE_PUBLIC_NORMALIZER_VERSION",
        "AshbyIngestionService",
        "GreenhouseIngestionService",
        "LeverIngestionService",
        "WorkablePublicIngestionService",
        "IngestionError",
        "IngestionResult",
        "IngestionService",
        "ashby_candidate",
        "greenhouse_candidate",
        "lever_candidate",
        "workable_public_candidate",
    }:
        from career_os.application import ingestion

        return getattr(ingestion, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
