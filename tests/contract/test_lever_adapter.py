"""Offline contract tests for the paginated Lever Postings API adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import assert_type

import pytest

from career_os.adapters.lever import (
    LeverAdapter,
    LeverAdapterError,
    LeverHttpResponse,
    LeverTransportError,
    _response_from_bytes,
)
from career_os.config.sources import LeverSourceConfig
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


class _FakeHttpClient:
    def __init__(self, responses: list[LeverHttpResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, Mapping[str, str], float]] = []

    async def get(
        self, *, url: str, headers: Mapping[str, str], timeout_seconds: float
    ) -> LeverHttpResponse:
        self.requests.append((url, headers, timeout_seconds))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.parametrize("body", [b"[NaN]", b"[Infinity]", b"[-Infinity]"])
def test_real_bytes_parser_rejects_nonstandard_json_constants(body: bytes) -> None:
    response = _response_from_bytes(status_code=200, headers={}, body=body)

    assert response.json_body is None
    assert response.json_error == "response body was not valid UTF-8 JSON"


def test_global_populated_recording_traverses_every_page_and_preserves_raw_evidence() -> None:
    page_zero = _fixture("global-page-0.json")
    page_two = _fixture("global-page-2.json")
    client = _FakeHttpClient([_response(200, page_zero), _response(200, page_two)])
    adapter = LeverAdapter(config=_config(), http_client=client, page_size=2)

    events = _fetch(adapter)

    postings = [event for event in events if isinstance(event, RawJobPosting)]
    assert [posting.external_id for posting in postings] == ["lever-001", "lever-002", "lever-003"]
    assert postings[0].payload == page_zero[0]
    assert postings[0].payload["unknownFutureField"] == {"preserved": ["as", "evidence"]}
    assert postings[0].source_url == "https://jobs.lever.co/example/lever-001"
    assert postings[0].apply_url == "https://jobs.lever.co/example/lever-001/apply"
    assert postings[1].apply_url is None
    assert events[-1] == ScanOutcome(
        ScanStatus.COMPLETE, diagnostics=ScanDiagnostics(http_requests=2)
    )
    assert [request[0] for request in client.requests] == [
        "https://api.lever.co/v0/postings/example-site?skip=0&limit=2&mode=json",
        "https://api.lever.co/v0/postings/example-site?skip=2&limit=2&mode=json",
    ]
    assert all(
        request[1]
        == {
            "Accept": "application/json",
            "User-Agent": "career-os/0.1.0 LeverPostingsAdapter",
        }
        and request[2] == 12.0
        for request in client.requests
    )
    _assert_one_terminal_outcome(events)


def test_full_page_requires_an_empty_terminal_page_before_a_complete_outcome() -> None:
    full_page = [
        {"id": "one", "hostedUrl": "https://jobs.lever.co/example/one"},
        {"id": "two", "hostedUrl": "https://jobs.lever.co/example/two"},
    ]
    client = _FakeHttpClient([_response(200, full_page), _response(200, [])])
    events = _fetch(LeverAdapter(config=_config(), http_client=client, page_size=2))

    assert [event.external_id for event in events if isinstance(event, RawJobPosting)] == [
        "one",
        "two",
    ]
    assert events[-1] == ScanOutcome(
        ScanStatus.COMPLETE, diagnostics=ScanDiagnostics(http_requests=2)
    )
    assert client.requests[-1][0].endswith("?skip=2&limit=2&mode=json")


def test_repeated_full_page_without_new_ids_terminates_partial_without_an_unbounded_loop() -> None:
    full_page = [{"id": "one"}, {"id": "two"}]
    client = _FakeHttpClient([_response(200, full_page), _response(200, full_page)])

    events = _fetch(LeverAdapter(config=_config(), http_client=client, page_size=2))

    assert events[-1] == ScanOutcome(
        ScanStatus.PARTIAL,
        incomplete_reason=(
            "page skip 2 item 0 duplicated an external ID; "
            "page skip 2 item 1 duplicated an external ID; "
            "Lever pagination made no progress at skip 2"
        ),
        diagnostics=ScanDiagnostics(http_requests=2, rejected_items=2),
    )
    assert len(client.requests) == 2


def test_full_pages_with_some_progress_stop_at_the_bounded_traversal_limit() -> None:
    client = _FakeHttpClient(
        [
            _response(200, [{"id": "one"}, {"id": "two"}]),
            _response(200, [{"id": "two"}, {"id": "three"}]),
        ]
    )
    events = _fetch(LeverAdapter(config=_config(), http_client=client, page_size=2, max_pages=2))

    assert [event.external_id for event in events if isinstance(event, RawJobPosting)] == [
        "one",
        "two",
        "three",
    ]
    assert events[-1] == ScanOutcome(
        ScanStatus.PARTIAL,
        incomplete_reason=(
            "page skip 2 item 0 duplicated an external ID; "
            "Lever pagination reached the 2-page traversal limit"
        ),
        diagnostics=ScanDiagnostics(http_requests=2, rejected_items=1),
    )
    assert len(client.requests) == 2


def test_eu_empty_recording_uses_eu_host_and_is_a_complete_empty_scan() -> None:
    client = _FakeHttpClient([_response(200, _fixture("eu-empty.json"))])
    adapter = LeverAdapter(config=_config(region="eu"), http_client=client)

    assert _fetch(adapter) == [
        ScanOutcome(ScanStatus.COMPLETE, diagnostics=ScanDiagnostics(http_requests=1))
    ]
    assert client.requests[0][0] == (
        "https://api.eu.lever.co/v0/postings/example-site?skip=0&limit=100&mode=json"
    )


def test_malformed_and_duplicate_items_remain_partial_after_valid_evidence() -> None:
    client = _FakeHttpClient(
        [
            _response(200, _fixture("malformed-page-0.json")),
            _response(200, _fixture("malformed-page-3.json")),
        ]
    )
    events = _fetch(LeverAdapter(config=_config(), http_client=client, page_size=3))

    assert [event.external_id for event in events if isinstance(event, RawJobPosting)] == [
        "valid-1"
    ]
    assert events[-1] == ScanOutcome(
        ScanStatus.PARTIAL,
        incomplete_reason=(
            "page skip 0 item 1 was malformed; page skip 0 item 2 duplicated an external ID"
        ),
        diagnostics=ScanDiagnostics(http_requests=2, rejected_items=2),
    )
    _assert_one_terminal_outcome(events)


def test_duplicate_id_across_pages_is_partial_after_retaining_unique_evidence() -> None:
    client = _FakeHttpClient(
        [
            _response(200, [{"id": "one"}, {"id": "two"}]),
            _response(200, [{"id": "one"}]),
        ]
    )
    events = _fetch(LeverAdapter(config=_config(), http_client=client, page_size=2))

    assert [event.external_id for event in events if isinstance(event, RawJobPosting)] == [
        "one",
        "two",
    ]
    assert events[-1] == ScanOutcome(
        ScanStatus.PARTIAL,
        incomplete_reason="page skip 2 item 0 duplicated an external ID",
        diagnostics=ScanDiagnostics(http_requests=2, rejected_items=1),
    )


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            LeverHttpResponse(200, {}, None, json_error="response body was not valid UTF-8 JSON"),
            "Lever page at skip 0: response body was not valid UTF-8 JSON",
        ),
        (
            LeverHttpResponse(200, {}, {"not": "an array"}),
            "Lever page at skip 0 was not a JSON array",
        ),
    ],
)
def test_malformed_success_page_is_partial_not_complete(
    response: LeverHttpResponse, reason: str
) -> None:
    events = _fetch(LeverAdapter(config=_config(), http_client=_FakeHttpClient([response])))

    assert events == [
        ScanOutcome(ScanStatus.PARTIAL, incomplete_reason=reason, diagnostics=ScanDiagnostics(1))
    ]


def test_later_malformed_page_keeps_earlier_rejected_item_reasons() -> None:
    client = _FakeHttpClient(
        [
            _response(200, [{"id": "one"}, {"id": " "}]),
            LeverHttpResponse(200, {}, None, json_error="response body was not valid UTF-8 JSON"),
        ]
    )
    events = _fetch(LeverAdapter(config=_config(), http_client=client, page_size=2))

    assert events[-1] == ScanOutcome(
        ScanStatus.PARTIAL,
        incomplete_reason=(
            "page skip 0 item 1 was malformed; "
            "Lever page at skip 2: response body was not valid UTF-8 JSON"
        ),
        diagnostics=ScanDiagnostics(http_requests=2, rejected_items=1),
    )


def test_retry_after_and_page_telemetry_are_accumulated_across_pages() -> None:
    client = _FakeHttpClient(
        [
            _response(429, [], headers={"Retry-After": "3.5"}),
            _response(200, _fixture("global-page-0.json")),
            _response(200, _fixture("global-page-2.json")),
        ]
    )
    sleeps: list[float] = []
    adapter = LeverAdapter(
        config=_config(),
        http_client=client,
        page_size=2,
        sleep=lambda seconds: _record_sleep(sleeps, seconds),
        retry_jitter=lambda _attempt: 0.25,
    )

    assert _fetch(adapter)[-1] == ScanOutcome(
        ScanStatus.COMPLETE,
        diagnostics=ScanDiagnostics(http_requests=3, retry_count=1, rate_limit_events=1),
    )
    assert sleeps == [3.5]


def test_retry_after_http_date_is_honored_with_an_injected_utc_clock() -> None:
    client = _FakeHttpClient(
        [
            _response(
                429,
                [],
                headers={"Retry-After": "Thu, 20 Aug 2026 10:00:04 GMT"},
            ),
            _response(200, []),
        ]
    )
    sleeps: list[float] = []
    adapter = LeverAdapter(
        config=_config(),
        http_client=client,
        sleep=lambda seconds: _record_sleep(sleeps, seconds),
        retry_jitter=lambda _attempt: 0.25,
        utc_now=lambda: datetime(2026, 8, 20, 10, tzinfo=UTC),
    )

    assert _fetch(adapter)[-1] == ScanOutcome(
        ScanStatus.COMPLETE,
        diagnostics=ScanDiagnostics(http_requests=2, retry_count=1, rate_limit_events=1),
    )
    assert sleeps == [4.0]


@pytest.mark.parametrize("retry_after", ["not-a-date", "NaN", "inf", "-1"])
def test_invalid_retry_after_falls_back_to_jitter(retry_after: str) -> None:
    client = _FakeHttpClient(
        [_response(503, {}, headers={"Retry-After": retry_after}), _response(200, [])]
    )
    sleeps: list[float] = []
    adapter = LeverAdapter(
        config=_config(),
        http_client=client,
        sleep=lambda seconds: _record_sleep(sleeps, seconds),
        retry_jitter=lambda _attempt: 0.25,
    )

    assert _fetch(adapter)[-1] == ScanOutcome(
        ScanStatus.COMPLETE, diagnostics=ScanDiagnostics(http_requests=2, retry_count=1)
    )
    assert sleeps == [0.25]


@pytest.mark.parametrize(
    "first_response",
    [
        LeverHttpResponse(429, {"Retry-After": "1000"}, []),
        LeverTransportError("timed out"),
    ],
)
def test_retry_delays_are_capped(first_response: LeverHttpResponse | LeverTransportError) -> None:
    client = _FakeHttpClient([first_response, _response(200, [])])
    sleeps: list[float] = []
    adapter = LeverAdapter(
        config=_config(),
        http_client=client,
        sleep=lambda seconds: _record_sleep(sleeps, seconds),
        retry_jitter=lambda _attempt: 1000.0,
    )

    _fetch(adapter)

    assert sleeps == [10.0]


def test_non_retryable_http_error_is_not_retried_and_retains_prior_page_telemetry() -> None:
    full_page = [{"id": "one"}, {"id": "two"}]
    client = _FakeHttpClient([_response(200, full_page), _response(404, {})])
    adapter = LeverAdapter(config=_config(), http_client=client, page_size=2)

    with pytest.raises(ScanFailedError, match="non-retryable HTTP 404") as raised:
        _fetch(adapter)

    assert raised.value.diagnostics == ScanDiagnostics(http_requests=2)
    assert len(client.requests) == 2


def test_later_failure_retains_prior_rejected_item_telemetry() -> None:
    client = _FakeHttpClient(
        [
            _response(200, [{"id": "one"}, {"id": " "}]),
            _response(503, {}),
        ]
    )
    adapter = LeverAdapter(config=_config(), http_client=client, page_size=2, max_attempts=1)

    with pytest.raises(ScanFailedError) as raised:
        _fetch(adapter)

    assert raised.value.diagnostics == ScanDiagnostics(http_requests=2, rejected_items=1)


def test_transport_errors_retry_and_exhaustion_retains_telemetry() -> None:
    client = _FakeHttpClient([LeverTransportError("timed out"), LeverTransportError("timed out")])
    sleeps: list[float] = []
    adapter = LeverAdapter(
        config=_config(),
        http_client=client,
        max_attempts=2,
        sleep=lambda seconds: _record_sleep(sleeps, seconds),
        retry_jitter=lambda _attempt: 0.25,
    )

    with pytest.raises(ScanFailedError) as raised:
        _fetch(adapter)

    assert raised.value.diagnostics == ScanDiagnostics(http_requests=2, retry_count=1)
    assert sleeps == [0.25]


def test_transient_http_retry_exhaustion_retains_telemetry() -> None:
    client = _FakeHttpClient([_response(503, {}), _response(503, {})])
    sleeps: list[float] = []
    adapter = LeverAdapter(
        config=_config(),
        http_client=client,
        max_attempts=2,
        sleep=lambda seconds: _record_sleep(sleeps, seconds),
        retry_jitter=lambda _attempt: 0.25,
    )

    with pytest.raises(ScanFailedError, match="exhausted retries after HTTP 503") as raised:
        _fetch(adapter)

    assert raised.value.diagnostics == ScanDiagnostics(http_requests=2, retry_count=1)
    assert sleeps == [0.25]


def test_retry_scheduling_failure_retains_telemetry_after_an_attempt() -> None:
    adapter = LeverAdapter(
        config=_config(),
        http_client=_FakeHttpClient([_response(503, {})]),
        retry_jitter=lambda _attempt: -1.0,
    )

    with pytest.raises(ScanFailedError) as raised:
        _fetch(adapter)

    assert raised.value.diagnostics == ScanDiagnostics(http_requests=1, retry_count=1)


def test_unexpected_transport_failure_is_wrapped_with_observed_request_count() -> None:
    adapter = LeverAdapter(
        config=_config(),
        http_client=_FakeHttpClient([RuntimeError("socket implementation failed")]),
        max_attempts=1,
    )

    with pytest.raises(ScanFailedError) as raised:
        _fetch(adapter)

    assert raised.value.diagnostics == ScanDiagnostics(http_requests=1)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_attempts": 0}, "max_attempts must be at least one"),
        ({"page_size": 0}, "page_size must be between 1 and 100"),
        ({"page_size": 101}, "page_size must be between 1 and 100"),
        ({"max_pages": 0}, "max_pages must be between 1 and 1000"),
        ({"user_agent": " "}, "user_agent must be a non-blank string"),
    ],
)
def test_validation_rejects_invalid_runtime_seams(kwargs: dict[str, object], message: str) -> None:
    adapter = LeverAdapter(config=_config(), http_client=_FakeHttpClient([]), **kwargs)

    with pytest.raises(LeverAdapterError, match=message):
        asyncio.run(adapter.validate())


def test_site_is_url_encoded_and_adapter_implements_the_runtime_source_contract() -> None:
    client = _FakeHttpClient([_response(200, [])])
    adapter: SourceAdapter = LeverAdapter(
        config=LeverSourceConfig(
            key="lever-site",
            kind="lever",
            company_name="Lever Site",
            site="a site/with slash",
        ),
        http_client=client,
    )

    assert_type(adapter, SourceAdapter)
    assert isinstance(adapter, SourceAdapter)
    asyncio.run(adapter.validate())
    events = asyncio.run(_collect(adapter))
    assert isinstance(events[-1], ScanOutcome)
    assert "a%20site%2Fwith%20slash" in client.requests[0][0]


async def _collect(adapter: SourceAdapter) -> list[FetchEvent]:
    return [event async for event in adapter.fetch(_context())]


def _fetch(adapter: LeverAdapter) -> list[FetchEvent]:
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
) -> LeverHttpResponse:
    return LeverHttpResponse(status_code=status_code, headers=headers or {}, json_body=json_body)


def _fixture(name: str) -> list[object]:
    path = Path(__file__).resolve().parents[1] / "fixtures" / "ats" / "lever" / name
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, list)
    return parsed


def _config(*, region: str = "global") -> LeverSourceConfig:
    return LeverSourceConfig(
        key="example-lever",
        kind="lever",
        company_name="Example Lever",
        site="example-site",
        region=region,  # type: ignore[arg-type]
    )


def _context() -> FetchContext:
    return FetchContext(
        run_id="run-1", fetched_at=datetime(2026, 8, 20, 10, tzinfo=UTC), timeout_seconds=12.0
    )


def _assert_one_terminal_outcome(events: list[FetchEvent]) -> None:
    outcomes = [event for event in events if isinstance(event, ScanOutcome)]
    assert len(outcomes) == 1
    assert events[-1] is outcomes[0]
