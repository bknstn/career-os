"""Contracts between source adapters and collection workflows."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Protocol, runtime_checkable

from career_os.domain.jobs import RawJobPosting, _require_nonempty_text, _require_utc_aware
from career_os.domain.runs import ScanDiagnostics


@dataclass(frozen=True, slots=True)
class FetchContext:
    """Immutable per-scan inputs supplied by the collection workflow."""

    run_id: str
    fetched_at: datetime
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _require_nonempty_text(self.run_id, field_name="run_id"))
        object.__setattr__(
            self,
            "fetched_at",
            _require_utc_aware(self.fetched_at, field_name="fetched_at"),
        )
        if not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")


class ScanStatus(StrEnum):
    """How completely an adapter observed its configured source."""

    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class ScanOutcome:
    """The required terminal event for a successful adapter stream.

    Collection code may inactivate postings absent from a scan only when
    :attr:`permits_inactivation` is true.  A missing terminal outcome, an
    exception, or a partial outcome must therefore be recorded as a failure or
    partial run and must never imply absence from the source.
    """

    status: ScanStatus
    incomplete_reason: str | None = None
    diagnostics: ScanDiagnostics = field(default_factory=ScanDiagnostics)

    def __post_init__(self) -> None:
        if not isinstance(self.diagnostics, ScanDiagnostics):
            raise TypeError("diagnostics must be ScanDiagnostics")
        if self.status is ScanStatus.COMPLETE:
            if self.incomplete_reason is not None:
                raise ValueError("a complete scan cannot have an incomplete_reason")
            return

        if self.status is ScanStatus.PARTIAL:
            if self.incomplete_reason is None or not self.incomplete_reason.strip():
                raise ValueError("a partial scan requires a non-blank incomplete_reason")
            object.__setattr__(self, "incomplete_reason", self.incomplete_reason.strip())
            return

        raise ValueError(f"unsupported scan status: {self.status!r}")

    @property
    def permits_inactivation(self) -> bool:
        """Whether downstream lifecycle logic may infer job absence."""
        return self.status is ScanStatus.COMPLETE


type FetchEvent = RawJobPosting | ScanOutcome


class ScanFailedError(RuntimeError):
    """A failed adapter scan with telemetry observed before the failure."""

    def __init__(self, message: str, *, diagnostics: ScanDiagnostics | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or ScanDiagnostics()

    def with_diagnostics(self, diagnostics: ScanDiagnostics) -> ScanFailedError:
        """Return the same failure class annotated with final observed telemetry."""
        return type(self)(str(self), diagnostics=diagnostics)


@runtime_checkable
class SourceAdapter(Protocol):
    """A source-native client that streams raw jobs and a terminal outcome.

    ``fetch`` must yield zero or more :class:`RawJobPosting` instances followed
    by exactly one final :class:`ScanOutcome`.  It must not yield records after
    that outcome.  If it raises or ends without the terminal outcome, callers
    must treat the scan as failed and must not inactivate unseen postings.
    """

    kind: str

    async def validate(self) -> None:
        """Validate adapter-specific inputs before attempting collection."""

    def fetch(self, context: FetchContext) -> AsyncIterator[FetchEvent]:
        """Stream source evidence and the scan's terminal completeness state."""
