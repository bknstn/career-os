"""Source-neutral operational contracts for one collection run."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RunCounts:
    """Non-negative counters shared by run state, logging, and later persistence."""

    items_fetched: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    inactivated: int = 0
    failed_items: int = 0
    http_requests: int = 0
    retry_count: int = 0
    rate_limit_events: int = 0
    normalization_errors: int = 0

    def __post_init__(self) -> None:
        for field_name, value in self.as_fields().items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")

    def as_fields(self) -> dict[str, int]:
        """Return stable scalar names for logs and collection-run persistence."""
        return {
            "items_fetched": self.items_fetched,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "inactivated": self.inactivated,
            "failed_items": self.failed_items,
            "http_requests": self.http_requests,
            "retry_count": self.retry_count,
            "rate_limit_events": self.rate_limit_events,
            "normalization_errors": self.normalization_errors,
        }
