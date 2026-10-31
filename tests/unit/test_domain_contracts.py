"""Unit tests for source-neutral adapter boundary invariants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from career_os.domain import (
    FetchContext,
    RawJobPosting,
    ScanOutcome,
    ScanStatus,
)


def test_context_normalizes_an_aware_timestamp_to_utc() -> None:
    context = FetchContext(
        run_id="run-1",
        fetched_at=datetime(2026, 8, 17, 13, tzinfo=timezone(timedelta(hours=2))),
    )

    assert context.fetched_at == datetime(2026, 8, 17, 11, tzinfo=UTC)
    assert context.timeout_seconds == 30.0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"run_id": " ", "fetched_at": datetime.now(UTC)}, "run_id"),
        ({"run_id": "run-1", "fetched_at": datetime(2026, 8, 17)}, "timezone-aware"),
        ({"run_id": "run-1", "fetched_at": datetime.now(UTC), "timeout_seconds": 0}, "finite"),
        (
            {"run_id": "run-1", "fetched_at": datetime.now(UTC), "timeout_seconds": float("inf")},
            "finite",
        ),
    ],
)
def test_context_rejects_unsafe_values(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        FetchContext(**kwargs)  # type: ignore[arg-type]


def test_raw_job_is_an_immutable_top_level_payload_snapshot() -> None:
    source_payload = {"id": "123", "unknown_source_field": True}
    job = RawJobPosting(
        source_key=" example ",
        source_kind=" fake ",
        external_id=" 123 ",
        source_url=None,
        apply_url=None,
        payload=source_payload,
        fetched_at=datetime(2026, 8, 17, tzinfo=UTC),
    )
    source_payload["id"] = "mutated"

    assert job.source_key == "example"
    assert job.source_kind == "fake"
    assert job.external_id == "123"
    assert job.payload == {"id": "123", "unknown_source_field": True}
    assert isinstance(job.payload, MappingProxyType)
    with pytest.raises(TypeError):
        job.payload["new"] = "value"  # type: ignore[index]


def test_raw_job_rejects_blank_identity_and_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="external_id"):
        RawJobPosting(
            source_key="example",
            source_kind="fake",
            external_id=" ",
            source_url=None,
            apply_url=None,
            payload={},
            fetched_at=datetime.now(UTC),
        )

    with pytest.raises(ValueError, match="timezone-aware"):
        RawJobPosting(
            source_key="example",
            source_kind="fake",
            external_id="123",
            source_url=None,
            apply_url=None,
            payload={},
            fetched_at=datetime(2026, 8, 17),
        )


def test_complete_scan_is_the_only_outcome_that_permits_inactivation() -> None:
    complete = ScanOutcome(ScanStatus.COMPLETE)
    partial = ScanOutcome(ScanStatus.PARTIAL, incomplete_reason="page 3 timed out")

    assert complete.permits_inactivation is True
    assert partial.permits_inactivation is False
    assert partial.incomplete_reason == "page 3 timed out"


@pytest.mark.parametrize(
    ("status", "reason", "message"),
    [
        (ScanStatus.COMPLETE, "unexpected", "complete scan"),
        (ScanStatus.PARTIAL, None, "partial scan"),
        (ScanStatus.PARTIAL, "  ", "partial scan"),
    ],
)
def test_scan_outcome_requires_unambiguous_completeness(
    status: ScanStatus, reason: str | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ScanOutcome(status, incomplete_reason=reason)
