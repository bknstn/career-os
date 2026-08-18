"""Fail-closed lifecycle reconciliation after a source collection result."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from career_os.application.collect import CollectionFailedError, CollectionResult, CollectionRun


class LifecycleAction(StrEnum):
    """Whether a collection result was safe to reconcile."""

    INACTIVATED = "inactivated"
    SKIPPED_UNSAFE = "skipped_unsafe"


class LifecycleMutationPort(Protocol):
    """Persistence boundary for source-scoped missing-job reconciliation."""

    def inactivate_unseen(self, *, run: CollectionRun, seen_external_ids: tuple[str, ...]) -> int:
        """Atomically inactivate eligible unseen jobs and return the actual count."""


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    """Typed outcome that makes unsafe collection results explicit no-ops."""

    action: LifecycleAction
    source_key: str
    seen_external_ids: tuple[str, ...]
    inactivated_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.action, LifecycleAction):
            raise TypeError("action must be a LifecycleAction")
        if not isinstance(self.source_key, str) or not self.source_key:
            raise ValueError("source_key must be non-blank")
        if any(
            not isinstance(external_id, str) or not external_id
            for external_id in self.seen_external_ids
        ):
            raise ValueError("seen_external_ids must contain non-blank strings")
        if self.seen_external_ids != tuple(sorted(set(self.seen_external_ids))):
            raise ValueError("seen_external_ids must be sorted and deduplicated")
        if (
            isinstance(self.inactivated_count, bool)
            or not isinstance(self.inactivated_count, int)
            or self.inactivated_count < 0
        ):
            raise ValueError("inactivated_count must be a non-negative integer")
        if self.action is LifecycleAction.SKIPPED_UNSAFE and self.inactivated_count != 0:
            raise ValueError("an unsafe lifecycle result cannot inactivate jobs")


type LifecycleInput = CollectionResult | CollectionFailedError


@dataclass(slots=True)
class LifecycleService:
    """Apply absence inference only after a persisted complete source scan."""

    mutation_port: LifecycleMutationPort

    def reconcile(self, collection: LifecycleInput) -> LifecycleResult:
        """Inactivate source-local unseen jobs, or return a typed safe no-op."""
        result = collection.result if isinstance(collection, CollectionFailedError) else collection
        seen_external_ids = _stable_seen_ids(posting.external_id for posting in result.postings)
        if not result.is_complete_success:
            return LifecycleResult(
                action=LifecycleAction.SKIPPED_UNSAFE,
                source_key=result.run.source_key,
                seen_external_ids=seen_external_ids,
                inactivated_count=0,
            )
        return LifecycleResult(
            action=LifecycleAction.INACTIVATED,
            source_key=result.run.source_key,
            seen_external_ids=seen_external_ids,
            inactivated_count=self.mutation_port.inactivate_unseen(
                run=result.run,
                seen_external_ids=seen_external_ids,
            ),
        )


def _stable_seen_ids(external_ids: Iterable[str]) -> tuple[str, ...]:
    """Deduplicate source-native identities independently of adapter event order."""
    return tuple(sorted(set(external_ids)))
