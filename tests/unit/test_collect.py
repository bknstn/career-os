"""Contract tests for the source-neutral collection application service."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import StringIO

import pytest

from career_os.application.collect import (
    CollectionFailedError,
    CollectionProtocolError,
    CollectionResult,
    CollectionRun,
    CollectionRunStatus,
    CollectionService,
    CollectionStateTransitionError,
)
from career_os.domain import (
    FetchContext,
    FetchEvent,
    RawJobPosting,
    RunCounts,
    ScanOutcome,
    ScanStatus,
)
from career_os.logging import configure_logging


@dataclass
class _InMemoryRunStore:
    history: list[CollectionRun] = field(default_factory=list)

    def create(self, run: CollectionRun) -> None:
        self.history.append(run)

    def update(self, run: CollectionRun) -> None:
        self.history.append(run)


@dataclass
class _FailingUpdateStore(_InMemoryRunStore):
    def update(self, run: CollectionRun) -> None:
        raise RuntimeError("state storage unavailable")


@dataclass
class _FakeAdapter:
    kind: str = "greenhouse"
    events: list[object] = field(default_factory=list)
    validation_error: Exception | None = None
    validate_calls: int = 0
    fetch_contexts: list[FetchContext] = field(default_factory=list)

    async def validate(self) -> None:
        self.validate_calls += 1
        if self.validation_error is not None:
            raise self.validation_error

    async def fetch(self, context: FetchContext) -> AsyncIterator[FetchEvent]:
        self.fetch_contexts.append(context)
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event  # type: ignore[misc]


def _posting(*, source_key: str = "example", source_kind: str = "greenhouse") -> RawJobPosting:
    return RawJobPosting(
        source_key=source_key,
        source_kind=source_kind,
        external_id="job-1",
        source_url=None,
        apply_url=None,
        payload={"id": "job-1"},
        fetched_at=datetime(2026, 8, 17, tzinfo=UTC),
    )


def _service(
    store: _InMemoryRunStore,
    *,
    clock: list[datetime] | None = None,
    stream: StringIO | None = None,
) -> CollectionService:
    timestamps = iter(
        clock
        or [
            datetime(2026, 8, 17, tzinfo=UTC),
            datetime(2026, 8, 17, 0, 0, 1, tzinfo=UTC),
        ]
    )
    logger = configure_logging(
        logger_name=f"career_os.tests.collect.{id(store)}",
        stream=stream or StringIO(),
        clock=lambda: datetime(2026, 8, 17, tzinfo=UTC),
    )
    return CollectionService(
        run_store=store,
        logger=logger,
        run_id_factory=lambda: "run-1",
        clock=lambda: next(timestamps),
    )


def _collect(service: CollectionService, adapter: _FakeAdapter) -> CollectionResult:
    return asyncio.run(service.collect(source_key="example", adapter=adapter))


def _failed_result(service: CollectionService, adapter: _FakeAdapter) -> CollectionFailedError:
    with pytest.raises(CollectionFailedError) as raised:
        _collect(service, adapter)
    return raised.value


def test_complete_scan_transitions_running_to_succeeded_and_preserves_raw_postings() -> None:
    store = _InMemoryRunStore()
    adapter = _FakeAdapter(events=[_posting(), ScanOutcome(ScanStatus.COMPLETE)])

    result = _collect(_service(store), adapter)

    assert result.run.status is CollectionRunStatus.SUCCEEDED
    assert result.outcome == ScanOutcome(ScanStatus.COMPLETE)
    assert result.is_complete_success is True
    assert result.terminal_state_persisted is True
    assert result.postings == (_posting(),)
    assert result.run.counts == RunCounts(items_fetched=1)
    assert result.run.counts.inactivated == 0
    assert [run.status for run in store.history] == [
        CollectionRunStatus.RUNNING,
        CollectionRunStatus.SUCCEEDED,
    ]
    assert adapter.validate_calls == 1
    assert adapter.fetch_contexts[0].run_id == "run-1"


def test_empty_complete_scan_is_successful() -> None:
    store = _InMemoryRunStore()

    result = _collect(_service(store), _FakeAdapter(events=[ScanOutcome(ScanStatus.COMPLETE)]))

    assert result.run.status is CollectionRunStatus.SUCCEEDED
    assert result.postings == ()
    assert result.is_complete_success is True
    assert result.run.counts.items_fetched == 0


def test_partial_scan_returns_a_result_and_transitions_to_partial() -> None:
    store = _InMemoryRunStore()
    outcome = ScanOutcome(ScanStatus.PARTIAL, incomplete_reason="page timed out")

    result = _collect(_service(store), _FakeAdapter(events=[_posting(), outcome]))

    assert result.run.status is CollectionRunStatus.PARTIAL
    assert result.outcome == outcome
    assert result.is_complete_success is False
    assert result.postings == (_posting(),)
    assert [run.status for run in store.history] == [
        CollectionRunStatus.RUNNING,
        CollectionRunStatus.PARTIAL,
    ]


def test_running_state_is_created_before_validation_failure_and_failed_is_persisted() -> None:
    store = _InMemoryRunStore()
    adapter = _FakeAdapter(validation_error=ValueError("invalid adapter configuration"))

    failure = _failed_result(_service(store), adapter)

    assert isinstance(failure.__cause__, ValueError)
    assert failure.result.run.status is CollectionRunStatus.FAILED
    assert failure.result.postings == ()
    assert failure.result.is_complete_success is False
    assert failure.result.terminal_state_persisted is True
    assert [run.status for run in store.history] == [
        CollectionRunStatus.RUNNING,
        CollectionRunStatus.FAILED,
    ]
    assert adapter.validate_calls == 1
    assert adapter.fetch_contexts == []


def test_mid_stream_adapter_exception_fails_and_retains_observed_raw_postings() -> None:
    store = _InMemoryRunStore()
    adapter = _FakeAdapter(events=[_posting(), TimeoutError("adapter timed out")])

    failure = _failed_result(_service(store), adapter)

    assert isinstance(failure.__cause__, TimeoutError)
    assert failure.result.run.status is CollectionRunStatus.FAILED
    assert failure.result.postings == (_posting(),)
    assert failure.result.run.counts.items_fetched == 1


@pytest.mark.parametrize(
    "events",
    [
        [],
        [ScanOutcome(ScanStatus.COMPLETE), ScanOutcome(ScanStatus.COMPLETE)],
        [ScanOutcome(ScanStatus.COMPLETE), _posting()],
        [object()],
    ],
)
def test_malformed_streams_always_fail_never_succeed_or_partial(events: list[object]) -> None:
    store = _InMemoryRunStore()

    failure = _failed_result(_service(store), _FakeAdapter(events=events))

    assert isinstance(failure.__cause__, CollectionProtocolError)
    assert failure.result.run.status is CollectionRunStatus.FAILED
    assert [run.status for run in store.history] == [
        CollectionRunStatus.RUNNING,
        CollectionRunStatus.FAILED,
    ]


def test_posting_source_identity_mismatch_fails_the_run() -> None:
    store = _InMemoryRunStore()
    adapter = _FakeAdapter(events=[_posting(source_key="other"), ScanOutcome(ScanStatus.COMPLETE)])

    failure = _failed_result(_service(store), adapter)

    assert isinstance(failure.__cause__, CollectionProtocolError)
    assert failure.result.run.status is CollectionRunStatus.FAILED


def test_failure_transition_attempt_does_not_mask_the_original_collection_error() -> None:
    store = _FailingUpdateStore()
    adapter = _FakeAdapter(validation_error=ValueError("invalid adapter configuration"))

    failure = _failed_result(_service(store), adapter)

    assert isinstance(failure.__cause__, ValueError)
    assert [run.status for run in store.history] == [CollectionRunStatus.RUNNING]
    assert failure.result.terminal_state_persisted is False


def test_complete_outcome_followed_by_an_exception_is_not_a_complete_success() -> None:
    store = _InMemoryRunStore()
    adapter = _FakeAdapter(
        events=[ScanOutcome(ScanStatus.COMPLETE), RuntimeError("stream closed unexpectedly")]
    )

    failure = _failed_result(_service(store), adapter)

    assert isinstance(failure.__cause__, RuntimeError)
    assert failure.result.run.status is CollectionRunStatus.FAILED
    assert failure.result.outcome == ScanOutcome(ScanStatus.COMPLETE)
    assert failure.result.is_complete_success is False
    assert failure.result.terminal_state_persisted is True


def test_terminal_update_failures_produce_a_failed_unpersisted_result() -> None:
    store = _FailingUpdateStore()
    adapter = _FakeAdapter(events=[ScanOutcome(ScanStatus.COMPLETE)])

    failure = _failed_result(_service(store), adapter)

    assert isinstance(failure.__cause__, RuntimeError)
    assert failure.result.run.status is CollectionRunStatus.FAILED
    assert failure.result.outcome == ScanOutcome(ScanStatus.COMPLETE)
    assert failure.result.is_complete_success is False
    assert failure.result.terminal_state_persisted is False
    assert [run.status for run in store.history] == [CollectionRunStatus.RUNNING]


def test_collection_emits_bound_start_finish_counts_and_status_logs() -> None:
    stream = StringIO()
    store = _InMemoryRunStore()
    service = _service(
        store,
        stream=stream,
        clock=[
            datetime(2026, 8, 17, tzinfo=UTC),
            datetime(2026, 8, 17, tzinfo=UTC) + timedelta(seconds=2),
        ],
    )

    _collect(service, _FakeAdapter(events=[_posting(), ScanOutcome(ScanStatus.COMPLETE)]))

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["event"] for event in events] == ["source_run_started", "source_run_finished"]
    assert events[1]["status"] == "succeeded"
    assert events[1]["items_fetched"] == 1
    assert events[1]["duration_seconds"] == 2.0
    assert all(event["run_id"] == "run-1" for event in events)


def test_failed_collection_logs_a_sanitized_error_and_failed_status() -> None:
    stream = StringIO()
    store = _InMemoryRunStore()
    service = _service(store, stream=stream)

    _failed_result(service, _FakeAdapter(validation_error=RuntimeError("password=log-secret")))

    rendered = stream.getvalue()
    assert "log-secret" not in rendered
    events = [json.loads(line) for line in rendered.splitlines()]
    assert events[1]["event"] == "source_run_finished"
    assert events[1]["status"] == "failed"
    assert events[1]["exception_type"] == "RuntimeError"
    assert events[1]["error_message"] == "password=[redacted]"


def test_collection_run_contract_rejects_invalid_timestamps_and_transitions() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        CollectionRun.start(
            run_id="run-1",
            source_key="example",
            source_kind="greenhouse",
            started_at=datetime(2026, 8, 17),
        )

    running = CollectionRun.start(
        run_id="run-1",
        source_key="example",
        source_kind="greenhouse",
        started_at=datetime(2026, 8, 17, tzinfo=UTC),
    )
    succeeded = running.finish(
        status=CollectionRunStatus.SUCCEEDED,
        finished_at=datetime(2026, 8, 17, tzinfo=UTC),
        counts=RunCounts(),
    )

    with pytest.raises(CollectionStateTransitionError):
        succeeded.finish(
            status=CollectionRunStatus.FAILED,
            finished_at=datetime(2026, 8, 17, tzinfo=UTC),
            counts=RunCounts(),
            error_summary="not allowed",
        )
    with pytest.raises(CollectionStateTransitionError):
        running.finish(
            status=CollectionRunStatus.RUNNING,
            finished_at=datetime(2026, 8, 17, tzinfo=UTC),
            counts=RunCounts(),
        )


def test_service_rejects_a_naive_clock_before_creating_a_run() -> None:
    store = _InMemoryRunStore()
    logger = configure_logging(
        logger_name="career_os.tests.collect.naive-clock",
        stream=StringIO(),
    )
    service = CollectionService(
        run_store=store,
        logger=logger,
        run_id_factory=lambda: "run-1",
        clock=lambda: datetime(2026, 8, 17),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        _collect(service, _FakeAdapter(events=[ScanOutcome(ScanStatus.COMPLETE)]))

    assert store.history == []
