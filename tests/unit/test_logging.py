"""Tests for structured, redacted source-run logging."""

from __future__ import annotations

import json
import logging as stdlib_logging
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import Any

import pytest

from career_os.logging import RunCounts, bind_source_run, configure_logging, safe_exception


def _lines(stream: StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_source_run_logs_a_machine_readable_start_and_finish_schema() -> None:
    stream = StringIO()
    emitted_at = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    started_at = datetime(2026, 8, 17, 9, 59, tzinfo=UTC)
    finished_at = started_at + timedelta(seconds=12.5)
    logger = configure_logging(
        logger_name="career_os.tests.logging.schema",
        stream=stream,
        clock=lambda: emitted_at,
    )
    run_logger = bind_source_run(
        logger,
        run_id="run-1",
        source_key="example",
        source_kind="greenhouse",
        started_at=started_at,
    )

    run_logger.started()
    run_logger.finished(
        status="succeeded",
        counts=RunCounts(items_fetched=3, inserted=1, unchanged=2, http_requests=4),
        finished_at=finished_at,
    )

    start_event, finish_event = _lines(stream)
    assert start_event == {
        "event": "source_run_started",
        "level": "INFO",
        "run_id": "run-1",
        "source_key": "example",
        "source_kind": "greenhouse",
        "started_at": "2026-08-17T09:59:00Z",
        "timestamp": "2026-08-17T10:00:00Z",
    }
    assert finish_event["event"] == "source_run_finished"
    assert finish_event["level"] == "INFO"
    assert finish_event["status"] == "succeeded"
    assert finish_event["started_at"] == "2026-08-17T09:59:00Z"
    assert finish_event["finished_at"] == "2026-08-17T09:59:12.500000Z"
    assert finish_event["duration_seconds"] == 12.5
    assert finish_event["items_fetched"] == 3
    assert finish_event["inserted"] == 1
    assert finish_event["unchanged"] == 2
    assert finish_event["http_requests"] == 4
    assert finish_event["retry_count"] == 0


def test_bound_context_does_not_leak_between_runs() -> None:
    stream = StringIO()
    logger = configure_logging(
        logger_name="career_os.tests.logging.context",
        stream=stream,
        clock=lambda: datetime(2026, 8, 17, tzinfo=UTC),
    )
    first = bind_source_run(
        logger,
        run_id="first-run",
        source_key="first-source",
        source_kind="greenhouse",
        started_at=datetime(2026, 8, 17, tzinfo=UTC),
    )
    second = bind_source_run(
        logger,
        run_id="second-run",
        source_key="second-source",
        source_kind="lever",
        started_at=datetime(2026, 8, 17, tzinfo=UTC),
    )

    first.event("first_event", attempt=1)
    second.event("second_event", attempt=2)
    first.event("first_event_again", attempt=3)

    assert [
        (event["run_id"], event["source_key"], event["source_kind"]) for event in _lines(stream)
    ] == [
        ("first-run", "first-source", "greenhouse"),
        ("second-run", "second-source", "lever"),
        ("first-run", "first-source", "greenhouse"),
    ]


def test_reconfiguration_uses_one_handler_and_leaves_root_logger_unchanged() -> None:
    stream = StringIO()
    root = stdlib_logging.getLogger()
    original_root_level = root.level
    original_root_handlers = tuple(root.handlers)

    logger = configure_logging(logger_name="career_os.tests.logging.idempotent", stream=stream)
    configure_logging(logger_name="career_os.tests.logging.idempotent", stream=stream)

    assert len(logger.handlers) == 1
    assert root.level == original_root_level
    assert tuple(root.handlers) == original_root_handlers


def test_error_and_scalar_fields_are_redacted_before_emission() -> None:
    stream = StringIO()
    logger = configure_logging(
        logger_name="career_os.tests.logging.redaction",
        stream=stream,
        clock=lambda: datetime(2026, 8, 17, tzinfo=UTC),
    )
    run_logger = bind_source_run(
        logger,
        run_id="run-1",
        source_key="example",
        source_kind="greenhouse",
        started_at=datetime(2026, 8, 17, tzinfo=UTC),
    )
    error = RuntimeError(
        "GET https://alice:password123@example.test/jobs?token=query-secret "
        "password=plain-secret Authorization: Bearer bearer-secret"
    )

    run_logger.event(
        "request_failed",
        endpoint="https://bob:other@example.test/token/path-secret?api_key=api-secret",
        password="field-password",
        api_token="field-token",
    )
    run_logger.finished(status="failed", counts=RunCounts(failed_items=1), error=error)

    rendered = stream.getvalue()
    for secret in (
        "password123",
        "query-secret",
        "plain-secret",
        "bearer-secret",
        "other",
        "api-secret",
        "field-password",
        "field-token",
        "path-secret",
    ):
        assert secret not in rendered
    events = _lines(stream)
    assert events[0]["endpoint"] == "https://example.test/token/[redacted]?[redacted]"
    assert events[0]["password"] == "[redacted]"
    assert events[0]["api_token"] == "[redacted]"
    assert events[1]["exception_type"] == "RuntimeError"
    assert "[redacted]" in events[1]["error_message"]
    assert events[1]["level"] == "ERROR"


def test_safe_exception_redacts_raw_structured_and_html_response_bodies() -> None:
    json_body = safe_exception(RuntimeError('{"description":"private job description"}'))
    html_body = safe_exception(RuntimeError("<html><body>private response</body></html>"))

    assert json_body.message == "[redacted structured payload]"
    assert html_body.message == "[redacted html payload]"


def test_formatter_redacts_sensitive_fields_on_direct_stdlib_records() -> None:
    stream = StringIO()
    logger = configure_logging(
        logger_name="career_os.tests.logging.direct-record",
        stream=stream,
        clock=lambda: datetime(2026, 8, 17, tzinfo=UTC),
    )

    logger.info(
        "ignored by the formatter",
        extra={
            "event": "direct_record",
            "run_id": "run-1",
            "source_key": "example",
            "source_kind": "greenhouse",
            "structured_fields": {
                "api_key": "direct-api-key",
                "Cookie": "direct-cookie",
                "safe_count": 2,
                "description": "must be omitted",
                "rawPayload": "must also be omitted",
                "responseBody": "must also be omitted",
            },
        },
    )

    rendered = stream.getvalue()
    assert "direct-api-key" not in rendered
    assert "direct-cookie" not in rendered
    event = _lines(stream)[0]
    assert event["api_key"] == "[redacted]"
    assert event["Cookie"] == "[redacted]"
    assert event["safe_count"] == 2
    assert "description" not in event
    assert "rawPayload" not in event
    assert "responseBody" not in event


def test_formatter_sanitizes_direct_record_events() -> None:
    stream = StringIO()
    logger = configure_logging(
        logger_name="career_os.tests.logging.direct-event",
        stream=stream,
        clock=lambda: datetime(2026, 8, 17, tzinfo=UTC),
    )

    logger.info("ignored", extra={"event": "password=event-secret"})

    rendered = stream.getvalue()
    assert "event-secret" not in rendered
    assert _lines(stream)[0]["event"] == "password=[redacted]"


def test_counts_and_completion_time_are_validated() -> None:
    with pytest.raises(ValueError, match="retry_count"):
        RunCounts(retry_count=-1)

    stream = StringIO()
    logger = configure_logging(logger_name="career_os.tests.logging.validation", stream=stream)
    run_logger = bind_source_run(
        logger,
        run_id="run-1",
        source_key="example",
        source_kind="greenhouse",
        started_at=datetime(2026, 8, 17, 1, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="finished_at"):
        run_logger.finished(
            status="failed",
            counts=RunCounts(),
            finished_at=datetime(2026, 8, 17, tzinfo=UTC),
        )

    with pytest.raises(ValueError, match="not permitted"):
        run_logger.event("unsafe", description="a raw job description")

    with pytest.raises(ValueError, match="not permitted"):
        run_logger.event("unsafe", rawPayload="a raw source payload")

    with pytest.raises(ValueError, match="finite"):
        run_logger.event("unsafe", duration_seconds=float("nan"))
