"""Offline contract tests for Workable's credential-free public endpoint."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import assert_type

import pytest

from career_os.adapters.workable_public import (
    WorkablePublicAdapter,
    WorkablePublicAdapterError,
    WorkablePublicHttpResponse,
    WorkablePublicTransportError,
    _response_from_bytes,
)
from career_os.config.sources import WorkablePublicSourceConfig
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
    def __init__(self, responses: list[WorkablePublicHttpResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, Mapping[str, str], float]] = []

    async def get(
        self, *, url: str, headers: Mapping[str, str], timeout_seconds: float
    ) -> WorkablePublicHttpResponse:
        self.requests.append((url, headers, timeout_seconds))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_response_from_bytes_accepts_valid_json_and_rejects_invalid_json_constants() -> None:
    valid = _response_from_bytes(status_code=200, headers={}, body=b'{"jobs": []}')
    invalid = _response_from_bytes(status_code=200, headers={}, body=b"not json")
    invalid_utf8 = _response_from_bytes(status_code=200, headers={}, body=b"\xff")
    nan = _response_from_bytes(status_code=200, headers={}, body=b'{"jobs": [NaN]}')
    infinity = _response_from_bytes(status_code=200, headers={}, body=b'{"jobs": [Infinity]}')

    assert valid.json_body == {"jobs": []}
    assert valid.json_error is None
    assert invalid.json_error == "response body was not valid UTF-8 JSON"
    assert invalid_utf8.json_error == "response body was not valid UTF-8 JSON"
    assert nan.json_error == "response body was not valid UTF-8 JSON"
    assert infinity.json_error == "response body was not valid UTF-8 JSON"


def test_response_json_error_yields_one_partial_terminal_outcome() -> None:
    response = WorkablePublicHttpResponse(
        status_code=200,
        headers={},
        json_body=None,
        json_error="response body was not valid UTF-8 JSON",
    )
    events = _fetch(
        WorkablePublicAdapter(config=_config(), http_client=_FakeHttpClient([response]))
    )

    assert events == [
        ScanOutcome(
            ScanStatus.PARTIAL,
            incomplete_reason="response body was not valid UTF-8 JSON",
            diagnostics=ScanDiagnostics(http_requests=1),
        )
    ]
    _assert_one_terminal_outcome(events)


def test_populated_fixture_preserves_raw_evidence_and_exact_request_contract() -> None:
    body = _fixture("populated.json")
    client = _FakeHttpClient([_response(200, body)])
    events = _fetch(WorkablePublicAdapter(config=_config(), http_client=client))

    postings = [event for event in events if isinstance(event, RawJobPosting)]
    assert len(postings) == 1
    posting = postings[0]
    assert posting.external_id == "eng-1"
    assert posting.source_url == "https://jobs.example/eng-1"
    assert posting.apply_url == "https://apply.example/eng-1"
    assert posting.payload == body["jobs"][0]
    assert posting.payload["unknownFutureField"] == {"kept": True}
    assert posting.payload["description"] == "<p>Build systems.</p>"
    assert client.requests == [
        (
            "https://www.workable.com/api/accounts/example%20account?details=true",
            {"Accept": "application/json", "User-Agent": "career-os/0.1.0 WorkablePublicAdapter"},
            12.0,
        )
    ]
    assert "Authorization" not in client.requests[0][1]
    assert events[-1] == ScanOutcome(ScanStatus.COMPLETE, diagnostics=ScanDiagnostics(1))
    _assert_one_terminal_outcome(events)


def test_empty_jobs_list_is_complete_under_weaker_documented_assumption() -> None:
    events = _fetch(
        WorkablePublicAdapter(
            config=_config(),
            http_client=_FakeHttpClient([_response(200, _fixture("empty.json"))]),
        )
    )
    assert events == [ScanOutcome(ScanStatus.COMPLETE, diagnostics=ScanDiagnostics(1))]
    assert isinstance(events[-1], ScanOutcome)
    assert events[-1].permits_inactivation is True


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ([], "Workable public response root was not an object"),
        ({}, "Workable public response did not contain a jobs list"),
        ({"jobs": {}}, "Workable public response did not contain a jobs list"),
    ],
)
def test_malformed_root_or_jobs_shape_is_partial(body: object, reason: str) -> None:
    events = _fetch(
        WorkablePublicAdapter(config=_config(), http_client=_FakeHttpClient([_response(200, body)]))
    )
    assert events == [
        ScanOutcome(ScanStatus.PARTIAL, incomplete_reason=reason, diagnostics=ScanDiagnostics(1))
    ]
    _assert_one_terminal_outcome(events)


def test_malformed_items_and_duplicate_shortcodes_are_partial_after_valid_evidence() -> None:
    events = _fetch(
        WorkablePublicAdapter(
            config=_config(),
            http_client=_FakeHttpClient([_response(200, _fixture("malformed.json"))]),
        )
    )
    assert [event.external_id for event in events if isinstance(event, RawJobPosting)] == ["valid"]
    assert events[-1] == ScanOutcome(
        ScanStatus.PARTIAL,
        incomplete_reason="job item 1 was malformed; job item 2 duplicated a shortcode",
        diagnostics=ScanDiagnostics(http_requests=1, rejected_items=2),
    )
    _assert_one_terminal_outcome(events)


def test_url_semantics_prefer_application_url_then_shortlink_and_use_url_for_apply() -> None:
    body = {
        "jobs": [
            {
                "shortcode": "preferred",
                "application_url": "https://jobs.example/preferred",
                "shortlink": "https://short.example/preferred",
                "url": "https://apply.example/preferred",
            },
            {
                "shortcode": "fallback",
                "shortlink": "https://short.example/fallback",
                "url": "https://apply.example/fallback",
            },
        ]
    }
    events = _fetch(
        WorkablePublicAdapter(config=_config(), http_client=_FakeHttpClient([_response(200, body)]))
    )
    postings = [event for event in events if isinstance(event, RawJobPosting)]
    assert [(posting.source_url, posting.apply_url) for posting in postings] == [
        ("https://jobs.example/preferred", "https://apply.example/preferred"),
        ("https://short.example/fallback", "https://apply.example/fallback"),
    ]


def test_retryable_status_then_success_retains_cumulative_diagnostics() -> None:
    client = _FakeHttpClient(
        [
            _response(429, {}, headers={"Retry-After": "3"}),
            _response(200, _fixture("empty.json")),
        ]
    )
    delays: list[float] = []
    adapter = WorkablePublicAdapter(
        config=_config(),
        http_client=client,
        sleep=lambda delay: _record_sleep(delays, delay),
        retry_jitter=lambda _attempt: 0.25,
    )
    assert _fetch(adapter)[-1] == ScanOutcome(
        ScanStatus.COMPLETE,
        diagnostics=ScanDiagnostics(http_requests=2, retry_count=1, rate_limit_events=1),
    )
    assert delays == [3.0]


def test_nonretryable_status_does_not_retry() -> None:
    client = _FakeHttpClient([_response(404, {})])
    with pytest.raises(ScanFailedError, match="non-retryable HTTP 404") as raised:
        _fetch(WorkablePublicAdapter(config=_config(), http_client=client))
    assert raised.value.diagnostics == ScanDiagnostics(http_requests=1)
    assert len(client.requests) == 1


def test_retry_exhaustion_transport_exhaustion_and_unexpected_transport_wrapping() -> None:
    exhausted = WorkablePublicAdapter(
        config=_config(),
        http_client=_FakeHttpClient([_response(503, {}), _response(503, {})]),
        max_attempts=2,
        sleep=lambda delay: _record_sleep([], delay),
        retry_jitter=lambda _attempt: 0.25,
    )
    with pytest.raises(ScanFailedError, match="exhausted retries after HTTP 503") as http_error:
        _fetch(exhausted)
    assert http_error.value.diagnostics == ScanDiagnostics(http_requests=2, retry_count=1)

    transport = WorkablePublicAdapter(
        config=_config(),
        http_client=_FakeHttpClient(
            [WorkablePublicTransportError("timeout"), WorkablePublicTransportError("timeout")]
        ),
        max_attempts=2,
        sleep=lambda delay: _record_sleep([], delay),
        retry_jitter=lambda _attempt: 0.25,
    )
    with pytest.raises(ScanFailedError) as transport_error:
        _fetch(transport)
    assert transport_error.value.diagnostics == ScanDiagnostics(http_requests=2, retry_count=1)

    unexpected = WorkablePublicAdapter(
        config=_config(),
        http_client=_FakeHttpClient([RuntimeError("socket implementation failed")]),
        max_attempts=1,
    )
    with pytest.raises(WorkablePublicTransportError, match="transport failure") as unexpected_error:
        _fetch(unexpected)
    assert unexpected_error.value.diagnostics == ScanDiagnostics(http_requests=1)


def test_transport_failure_retries_then_returns_success() -> None:
    delays: list[float] = []
    adapter = WorkablePublicAdapter(
        config=_config(),
        http_client=_FakeHttpClient(
            [WorkablePublicTransportError("timeout"), _response(200, _fixture("empty.json"))]
        ),
        sleep=lambda delay: _record_sleep(delays, delay),
        retry_jitter=lambda _attempt: 0.5,
    )
    assert _fetch(adapter)[-1] == ScanOutcome(
        ScanStatus.COMPLETE,
        diagnostics=ScanDiagnostics(http_requests=2, retry_count=1),
    )
    assert delays == [0.5]


def test_retry_after_date_invalid_past_nonfinite_and_cap_follow_shared_policy() -> None:
    dated_delays: list[float] = []
    dated = WorkablePublicAdapter(
        config=_config(),
        http_client=_FakeHttpClient(
            [
                _response(429, {}, headers={"Retry-After": "Thu, 20 Aug 2026 10:00:04 GMT"}),
                _response(200, _fixture("empty.json")),
            ]
        ),
        sleep=lambda delay: _record_sleep(dated_delays, delay),
        retry_jitter=lambda _attempt: 0.25,
        utc_now=lambda: datetime(2026, 8, 20, 10, tzinfo=UTC),
    )
    _fetch(dated)
    assert dated_delays == [4.0]

    capped_delays: list[float] = []
    capped = WorkablePublicAdapter(
        config=_config(),
        http_client=_FakeHttpClient(
            [
                _response(429, {}, headers={"Retry-After": "100"}),
                _response(200, _fixture("empty.json")),
            ]
        ),
        sleep=lambda delay: _record_sleep(capped_delays, delay),
        retry_jitter=lambda _attempt: 0.25,
    )
    _fetch(capped)
    assert capped_delays == [10.0]

    for retry_after in ("invalid", "Wed, 19 Aug 2026 10:00:00 GMT", "NaN", "inf"):
        fallback_delays: list[float] = []
        fallback = WorkablePublicAdapter(
            config=_config(),
            http_client=_FakeHttpClient(
                [
                    _response(503, {}, headers={"Retry-After": retry_after}),
                    _response(200, _fixture("empty.json")),
                ]
            ),
            sleep=lambda delay, recorded=fallback_delays: _record_sleep(recorded, delay),
            retry_jitter=lambda _attempt: 1000.0,
        )
        _fetch(fallback)
        assert fallback_delays == [10.0]


def test_retry_scheduling_failure_retains_diagnostics() -> None:
    adapter = WorkablePublicAdapter(
        config=_config(),
        http_client=_FakeHttpClient([_response(503, {})]),
        retry_jitter=lambda _attempt: -1.0,
    )
    with pytest.raises(WorkablePublicAdapterError, match="retry scheduling failed") as raised:
        _fetch(adapter)
    assert raised.value.diagnostics == ScanDiagnostics(http_requests=1, retry_count=1)

    sleep_failure = WorkablePublicAdapter(
        config=_config(),
        http_client=_FakeHttpClient([_response(503, {})]),
        sleep=_failing_sleep,
        retry_jitter=lambda _attempt: 0.25,
    )
    with pytest.raises(WorkablePublicAdapterError, match="retry scheduling failed") as sleep_error:
        _fetch(sleep_failure)
    assert sleep_error.value.diagnostics == ScanDiagnostics(http_requests=1, retry_count=1)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_attempts": True}, "max_attempts must be an integer"),
        ({"max_attempts": "3"}, "max_attempts must be an integer"),
        ({"max_attempts": 0}, "max_attempts must be at least one"),
        ({"user_agent": " "}, "user_agent"),
    ],
)
def test_validation_and_runtime_source_adapter_protocol(
    kwargs: dict[str, object], message: str
) -> None:
    adapter = WorkablePublicAdapter(config=_config(), http_client=_FakeHttpClient([]), **kwargs)
    with pytest.raises(WorkablePublicAdapterError, match=message):
        asyncio.run(adapter.validate())
    valid: SourceAdapter = WorkablePublicAdapter(
        config=_config(), http_client=_FakeHttpClient([_response(200, _fixture("empty.json"))])
    )
    assert_type(valid, SourceAdapter)
    assert isinstance(valid, SourceAdapter)
    asyncio.run(valid.validate())


def _fetch(adapter: WorkablePublicAdapter) -> list[FetchEvent]:
    async def collect() -> list[FetchEvent]:
        await adapter.validate()
        return [event async for event in adapter.fetch(_context())]

    return asyncio.run(collect())


def _record_sleep(recorded: list[float], delay: float):
    recorded.append(delay)

    async def done() -> None:
        return None

    return done()


async def _failing_sleep(_delay: float) -> None:
    raise RuntimeError("event loop was closed")


def _response(
    status: int, body: object, *, headers: Mapping[str, str] | None = None
) -> WorkablePublicHttpResponse:
    return WorkablePublicHttpResponse(status_code=status, headers=headers or {}, json_body=body)


def _fixture(name: str) -> dict[str, object]:
    path = Path(__file__).resolve().parents[1] / "fixtures" / "ats" / "workable_public" / name
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _config() -> WorkablePublicSourceConfig:
    return WorkablePublicSourceConfig(
        key="example-workable",
        kind="workable_public",
        company_name="Example Workable",
        account_subdomain="example account",
    )


def _context() -> FetchContext:
    return FetchContext("run-1", datetime(2026, 8, 20, 10, tzinfo=UTC), 12.0)


def _assert_one_terminal_outcome(events: list[FetchEvent]) -> None:
    outcomes = [event for event in events if isinstance(event, ScanOutcome)]
    assert len(outcomes) == 1
    assert events[-1] is outcomes[0]
