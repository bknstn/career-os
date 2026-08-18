"""Source-neutral representations of raw jobs before normalization."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any


def _require_utc_aware(value: datetime, *, field_name: str) -> datetime:
    """Return a UTC timestamp or reject a datetime without a usable offset."""
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _require_nonempty_text(value: str, *, field_name: str) -> str:
    """Normalize stable identity fields without permitting empty values."""
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized


@dataclass(frozen=True, slots=True)
class RawJobPosting:
    """One source-native job record and the point at which it was observed.

    ``payload`` deliberately preserves unrecognized source fields.  A shallow,
    read-only copy prevents changes to the caller's top-level mapping from
    changing the recorded source evidence.  Adapters should provide only
    JSON-compatible payload values; deep normalization belongs downstream.
    """

    source_key: str
    source_kind: str
    external_id: str
    source_url: str | None
    apply_url: str | None
    payload: Mapping[str, Any]
    fetched_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_key",
            _require_nonempty_text(self.source_key, field_name="source_key"),
        )
        object.__setattr__(
            self,
            "source_kind",
            _require_nonempty_text(self.source_kind, field_name="source_kind"),
        )
        object.__setattr__(
            self,
            "external_id",
            _require_nonempty_text(self.external_id, field_name="external_id"),
        )
        object.__setattr__(
            self,
            "fetched_at",
            _require_utc_aware(self.fetched_at, field_name="fetched_at"),
        )
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
