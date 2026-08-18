"""PostgreSQL persistence metadata for Career OS."""

from career_os.persistence.models import Base
from career_os.persistence.repositories import (
    CanonicalJobRepository,
    CanonicalVersionConflictError,
    CollectionRunPersistenceConflictError,
    JobUpsertAction,
    JobUpsertResult,
    LifecycleReconciliationConflictError,
    PersistenceError,
    SourceIdentityConflictError,
    SourceLifecycleRepository,
    SourceNotFoundError,
    SqlAlchemyCollectionRunStore,
    StaleObservationError,
)

__all__ = [
    "Base",
    "CanonicalJobRepository",
    "CanonicalVersionConflictError",
    "CollectionRunPersistenceConflictError",
    "JobUpsertAction",
    "JobUpsertResult",
    "LifecycleReconciliationConflictError",
    "PersistenceError",
    "SourceIdentityConflictError",
    "SourceLifecycleRepository",
    "SourceNotFoundError",
    "SqlAlchemyCollectionRunStore",
    "StaleObservationError",
]
