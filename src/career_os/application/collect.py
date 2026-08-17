"""Source collection orchestration without persistence or normalization concerns.

``collect`` returns a result for successful and partial scans.  Failed scans
raise :class:`CollectionFailedError`; its ``result`` preserves the collected
raw evidence and terminal run state for callers that need diagnostics.
"""

from __future__ import annotations

import logging as stdlib_logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import NoReturn, Protocol

from career_os.domain import (
    FetchContext,
    FetchEvent,
    RawJobPosting,
    RunCounts,
    ScanOutcome,
    ScanStatus,
    SourceAdapter,
)
from career_os.logging import SourceRunLogger, bind_source_run, safe_exception

type UtcClock = Callable[[], datetime]
type RunIdFactory = Callable[[], str]


def utcnow() -> datetime:
    """Return a timezone-aware timestamp for an application run boundary."""
    return datetime.now(UTC)


def _as_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _require_nonblank(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized


class CollectionRunStatus(StrEnum):
    """The only supported lifecycle states for one source collection run."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CollectionRun:
    """A source-neutral run-state snapshot persisted through an injected port."""

    id: str
    source_key: str
    source_kind: str
    status: CollectionRunStatus
    started_at: datetime
    finished_at: datetime | None = None
    counts: RunCounts = field(default_factory=RunCounts)
    error_summary: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, CollectionRunStatus):
            raise ValueError("status must be a CollectionRunStatus")
        if not isinstance(self.counts, RunCounts):
            raise TypeError("counts must be RunCounts")
        object.__setattr__(self, "id", _require_nonblank(self.id, field_name="id"))
        object.__setattr__(
            self,
            "source_key",
            _require_nonblank(self.source_key, field_name="source_key"),
        )
        object.__setattr__(
            self,
            "source_kind",
            _require_nonblank(self.source_kind, field_name="source_kind"),
        )
        object.__setattr__(
            self,
            "started_at",
            _as_utc(self.started_at, field_name="started_at"),
        )
        if self.status is CollectionRunStatus.RUNNING:
            if self.finished_at is not None or self.error_summary is not None:
                raise ValueError("a running collection run cannot be finished or have an error")
            return

        if self.finished_at is None:
            raise ValueError("a terminal collection run requires finished_at")
        finished_at = _as_utc(self.finished_at, field_name="finished_at")
        if finished_at < self.started_at:
            raise ValueError("finished_at must not be earlier than started_at")
        object.__setattr__(self, "finished_at", finished_at)
        if self.status is CollectionRunStatus.FAILED:
            if self.error_summary is None or not self.error_summary.strip():
                raise ValueError("a failed collection run requires error_summary")
            object.__setattr__(self, "error_summary", self.error_summary.strip())
        elif self.error_summary is not None:
            raise ValueError("only a failed collection run may have error_summary")

    @classmethod
    def start(
        cls,
        *,
        run_id: str,
        source_key: str,
        source_kind: str,
        started_at: datetime,
    ) -> CollectionRun:
        """Create the sole valid initial state for a collection attempt."""
        return cls(
            id=run_id,
            source_key=source_key,
            source_kind=source_kind,
            status=CollectionRunStatus.RUNNING,
            started_at=started_at,
        )

    def finish(
        self,
        *,
        status: CollectionRunStatus,
        finished_at: datetime,
        counts: RunCounts,
        error_summary: str | None = None,
    ) -> CollectionRun:
        """Transition a running run exactly once to a terminal status."""
        if self.status is not CollectionRunStatus.RUNNING:
            raise CollectionStateTransitionError(
                f"cannot transition collection run from {self.status}"
            )
        if status not in {
            CollectionRunStatus.SUCCEEDED,
            CollectionRunStatus.PARTIAL,
            CollectionRunStatus.FAILED,
        }:
            raise CollectionStateTransitionError(f"cannot transition collection run to {status}")
        return replace(
            self,
            status=status,
            finished_at=finished_at,
            counts=counts,
            error_summary=error_summary,
        )


class CollectionRunStore(Protocol):
    """Persistence port for run-state snapshots; implementations own I/O/transactions."""

    def create(self, run: CollectionRun) -> None:
        """Persist the initial running record before adapter work begins."""

    def update(self, run: CollectionRun) -> None:
        """Atomically persist one terminal transition or leave stored state unchanged.

        If this method raises, callers must be able to assume it did not change
        the stored state.  This permits collection recovery to attempt a failed
        transition without silently overwriting an unknown terminal state.
        """


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """Raw collection output awaiting a future normalization/upsert boundary."""

    run: CollectionRun
    postings: tuple[RawJobPosting, ...]
    outcome: ScanOutcome | None
    terminal_state_persisted: bool

    def __post_init__(self) -> None:
        if not isinstance(self.terminal_state_persisted, bool):
            raise TypeError("terminal_state_persisted must be a bool")
        if self.run.status is CollectionRunStatus.RUNNING:
            raise ValueError("a collection result requires a terminal run")
        if self.run.status is CollectionRunStatus.SUCCEEDED and (
            self.outcome is None or self.outcome.status is not ScanStatus.COMPLETE
        ):
            raise ValueError("a succeeded collection result requires a complete outcome")
        if self.run.status is CollectionRunStatus.PARTIAL and (
            self.outcome is None or self.outcome.status is not ScanStatus.PARTIAL
        ):
            raise ValueError("a partial collection result requires a partial outcome")

    @property
    def is_complete_success(self) -> bool:
        """True only for a persisted succeeded run with a complete scan outcome."""
        return (
            self.terminal_state_persisted
            and self.run.status is CollectionRunStatus.SUCCEEDED
            and self.outcome is not None
            and self.outcome.status is ScanStatus.COMPLETE
        )


class CollectionError(RuntimeError):
    """Base class for failures that make a scan unsafe for lifecycle work."""


class CollectionProtocolError(CollectionError):
    """An adapter violated the required raw-posting/terminal-outcome stream contract."""


class CollectionStateTransitionError(CollectionError):
    """A caller attempted an invalid in-memory collection run transition."""


class CollectionFailedError(CollectionError):
    """Raised after the service has attempted to persist the failed terminal state."""

    def __init__(self, result: CollectionResult) -> None:
        super().__init__(f"collection run {result.run.id} failed")
        self.result = result


@dataclass(slots=True)
class CollectionService:
    """Create a run, consume one adapter stream, and persist its terminal state.

    This service deliberately retains raw postings only in :class:`CollectionResult`.
    It does not normalize, upsert, version, or inactivate jobs; those are later
    application and persistence responsibilities.
    """

    run_store: CollectionRunStore
    logger: stdlib_logging.Logger
    run_id_factory: RunIdFactory
    clock: UtcClock = utcnow
    timeout_seconds: float = 30.0

    async def collect(self, *, source_key: str, adapter: SourceAdapter) -> CollectionResult:
        """Collect one source, returning partial data but raising for failed scans."""
        started_at = _as_utc(self.clock(), field_name="started_at")
        run = CollectionRun.start(
            run_id=self.run_id_factory(),
            source_key=source_key,
            source_kind=adapter.kind,
            started_at=started_at,
        )
        context = FetchContext(
            run_id=run.id,
            fetched_at=started_at,
            timeout_seconds=self.timeout_seconds,
        )
        self.run_store.create(run)

        run_logger = bind_source_run(
            self.logger,
            run_id=run.id,
            source_key=run.source_key,
            source_kind=run.source_kind,
            started_at=run.started_at,
            clock=self.clock,
        )
        self._log_started(run_logger)

        postings: list[RawJobPosting] = []
        outcome: ScanOutcome | None = None
        try:
            await adapter.validate()
            async for event in adapter.fetch(context):
                outcome = self._consume_event(
                    event=event,
                    outcome=outcome,
                    postings=postings,
                    source_key=run.source_key,
                    source_kind=run.source_kind,
                )
            if outcome is None:
                raise CollectionProtocolError(
                    "adapter stream ended without a terminal scan outcome"
                )

            status = (
                CollectionRunStatus.SUCCEEDED
                if outcome.status is ScanStatus.COMPLETE
                else CollectionRunStatus.PARTIAL
            )
            counts = RunCounts(items_fetched=len(postings))
            final_run = run.finish(
                status=status,
                finished_at=_as_utc(self.clock(), field_name="finished_at"),
                counts=counts,
            )
            self.run_store.update(final_run)
            self._log_finished(run_logger, final_run)
            return CollectionResult(
                run=final_run,
                postings=tuple(postings),
                outcome=outcome,
                terminal_state_persisted=True,
            )
        except Exception as error:
            return self._raise_failed(
                run=run,
                postings=postings,
                outcome=outcome,
                error=error,
                run_logger=run_logger,
            )

    @staticmethod
    def _consume_event(
        *,
        event: FetchEvent,
        outcome: ScanOutcome | None,
        postings: list[RawJobPosting],
        source_key: str,
        source_kind: str,
    ) -> ScanOutcome | None:
        if isinstance(event, RawJobPosting):
            if outcome is not None:
                raise CollectionProtocolError(
                    "adapter yielded a posting after its terminal outcome"
                )
            if event.source_key != source_key or event.source_kind != source_kind:
                raise CollectionProtocolError(
                    "adapter yielded a posting with a mismatched source identity"
                )
            postings.append(event)
            return None
        if isinstance(event, ScanOutcome):
            if outcome is not None:
                raise CollectionProtocolError("adapter yielded more than one terminal scan outcome")
            return event
        raise CollectionProtocolError(
            f"adapter yielded an unsupported stream event: {type(event).__name__}"
        )

    def _raise_failed(
        self,
        *,
        run: CollectionRun,
        postings: list[RawJobPosting],
        outcome: ScanOutcome | None,
        error: Exception,
        run_logger: SourceRunLogger,
    ) -> NoReturn:
        safe_error = safe_exception(error)
        counts = RunCounts(items_fetched=len(postings))
        try:
            finished_at = _as_utc(self.clock(), field_name="finished_at")
        except Exception as clock_error:
            error.add_note(f"Could not read finishing clock: {type(clock_error).__name__}")
            finished_at = run.started_at
        failed_run = run.finish(
            status=CollectionRunStatus.FAILED,
            finished_at=finished_at,
            counts=counts,
            error_summary=safe_error.message,
        )
        terminal_state_persisted = False
        try:
            self.run_store.update(failed_run)
            terminal_state_persisted = True
        except Exception as state_error:
            error.add_note(f"Could not persist failed state: {type(state_error).__name__}")
        self._log_failed(run_logger, failed_run, error)
        result = CollectionResult(
            run=failed_run,
            postings=tuple(postings),
            outcome=outcome,
            terminal_state_persisted=terminal_state_persisted,
        )
        raise CollectionFailedError(result) from error

    @staticmethod
    def _log_started(run_logger: SourceRunLogger) -> None:
        try:
            run_logger.started()
        except Exception:
            return

    @staticmethod
    def _log_finished(run_logger: SourceRunLogger, run: CollectionRun) -> None:
        try:
            if run.finished_at is None:
                return
            run_logger.finished(status=run.status, counts=run.counts, finished_at=run.finished_at)
        except Exception:
            return

    @staticmethod
    def _log_failed(run_logger: SourceRunLogger, run: CollectionRun, error: Exception) -> None:
        try:
            if run.finished_at is None:
                return
            run_logger.finished(
                status=run.status,
                counts=run.counts,
                finished_at=run.finished_at,
                error=error,
            )
        except Exception:
            return
