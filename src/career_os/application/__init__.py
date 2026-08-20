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
)
from career_os.application.lifecycle import (
    LifecycleAction,
    LifecycleMutationPort,
    LifecycleResult,
    LifecycleService,
)

if TYPE_CHECKING:
    from career_os.application.ingestion import (
        ASHBY_NORMALIZER_VERSION,
        GREENHOUSE_NORMALIZER_VERSION,
        LEVER_NORMALIZER_VERSION,
        NO_TAXONOMY_VERSION,
        AshbyIngestionService,
        GreenhouseIngestionService,
        IngestionError,
        IngestionResult,
        IngestionService,
        LeverIngestionService,
        ashby_candidate,
        greenhouse_candidate,
        lever_candidate,
    )

__all__ = [
    "ASHBY_NORMALIZER_VERSION",
    "GREENHOUSE_NORMALIZER_VERSION",
    "LEVER_NORMALIZER_VERSION",
    "NO_TAXONOMY_VERSION",
    "AshbyIngestionService",
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
    "LeverIngestionService",
    "LifecycleAction",
    "LifecycleMutationPort",
    "LifecycleResult",
    "LifecycleService",
    "ashby_candidate",
    "greenhouse_candidate",
    "lever_candidate",
]


def __getattr__(name: str) -> object:
    """Load ingestion composition lazily to keep Alembic metadata importable."""
    if name in {
        "ASHBY_NORMALIZER_VERSION",
        "GREENHOUSE_NORMALIZER_VERSION",
        "LEVER_NORMALIZER_VERSION",
        "NO_TAXONOMY_VERSION",
        "AshbyIngestionService",
        "GreenhouseIngestionService",
        "LeverIngestionService",
        "IngestionError",
        "IngestionResult",
        "IngestionService",
        "ashby_candidate",
        "greenhouse_candidate",
        "lever_candidate",
    }:
        from career_os.application import ingestion

        return getattr(ingestion, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
