"""Offline contract tests for the Greenhouse public Job Board API adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import assert_type

import pytest

from career_os.adapters.greenhouse import (
    GreenhouseAdapter,
    GreenhouseHttpResponse,
    GreenhouseTransportError,
    _response_from_bytes,
)
from career_os.config.sources import GreenhouseSourceConfig
from career_os.domain import (
    FetchContext,
    FetchEvent,
    RawJobPosting,
    ScanDiagnostics,
    ScanFailedError,
    ScanOutcome,
    ScanStatus,
    SourceAdapter,
)

_VALID_JOB: dict[str, object] = {
    "id": 12345,
    "title": "Valid job retained",
    "absolute_url": "https://boards.greenhouse.io/example/jobs/12345",
}


class _FakeHttpClient:
    def __init__(self, responses: list[GreenhouseHttpResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, Mapping[str, str], float]] = []

    async def get(
        self, *, url: str, headers: Mapping[str, str], timeout_seconds: float
    ) -> GreenhouseHttpResponse:
        self.requests.append((url, headers, timeout_seconds))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.parametrize(
    "body", [b'{"value": NaN}', b'{"value": Infinity}', b'{"value": -Infinity}']
)
def test_real_bytes_parser_rejects_nonstandard_json_constants(body: bytes) -> None:
    response = _response_from_bytes(status_code=200, headers={}, body=body)

    assert response.json_body is None
    assert response.json_error == "response body was not valid UTF-8 JSON"


def test_real_bytes_parser_preserves_valid_utf8_json() -> None:
    response = _response_from_bytes(
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=b'{"jobs": [], "meta": {"total": 0}}',
    )

    assert response.json_body == {"jobs": [], "meta": {"total": 0}}
    assert response.json_error is None


def test_populated_recording_preserves_source_native_payload_and_completes() -> None:
    payload = _fixture("populated.json")
    client = _FakeHttpClient([_response(200, payload)])
    adapter = GreenhouseAdapter(config=_config(), http_client=client)

    events = _fetch(adapter)

    postings = [event for event in events if isinstance(event, RawJobPosting)]
    assert [posting.external_id for posting in postings] == ["12345", "67890"]
    assert postings[0].source_url == "https://boards.greenhouse.io/example/jobs/12345"
    assert postings[0].apply_url is None
    assert postings[0].fetched_at == _context().fetched_at
    assert postings[0].payload == payload["jobs"][0]
    assert postings[0].payload["unknown_future_field"] == {"kept": ["without", "normalization"]}
    assert events[-1] == ScanOutcome(
        ScanStatus.COMPLETE, diagnostics=ScanDiagnostics(http_requests=1)
    )
    assert client.requests == [
        (
            "https://boards-api.greenhouse.io/v1/boards/example-board/jobs?content=true",
            {
                "Accept": "application/json",
                "User-Agent": "career-os/0.1.0 GreenhouseJobBoardAdapter",
            },
            12.0,
        )
    ]


def test_empty_recording_is_a_complete_empty_scan() -> None:
    adapter = GreenhouseAdapter(
        config=_config(), http_client=_FakeHttpClient([_response(200, _fixture("empty.json"))])
    )

    assert _fetch(adapter) == [
        ScanOutcome(ScanStatus.COMPLETE, diagnostics=ScanDiagnostics(http_requests=1))
    ]


def test_malformed_items_produce_a_partial_terminal_outcome_after_valid_evidence() -> None:
    adapter = GreenhouseAdapter(
        config=_config(),
        http_client=_FakeHttpClient([_response(200, _fixture("malformed.json"))]),
    )

    events = _fetch(adapter)

    assert [event.external_id for event in events if isinstance(event, RawJobPosting)] == ["12345"]
    assert events[-1] == ScanOutcome(
        ScanStatus.PARTIAL,
        incomplete_reason=(
            "job item 1 was malformed; job item 2 was malformed; "
            "job item 3 duplicated an external ID"
        ),
        diagnostics=ScanDiagnostics(http_requests=1, rejected_items=3),
    )


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (
            {"jobs": [dict(_VALID_JOB)]},
            "Greenhouse response meta was not an object",
        ),
        (
            {"jobs": [dict(_VALID_JOB)], "meta": {}},
            "Greenhouse response meta.total was not a non-negative integer",
        ),
        (
            {"jobs": [dict(_VALID_JOB)], "meta": {"total": True}},
            "Greenhouse response meta.total was not a non-negative integer",
        ),
        (
            {"jobs": [dict(_VALID_JOB)], "meta": {"total": 2}},
            "Greenhouse response meta.total did not match jobs list length",
        ),
    ],
)
def test_unproven_response_total_keeps_valid_evidence_but_is_partial(
    payload: dict[str, object], reason: str
) -> None:
    adapter = GreenhouseAdapter(
        config=_config(), http_client=_FakeHttpClient([_response(200, payload)])
    )

    events = _fetch(adapter)

    assert [event.external_id for event in events if isinstance(event, RawJobPosting)] == ["12345"]
    assert events[-1] == ScanOutcome(
        ScanStatus.PARTIAL,
        incomplete_reason=reason,
        diagnostics=ScanDiagnostics(http_requests=1),
    )
    _assert_one_terminal_outcome(events)


def test_total_mismatch_and_malformed_item_are_combined_deterministically() -> None:
    payload = {
        "jobs": [dict(_VALID_JOB), {"title": "missing stable ID"}],
        "meta": {"total": 3},
    }
    adapter = GreenhouseAdapter(
        config=_config(), http_client=_FakeHttpClient([_response(200, payload)])
    )

    events = _fetch(adapter)

    assert [event.external_id for event in events if isinstance(event, RawJobPosting)] == ["12345"]
    assert events[-1] == ScanOutcome(
        ScanStatus.PARTIAL,
        incomplete_reason=(
            "Greenhouse response meta.total did not match jobs list length; "
            "job item 1 was malformed"
        ),
        diagnostics=ScanDiagnostics(http_requests=1, rejected_items=1),
    )
    _assert_one_terminal_outcome(events)


@pytest.mark.parametrize(
    "response_kind",
    [
        "jobs_not_list",
        "invalid_json",
    ],
)
def test_malformed_success_response_is_partial_not_complete(
    response_kind: str,
) -> None:
    response = (
        _response(200, {"jobs": "not a list"})
        if response_kind == "jobs_not_list"
        else GreenhouseHttpResponse(
            status_code=200,
            headers={},
            json_body=None,
            json_error="response body was not valid UTF-8 JSON",
        )
    )
    adapter = GreenhouseAdapter(config=_config(), http_client=_FakeHttpClient([response]))

    events = _fetch(adapter)

    assert len(events) == 1
    assert isinstance(events[0], ScanOutcome)
    assert events[0].status is ScanStatus.PARTIAL
    _assert_one_terminal_outcome(events)


@pytest.mark.parametrize("fixture_name", ["populated.json", "empty.json", "malformed.json"])
def test_successful_response_stream_has_one_final_terminal_outcome(fixture_name: str) -> None:
    adapter = GreenhouseAdapter(
        config=_config(), http_client=_FakeHttpClient([_response(200, _fixture(fixture_name))])
    )

    _assert_one_terminal_outcome(_fetch(adapter))


def test_permanent_http_error_is_not_retried() -> None:
    client = _FakeHttpClient([_response(404, {"errors": ["not found"]})])
    sleeps: list[float] = []
    adapter = GreenhouseAdapter(
        config=_config(),
        http_client=client,
        sleep=lambda seconds: _record_sleep(sleeps, seconds),
    )

    with pytest.raises(RuntimeError, match="non-retryable HTTP 404"):
        _fetch(adapter)

    assert len(client.requests) == 1
    assert sleeps == []


def test_retry_after_is_honored_before_a_successful_retry() -> None:
    client = _FakeHttpClient(
        [
            _response(429, {"error": "rate limited"}, headers={"Retry-After": "3.5"}),
            _response(200, _fixture("empty.json")),
        ]
    )
    sleeps: list[float] = []
    adapter = GreenhouseAdapter(
        config=_config(),
        http_client=client,
        sleep=lambda seconds: _record_sleep(sleeps, seconds),
        retry_jitter=lambda _attempt: 99.0,
    )

    assert _fetch(adapter) == [
        ScanOutcome(
            ScanStatus.COMPLETE,
            diagnostics=ScanDiagnostics(http_requests=2, retry_count=1, rate_limit_events=1),
        )
    ]
    assert len(client.requests) == 2
    assert sleeps == [3.5]


def test_retry_after_http_date_is_honored_with_an_injected_utc_clock() -> None:
    client = _FakeHttpClient(
        [
            _response(
                429,
                {"error": "rate limited"},
                headers={"Retry-After": "Tue, 18 Aug 2026 10:00:04 GMT"},
            ),
            _response(200, _fixture("empty.json")),
        ]
    )
    sleeps: list[float] = []
    adapter = GreenhouseAdapter(
        config=_config(),
        http_client=client,
        sleep=lambda seconds: _record_sleep(sleeps, seconds),
        retry_jitter=lambda _attempt: 0.25,
        utc_now=lambda: datetime(2026, 8, 18, 10, tzinfo=UTC),
    )

    assert _fetch(adapter) == [
        ScanOutcome(
            ScanStatus.COMPLETE,
            diagnostics=ScanDiagnostics(http_requests=2, retry_count=1, rate_limit_events=1),
        )
    ]
    assert sleeps == [4.0]


@pytest.mark.parametrize(
    "retry_after", ["not-a-date", "NaN", "inf", "-1", "Tue, 18 Aug 2026 09:59:59 GMT"]
)
def test_invalid_or_past_retry_after_falls_back_to_jitter(retry_after: str) -> None:
    client = _FakeHttpClient(
        [
            _response(503, {}, headers={"Retry-After": retry_after}),
            _response(200, _fixture("empty.json")),
        ]
    )
    sleeps: list[float] = []
    adapter = GreenhouseAdapter(
        config=_config(),
        http_client=client,
        sleep=lambda seconds: _record_sleep(sleeps, seconds),
        retry_jitter=lambda _attempt: 0.25,
        utc_now=lambda: datetime(2026, 8, 18, 10, tzinfo=UTC),
    )

    assert _fetch(adapter) == [
        ScanOutcome(
            ScanStatus.COMPLETE, diagnostics=ScanDiagnostics(http_requests=2, retry_count=1)
        )
    ]
    assert sleeps == [0.25]


@pytest.mark.parametrize(
    ("first_response", "retry_jitter"),
    [
        (
            GreenhouseHttpResponse(status_code=429, headers={"Retry-After": "1000"}, json_body={}),
            lambda _attempt: 0.25,
        ),
        (GreenhouseTransportError("timed out"), lambda _attempt: 1000.0),
    ],
)
def test_retry_delays_are_capped(
    first_response: GreenhouseHttpResponse | GreenhouseTransportError,
    retry_jitter: Callable[[int], float],
) -> None:
    client = _FakeHttpClient([first_response, _response(200, _fixture("empty.json"))])
    sleeps: list[float] = []
    adapter = GreenhouseAdapter(
        config=_config(),
        http_client=client,
        sleep=lambda seconds: _record_sleep(sleeps, seconds),
        retry_jitter=retry_jitter,
    )

    expected_rate_limits = 1 if isinstance(first_response, GreenhouseHttpResponse) else 0
    assert _fetch(adapter) == [
        ScanOutcome(
            ScanStatus.COMPLETE,
            diagnostics=ScanDiagnostics(
                http_requests=2, retry_count=1, rate_limit_events=expected_rate_limits
            ),
        )
    ]
    assert sleeps == [10.0]


def test_transient_transport_failure_retries_with_injected_jitter() -> None:
    client = _FakeHttpClient(
        [GreenhouseTransportError("timed out"), _response(200, _fixture("empty.json"))]
    )
    sleeps: list[float] = []
    adapter = GreenhouseAdapter(
        config=_config(),
        http_client=client,
        sleep=lambda seconds: _record_sleep(sleeps, seconds),
        retry_jitter=lambda attempt: [0.25, 0.5][attempt],
    )

    assert _fetch(adapter) == [
        ScanOutcome(
            ScanStatus.COMPLETE, diagnostics=ScanDiagnostics(http_requests=2, retry_count=1)
        )
    ]
    assert len(client.requests) == 2
    assert sleeps == [0.25]


def test_transient_http_retry_exhaustion_raises_after_the_bounded_attempts() -> None:
    client = _FakeHttpClient([_response(503, {}), _response(503, {}), _response(503, {})])
    sleeps: list[float] = []
    adapter = GreenhouseAdapter(
        config=_config(),
        http_client=client,
        sleep=lambda seconds: _record_sleep(sleeps, seconds),
        retry_jitter=lambda attempt: [0.25, 0.5][attempt],
        max_attempts=3,
    )

    with pytest.raises(RuntimeError, match="exhausted retries after HTTP 503"):
        _fetch(adapter)

    assert len(client.requests) == 3
    assert sleeps == [0.25, 0.5]


def test_retry_scheduling_failure_retains_telemetry_after_an_attempt() -> None:
    adapter = GreenhouseAdapter(
        config=_config(),
        http_client=_FakeHttpClient([_response(503, {})]),
        retry_jitter=lambda _attempt: -1.0,
    )

    with pytest.raises(ScanFailedError) as raised:
        _fetch(adapter)

    assert raised.value.diagnostics == ScanDiagnostics(http_requests=1, retry_count=1)


def test_unexpected_transport_failure_is_wrapped_with_observed_request_count() -> None:
    adapter = GreenhouseAdapter(
        config=_config(),
        http_client=_FakeHttpClient([RuntimeError("socket implementation failed")]),
        max_attempts=1,
    )

    with pytest.raises(ScanFailedError) as raised:
        _fetch(adapter)

    assert raised.value.diagnostics == ScanDiagnostics(http_requests=1)


def test_adapter_implements_the_async_source_contract() -> None:
    adapter: SourceAdapter = GreenhouseAdapter(
        config=_config(), http_client=_FakeHttpClient([_response(200, _fixture("empty.json"))])
    )

    assert_type(adapter, SourceAdapter)
    assert isinstance(adapter, SourceAdapter)
    asyncio.run(adapter.validate())


def _fetch(adapter: GreenhouseAdapter) -> list[FetchEvent]:
    async def collect() -> list[FetchEvent]:
        await adapter.validate()
        return [event async for event in adapter.fetch(_context())]

    return asyncio.run(collect())


def _record_sleep(recorded: list[float], seconds: float):
    recorded.append(seconds)

    async def done() -> None:
        return None

    return done()


def _response(
    status_code: int, json_body: object, *, headers: Mapping[str, str] | None = None
) -> GreenhouseHttpResponse:
    return GreenhouseHttpResponse(
        status_code=status_code, headers=headers or {}, json_body=json_body
    )


def _fixture(name: str) -> dict[str, object]:
    path = Path(__file__).resolve().parents[1] / "fixtures" / "ats" / "greenhouse" / name
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _config() -> GreenhouseSourceConfig:
    return GreenhouseSourceConfig(
        key="example-greenhouse",
        kind="greenhouse",
        company_name="Example Inc.",
        board_token="example-board",
    )


def _context() -> FetchContext:
    return FetchContext(
        run_id="run-1",
        fetched_at=datetime(2026, 8, 18, 10, tzinfo=UTC),
        timeout_seconds=12.0,
    )


def _assert_one_terminal_outcome(events: list[FetchEvent]) -> None:
    outcomes = [event for event in events if isinstance(event, ScanOutcome)]
    assert len(outcomes) == 1
    assert events[-1] is outcomes[0]
