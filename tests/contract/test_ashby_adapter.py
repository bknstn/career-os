"""Offline contract tests for Ashby's documented public Job Board endpoint."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import assert_type

import pytest

from career_os.adapters.ashby import (
    AshbyAdapter,
    AshbyAdapterError,
    AshbyHttpResponse,
    AshbyTransportError,
    _response_from_bytes,
)
from career_os.config.sources import AshbySourceConfig
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
    def __init__(self, responses: list[AshbyHttpResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, Mapping[str, str], float]] = []

    async def get(
        self, *, url: str, headers: Mapping[str, str], timeout_seconds: float
    ) -> AshbyHttpResponse:
        self.requests.append((url, headers, timeout_seconds))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_populated_documented_fixture_preserves_compensation_and_known_raw_evidence() -> None:
    body = _fixture("populated.json")
    client = _FakeHttpClient([_response(200, body)])
    events = _fetch(AshbyAdapter(config=_config(), http_client=client))

    postings = [event for event in events if isinstance(event, RawJobPosting)]
    assert len(postings) == 1
    assert postings[0].external_id == "https://jobs.ashbyhq.com/example/senior-data-engineer"
    assert postings[0].source_url == postings[0].external_id
    assert (
        postings[0].apply_url == "https://jobs.ashbyhq.com/example/senior-data-engineer/application"
    )
    assert postings[0].payload == body["jobs"][0]
    compensation = postings[0].payload["compensation"]
    assert compensation["compensationTierSummary"] == "Poland senior engineering compensation"
    assert compensation["compensationTiers"][0]["components"][0] == {
        "id": "component-salary",
        "summary": "PLN 300,000 - 420,000 annually",
        "compensationType": "Salary",
        "currencyCode": "PLN",
        "minValue": 300000,
        "maxValue": 420000,
        "interval": "1 YEAR",
    }
    assert compensation["summaryComponents"][1] == {
        "compensationType": "EquityPercentage",
        "interval": "NONE",
        "currencyCode": None,
        "minValue": 0.01,
        "maxValue": 0.05,
    }
    assert postings[0].payload["secondaryLocations"][0]["address"] == {
        "addressCountry": "PL",
        "addressLocality": "Warsaw",
        "addressRegion": "Mazowieckie",
    }
    assert postings[0].payload["unknownFutureField"] == {"preserved": ["without", "normalization"]}
    assert events[-1] == ScanOutcome(ScanStatus.COMPLETE, diagnostics=ScanDiagnostics(1))
    assert client.requests == [
        (
            "https://api.ashbyhq.com/posting-api/job-board/example%20board?includeCompensation=true",
            {"Accept": "application/json", "User-Agent": "career-os/0.1.0 AshbyJobBoardAdapter"},
            12.0,
        )
    ]
    _assert_one_outcome(events)


def test_compensation_flag_false_is_lowercase_and_missing_compensation_is_accepted() -> None:
    body = {
        "apiVersion": "1",
        "jobs": [
            {
                "isListed": True,
                "jobUrl": "https://jobs.ashbyhq.com/example/no-compensation",
            }
        ],
    }
    client = _FakeHttpClient([_response(200, body)])
    events = _fetch(AshbyAdapter(config=_config(include_compensation=False), http_client=client))

    assert client.requests[0][0].endswith("?includeCompensation=false")
    assert events[-1].status is ScanStatus.COMPLETE  # type: ignore[union-attr]


def test_empty_and_all_unlisted_boards_are_complete_empty_scans() -> None:
    for fixture_name in ("empty.json", "all-unlisted.json"):
        events = _fetch(
            AshbyAdapter(
                config=_config(),
                http_client=_FakeHttpClient([_response(200, _fixture(fixture_name))]),
            )
        )
        assert events == [ScanOutcome(ScanStatus.COMPLETE, diagnostics=ScanDiagnostics(1))]
        assert isinstance(events[-1], ScanOutcome)
        assert events[-1].permits_inactivation is True


def test_malformed_ambiguous_and_duplicate_items_are_partial_after_valid_evidence() -> None:
    events = _fetch(
        AshbyAdapter(
            config=_config(),
            http_client=_FakeHttpClient([_response(200, _fixture("malformed.json"))]),
        )
    )

    assert [event.external_id for event in events if isinstance(event, RawJobPosting)] == [
        "https://jobs.ashbyhq.com/example/valid"
    ]
    assert events[-1] == ScanOutcome(
        ScanStatus.PARTIAL,
        incomplete_reason=(
            "job item 1 was malformed; job item 2 had an ambiguous isListed flag; "
            "job item 3 duplicated a jobUrl"
        ),
        diagnostics=ScanDiagnostics(http_requests=1, rejected_items=3),
    )


def test_missing_is_listed_is_ambiguous_and_never_silently_included() -> None:
    body = {
        "apiVersion": "1",
        "jobs": [{"jobUrl": "https://jobs.ashbyhq.com/example/missing-listed-flag"}],
    }
    events = _fetch(
        AshbyAdapter(config=_config(), http_client=_FakeHttpClient([_response(200, body)]))
    )

    assert events == [
        ScanOutcome(
            ScanStatus.PARTIAL,
            incomplete_reason="job item 0 had an ambiguous isListed flag",
            diagnostics=ScanDiagnostics(http_requests=1, rejected_items=1),
        )
    ]


def test_non_object_job_is_malformed_while_valid_listed_evidence_is_retained() -> None:
    body = {
        "apiVersion": "1",
        "jobs": [
            "not a job object",
            {"isListed": True, "jobUrl": "https://jobs.ashbyhq.com/example/valid"},
        ],
    }
    events = _fetch(
        AshbyAdapter(config=_config(), http_client=_FakeHttpClient([_response(200, body)]))
    )

    assert [event.external_id for event in events if isinstance(event, RawJobPosting)] == [
        "https://jobs.ashbyhq.com/example/valid"
    ]
    assert events[-1] == ScanOutcome(
        ScanStatus.PARTIAL,
        incomplete_reason="job item 0 was malformed",
        diagnostics=ScanDiagnostics(http_requests=1, rejected_items=1),
    )


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ([], "Ashby response root was not an object"),
        ({"jobs": []}, "Ashby response apiVersion was not supported version 1"),
        ({"apiVersion": "2", "jobs": []}, "Ashby response apiVersion was not supported version 1"),
        ({"apiVersion": "1", "jobs": {}}, "Ashby response did not contain a jobs list"),
    ],
)
def test_invalid_root_version_or_jobs_shape_is_partial(body: object, reason: str) -> None:
    events = _fetch(
        AshbyAdapter(config=_config(), http_client=_FakeHttpClient([_response(200, body)]))
    )
    assert events == [
        ScanOutcome(ScanStatus.PARTIAL, incomplete_reason=reason, diagnostics=ScanDiagnostics(1))
    ]


def test_invalid_json_is_partial_and_real_parser_rejects_nonstandard_constants() -> None:
    parsed = _response_from_bytes(status_code=200, headers={}, body=b'{"apiVersion": NaN}')
    assert parsed.json_error == "response body was not valid UTF-8 JSON"
    events = _fetch(AshbyAdapter(config=_config(), http_client=_FakeHttpClient([parsed])))
    assert events[0].status is ScanStatus.PARTIAL  # type: ignore[union-attr]


def test_retry_after_and_transient_telemetry_are_retained() -> None:
    client = _FakeHttpClient(
        [_response(429, {}, headers={"Retry-After": "3"}), _response(200, _fixture("empty.json"))]
    )
    sleeps: list[float] = []
    adapter = AshbyAdapter(
        config=_config(),
        http_client=client,
        sleep=lambda delay: _record_sleep(sleeps, delay),
        retry_jitter=lambda _attempt: 0.25,
    )
    assert _fetch(adapter)[-1] == ScanOutcome(
        ScanStatus.COMPLETE,
        diagnostics=ScanDiagnostics(http_requests=2, retry_count=1, rate_limit_events=1),
    )
    assert sleeps == [3.0]


def test_retry_after_http_date_invalid_values_and_capping_follow_shared_policy() -> None:
    dated_client = _FakeHttpClient(
        [
            _response(429, {}, headers={"Retry-After": "Thu, 20 Aug 2026 10:00:04 GMT"}),
            _response(200, _fixture("empty.json")),
        ]
    )
    dated_sleeps: list[float] = []
    dated = AshbyAdapter(
        config=_config(),
        http_client=dated_client,
        sleep=lambda delay: _record_sleep(dated_sleeps, delay),
        retry_jitter=lambda _attempt: 0.25,
        utc_now=lambda: datetime(2026, 8, 20, 10, tzinfo=UTC),
    )
    _fetch(dated)
    assert dated_sleeps == [4.0]

    fallback_client = _FakeHttpClient(
        [
            _response(503, {}, headers={"Retry-After": "not-a-date"}),
            _response(200, _fixture("empty.json")),
        ]
    )
    fallback_sleeps: list[float] = []
    fallback = AshbyAdapter(
        config=_config(),
        http_client=fallback_client,
        sleep=lambda delay: _record_sleep(fallback_sleeps, delay),
        retry_jitter=lambda _attempt: 1000.0,
    )
    _fetch(fallback)
    assert fallback_sleeps == [10.0]


def test_retry_scheduling_and_unexpected_transport_failures_retain_diagnostics() -> None:
    scheduling = AshbyAdapter(
        config=_config(),
        http_client=_FakeHttpClient([_response(503, {})]),
        retry_jitter=lambda _attempt: -1.0,
    )
    with pytest.raises(ScanFailedError) as scheduling_error:
        _fetch(scheduling)
    assert scheduling_error.value.diagnostics == ScanDiagnostics(http_requests=1, retry_count=1)

    unexpected = AshbyAdapter(
        config=_config(),
        http_client=_FakeHttpClient([RuntimeError("socket implementation failed")]),
        max_attempts=1,
    )
    with pytest.raises(ScanFailedError) as transport_error:
        _fetch(unexpected)
    assert transport_error.value.diagnostics == ScanDiagnostics(http_requests=1)


def test_permanent_and_transport_errors_follow_bounded_failure_contract() -> None:
    permanent = AshbyAdapter(config=_config(), http_client=_FakeHttpClient([_response(404, {})]))
    with pytest.raises(ScanFailedError, match="non-retryable HTTP 404"):
        _fetch(permanent)
    transport = AshbyAdapter(
        config=_config(),
        http_client=_FakeHttpClient(
            [AshbyTransportError("timeout"), AshbyTransportError("timeout")]
        ),
        max_attempts=2,
        sleep=lambda _delay: _record_sleep([], 0.0),
        retry_jitter=lambda _attempt: 0.25,
    )
    with pytest.raises(ScanFailedError) as raised:
        _fetch(transport)
    assert raised.value.diagnostics == ScanDiagnostics(http_requests=2, retry_count=1)


def test_transient_http_retry_exhaustion_retains_telemetry() -> None:
    sleeps: list[float] = []
    adapter = AshbyAdapter(
        config=_config(),
        http_client=_FakeHttpClient([_response(503, {}), _response(503, {})]),
        max_attempts=2,
        sleep=lambda delay: _record_sleep(sleeps, delay),
        retry_jitter=lambda _attempt: 0.25,
    )
    with pytest.raises(ScanFailedError, match="exhausted retries after HTTP 503") as raised:
        _fetch(adapter)
    assert raised.value.diagnostics == ScanDiagnostics(http_requests=2, retry_count=1)
    assert sleeps == [0.25]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_attempts": 0}, "max_attempts must be at least one"),
        ({"user_agent": " "}, "user_agent"),
    ],
)
def test_validation_and_runtime_protocol(kwargs: dict[str, object], message: str) -> None:
    adapter = AshbyAdapter(config=_config(), http_client=_FakeHttpClient([]), **kwargs)
    with pytest.raises(AshbyAdapterError, match=message):
        asyncio.run(adapter.validate())
    valid: SourceAdapter = AshbyAdapter(
        config=_config(), http_client=_FakeHttpClient([_response(200, _fixture("empty.json"))])
    )
    assert_type(valid, SourceAdapter)
    assert isinstance(valid, SourceAdapter)
    asyncio.run(valid.validate())


def _fetch(adapter: AshbyAdapter) -> list[FetchEvent]:
    async def collect() -> list[FetchEvent]:
        await adapter.validate()
        return [event async for event in adapter.fetch(_context())]

    return asyncio.run(collect())


def _record_sleep(recorded: list[float], delay: float):
    recorded.append(delay)

    async def done() -> None:
        return None

    return done()


def _response(
    status: int, body: object, *, headers: Mapping[str, str] | None = None
) -> AshbyHttpResponse:
    return AshbyHttpResponse(status, headers or {}, body)


def _fixture(name: str) -> dict[str, object]:
    path = Path(__file__).resolve().parents[1] / "fixtures" / "ats" / "ashby" / name
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _config(*, include_compensation: bool = True) -> AshbySourceConfig:
    return AshbySourceConfig(
        key="example-ashby",
        kind="ashby",
        company_name="Example Ashby",
        board_name="example board",
        include_compensation=include_compensation,
    )


def _context() -> FetchContext:
    return FetchContext("run-1", datetime(2026, 8, 20, 10, tzinfo=UTC), 12.0)


def _assert_one_outcome(events: list[FetchEvent]) -> None:
    outcomes = [event for event in events if isinstance(event, ScanOutcome)]
    assert len(outcomes) == 1
    assert events[-1] is outcomes[0]
