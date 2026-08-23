"""Operational policy and source-health contracts for collection hardening."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CollectionOperationalPolicy:
    """Small, explicit local-MVP safeguards around source collection."""

    max_fetch_drop_ratio: float = 0.5
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: int = 900
    freshness_target_seconds: int = 86_400

    def __post_init__(self) -> None:
        if isinstance(self.max_fetch_drop_ratio, bool) or not isinstance(
            self.max_fetch_drop_ratio, (int, float)
        ):
            raise TypeError("max_fetch_drop_ratio must be numeric")
        if not 0 <= self.max_fetch_drop_ratio < 1:
            raise ValueError("max_fetch_drop_ratio must be between zero (inclusive) and one")
        for field_name in (
            "circuit_failure_threshold",
            "circuit_cooldown_seconds",
            "freshness_target_seconds",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field_name} must be an integer")
            if value < 1:
                raise ValueError(f"{field_name} must be positive")


class ScanOutcomeSafeguard(Protocol):
    """Turn suspicious complete scans into partial scans before lifecycle work."""

    def incomplete_reason(self, *, source_key: str, items_fetched: int) -> str | None:
        """Return a durable reason when absence inference is unsafe."""


@dataclass(frozen=True, slots=True)
class SourceOperationalStatus:
    """Low-cardinality, machine-readable health and metrics for one source."""

    source_key: str
    source_kind: str
    enabled: bool
    active_jobs: int
    latest_status: str | None
    latest_finished_at: datetime | None
    last_success_at: datetime | None
    last_success_age_seconds: float | None
    stale: bool
    consecutive_unsafe_runs: int
    circuit_open: bool
    latest_items_fetched: int
    latest_http_requests: int
    latest_retry_count: int
    latest_rate_limit_events: int
    latest_failed_items: int
    latest_normalization_errors: int

    def __post_init__(self) -> None:
        for field_name in ("latest_finished_at", "last_success_at"):
            value = getattr(self, field_name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{field_name} must be timezone-aware")


class SourceCollectionUnavailableError(RuntimeError):
    """Collection cannot safely start because its source lock or circuit is open."""


def utcnow() -> datetime:
    return datetime.now(UTC)
