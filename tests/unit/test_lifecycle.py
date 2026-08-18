"""Fail-closed application tests for complete-scan lifecycle reconciliation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from career_os.application import (
    CollectionFailedError,
    CollectionResult,
    CollectionRun,
    CollectionRunStatus,
    LifecycleAction,
    LifecycleService,
)
from career_os.domain import RawJobPosting, RunCounts, ScanOutcome, ScanStatus


@dataclass
class _MutationPort:
    calls: list[tuple[CollectionRun, tuple[str, ...]]] = field(default_factory=list)
    inactivated_count: int = 0

    def inactivate_unseen(self, *, run: CollectionRun, seen_external_ids: tuple[str, ...]) -> int:
        self.calls.append((run, seen_external_ids))
        return self.inactivated_count


def test_complete_persisted_result_reconciles_sorted_deduplicated_source_ids() -> None:
    port = _MutationPort(inactivated_count=2)
    result = _result(
        status=CollectionRunStatus.SUCCEEDED,
        outcome=ScanOutcome(ScanStatus.COMPLETE),
        terminal_state_persisted=True,
        postings=(_posting("job-b"), _posting("job-a"), _posting("job-b")),
    )

    lifecycle_result = LifecycleService(port).reconcile(result)

    assert lifecycle_result.action is LifecycleAction.INACTIVATED
    assert lifecycle_result.seen_external_ids == ("job-a", "job-b")
    assert lifecycle_result.inactivated_count == 2
    assert port.calls == [(result.run, ("job-a", "job-b"))]


@pytest.mark.parametrize("kind", ["partial", "failed", "unpersisted_complete"])
def test_partial_failed_and_unpersisted_terminal_results_are_safe_noops(
    kind: str,
) -> None:
    port = _MutationPort(inactivated_count=5)

    lifecycle_result = LifecycleService(port).reconcile(_unsafe_collection(kind))

    assert lifecycle_result.action is LifecycleAction.SKIPPED_UNSAFE
    assert lifecycle_result.inactivated_count == 0
    assert port.calls == []


def _result(
    *,
    status: CollectionRunStatus,
    outcome: ScanOutcome | None,
    terminal_state_persisted: bool,
    postings: tuple[RawJobPosting, ...] = (),
) -> CollectionResult:
    started_at = datetime(2026, 8, 18, 10, tzinfo=UTC)
    running = CollectionRun.start(
        run_id="run-1",
        source_key="example",
        source_kind="greenhouse",
        started_at=started_at,
    )
    finished = running.finish(
        status=status,
        finished_at=started_at,
        counts=RunCounts(items_fetched=len(postings)),
        error_summary="collection failed" if status is CollectionRunStatus.FAILED else None,
    )
    return CollectionResult(
        run=finished,
        postings=postings,
        outcome=outcome,
        terminal_state_persisted=terminal_state_persisted,
    )


def _posting(external_id: str) -> RawJobPosting:
    return RawJobPosting(
        source_key="example",
        source_kind="greenhouse",
        external_id=external_id,
        source_url=None,
        apply_url=None,
        payload={"id": external_id},
        fetched_at=datetime(2026, 8, 18, 10, tzinfo=UTC),
    )


def _unsafe_collection(kind: str) -> CollectionResult | CollectionFailedError:
    if kind == "partial":
        return _result(
            status=CollectionRunStatus.PARTIAL,
            outcome=ScanOutcome(ScanStatus.PARTIAL, incomplete_reason="page timed out"),
            terminal_state_persisted=True,
        )
    if kind == "failed":
        return CollectionFailedError(
            _result(
                status=CollectionRunStatus.FAILED,
                outcome=None,
                terminal_state_persisted=True,
            )
        )
    if kind == "unpersisted_complete":
        return _result(
            status=CollectionRunStatus.SUCCEEDED,
            outcome=ScanOutcome(ScanStatus.COMPLETE),
            terminal_state_persisted=False,
        )
    raise AssertionError(f"unsupported unsafe collection kind: {kind}")
