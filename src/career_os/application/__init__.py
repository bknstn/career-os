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
        GREENHOUSE_NORMALIZER_VERSION,
        NO_TAXONOMY_VERSION,
        GreenhouseIngestionService,
        IngestionError,
        IngestionResult,
        IngestionService,
        greenhouse_candidate,
    )

__all__ = [
    "GREENHOUSE_NORMALIZER_VERSION",
    "NO_TAXONOMY_VERSION",
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
    "LifecycleAction",
    "LifecycleMutationPort",
    "LifecycleResult",
    "LifecycleService",
    "greenhouse_candidate",
]


def __getattr__(name: str) -> object:
    """Load ingestion composition lazily to keep Alembic metadata importable."""
    if name in {
        "GREENHOUSE_NORMALIZER_VERSION",
        "NO_TAXONOMY_VERSION",
        "GreenhouseIngestionService",
        "IngestionError",
        "IngestionResult",
        "IngestionService",
        "greenhouse_candidate",
    }:
        from career_os.application import ingestion

        return getattr(ingestion, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
