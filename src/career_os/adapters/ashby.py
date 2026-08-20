"""Ashby public Job Board API adapter.

The official endpoint is ``GET https://api.ashbyhq.com/posting-api/job-board/
{JOB_BOARD_NAME}?includeCompensation={true|false}``.  It documents one response
containing all currently published postings, so this adapter deliberately makes
one request and never invents pagination.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Any, ClassVar, Final, NoReturn, Protocol, cast
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from career_os.config.sources import AshbySourceConfig
from career_os.domain import (
    FetchContext,
    FetchEvent,
    RawJobPosting,
    ScanDiagnostics,
    ScanFailedError,
    ScanOutcome,
    ScanStatus,
)

_BASE_URL: Final = "https://api.ashbyhq.com/posting-api/job-board"
_DEFAULT_USER_AGENT: Final = "career-os/0.1.0 AshbyJobBoardAdapter"
_TRANSIENT_STATUS_CODES: Final = frozenset({408, 429, 500, 502, 503, 504})
_MAX_RETRY_DELAY_SECONDS: Final = 10.0
_SUPPORTED_API_VERSION: Final = "1"

type AsyncSleep = Callable[[float], Awaitable[None]]
type RetryJitter = Callable[[int], float]
type UtcClock = Callable[[], datetime]


class AshbyAdapterError(ScanFailedError):
    """Base error for an unusable Ashby request or runtime configuration."""


class AshbyTransportError(AshbyAdapterError):
    """A network-level failure prevented a usable HTTP response."""


class AshbyHttpError(AshbyAdapterError):
    """A non-successful or exhausted HTTP response prevented collection."""


@dataclass(frozen=True, slots=True)
class AshbyHttpResponse:
    """Minimal injectable HTTP response shape for deterministic adapter tests."""

    status_code: int
    headers: Mapping[str, str]
    json_body: object
    json_error: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not isinstance(self.status_code, int):
            raise TypeError("status_code must be an integer")
        object.__setattr__(self, "headers", dict(self.headers))


class AshbyHttpClient(Protocol):
    async def get(
        self, *, url: str, headers: Mapping[str, str], timeout_seconds: float
    ) -> AshbyHttpResponse:
        """Return an HTTP response or raise :class:`AshbyTransportError`."""


@dataclass(slots=True)
class UrllibAshbyHttpClient:
    """Stdlib HTTP client, offloaded so callers retain an async boundary."""

    async def get(
        self, *, url: str, headers: Mapping[str, str], timeout_seconds: float
    ) -> AshbyHttpResponse:
        try:
            return await asyncio.to_thread(
                _blocking_get, url=url, headers=headers, timeout_seconds=timeout_seconds
            )
        except (OSError, TimeoutError) as error:
            raise AshbyTransportError("Ashby request transport failure") from error


def _blocking_get(
    *, url: str, headers: Mapping[str, str], timeout_seconds: float
) -> AshbyHttpResponse:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return _response_from_bytes(
                status_code=response.status,
                headers=dict(response.headers.items()),
                body=response.read(),
            )
    except HTTPError as error:
        return _response_from_bytes(
            status_code=error.code,
            headers=dict(error.headers.items()) if error.headers is not None else {},
            body=error.read(),
        )


def _response_from_bytes(
    *, status_code: int, headers: Mapping[str, str], body: bytes
) -> AshbyHttpResponse:
    try:
        decoded: object = json.loads(
            body.decode("utf-8"), parse_constant=_reject_nonstandard_json_constant
        )
    except (UnicodeDecodeError, ValueError):
        return AshbyHttpResponse(
            status_code=status_code,
            headers=headers,
            json_body=None,
            json_error="response body was not valid UTF-8 JSON",
        )
    return AshbyHttpResponse(status_code=status_code, headers=headers, json_body=decoded)


def _reject_nonstandard_json_constant(constant: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant: {constant}")


def _default_jitter(attempt: int) -> float:
    return random.uniform(0.0, min(1.0 * 2**attempt, _MAX_RETRY_DELAY_SECONDS))


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class AshbyAdapter:
    """Stream one Ashby board and an unambiguous terminal completeness outcome."""

    config: AshbySourceConfig
    http_client: AshbyHttpClient = field(default_factory=UrllibAshbyHttpClient)
    sleep: AsyncSleep = asyncio.sleep
    retry_jitter: RetryJitter = _default_jitter
    utc_now: UtcClock = _utc_now
    max_attempts: int = 3
    user_agent: str = _DEFAULT_USER_AGENT

    kind: ClassVar[str] = "ashby"

    async def validate(self) -> None:
        if self.config.kind != self.kind:
            raise AshbyAdapterError("Ashby adapter requires ashby configuration")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise AshbyAdapterError("max_attempts must be an integer")
        if self.max_attempts < 1:
            raise AshbyAdapterError("max_attempts must be at least one")
        if not isinstance(self.user_agent, str) or not self.user_agent.strip():
            raise AshbyAdapterError("user_agent must be a non-blank string")

    async def fetch(self, context: FetchContext) -> AsyncIterator[FetchEvent]:
        response, diagnostics = await self._request(context)
        if response.json_error is not None:
            yield ScanOutcome(
                ScanStatus.PARTIAL,
                incomplete_reason=response.json_error,
                diagnostics=diagnostics,
            )
            return
        if not isinstance(response.json_body, Mapping):
            yield ScanOutcome(
                ScanStatus.PARTIAL,
                incomplete_reason="Ashby response root was not an object",
                diagnostics=diagnostics,
            )
            return
        version = response.json_body.get("apiVersion")
        if version != _SUPPORTED_API_VERSION:
            yield ScanOutcome(
                ScanStatus.PARTIAL,
                incomplete_reason="Ashby response apiVersion was not supported version 1",
                diagnostics=diagnostics,
            )
            return
        jobs = response.json_body.get("jobs")
        if not isinstance(jobs, list):
            yield ScanOutcome(
                ScanStatus.PARTIAL,
                incomplete_reason="Ashby response did not contain a jobs list",
                diagnostics=diagnostics,
            )
            return

        seen_external_ids: set[str] = set()
        reasons: list[str] = []
        rejected_items = 0
        for index, item in enumerate(jobs):
            if not isinstance(item, Mapping):
                reasons.append(f"job item {index} was malformed")
                rejected_items += 1
                continue
            listed = _listing_state(item)
            if listed is None:
                reasons.append(f"job item {index} had an ambiguous isListed flag")
                rejected_items += 1
                continue
            if not listed:
                continue
            posting = _posting_from_item(
                config=self.config, item=item, fetched_at=context.fetched_at
            )
            if posting is None:
                reasons.append(f"job item {index} was malformed")
                rejected_items += 1
                continue
            if posting.external_id in seen_external_ids:
                reasons.append(f"job item {index} duplicated a jobUrl")
                rejected_items += 1
                continue
            seen_external_ids.add(posting.external_id)
            yield posting

        outcome_diagnostics = ScanDiagnostics(
            http_requests=diagnostics.http_requests,
            retry_count=diagnostics.retry_count,
            rate_limit_events=diagnostics.rate_limit_events,
            rejected_items=rejected_items,
        )
        if reasons:
            yield ScanOutcome(
                ScanStatus.PARTIAL,
                incomplete_reason="; ".join(reasons),
                diagnostics=outcome_diagnostics,
            )
        else:
            yield ScanOutcome(ScanStatus.COMPLETE, diagnostics=outcome_diagnostics)

    async def _request(self, context: FetchContext) -> tuple[AshbyHttpResponse, ScanDiagnostics]:
        compensation = "true" if self.config.include_compensation else "false"
        url = (
            f"{_BASE_URL}/{quote(self.config.board_name, safe='')}"
            f"?includeCompensation={compensation}"
        )
        headers = {"Accept": "application/json", "User-Agent": self.user_agent.strip()}
        http_requests = retry_count = rate_limit_events = 0
        for attempt in range(self.max_attempts):
            try:
                http_requests += 1
                response = await self.http_client.get(
                    url=url, headers=headers, timeout_seconds=context.timeout_seconds
                )
            except Exception as error:
                diagnostics = ScanDiagnostics(
                    http_requests=http_requests,
                    retry_count=retry_count,
                    rate_limit_events=rate_limit_events,
                )
                transport_error = (
                    error
                    if isinstance(error, AshbyTransportError)
                    else AshbyTransportError(
                        "Ashby request transport failure", diagnostics=diagnostics
                    )
                )
                if attempt + 1 == self.max_attempts:
                    raise transport_error.with_diagnostics(diagnostics) from error
                retry_count += 1
                await self._retry_or_raise(
                    attempt,
                    retry_after=None,
                    diagnostics=ScanDiagnostics(
                        http_requests=http_requests,
                        retry_count=retry_count,
                        rate_limit_events=rate_limit_events,
                    ),
                )
                continue
            if 200 <= response.status_code < 300:
                return response, ScanDiagnostics(
                    http_requests=http_requests,
                    retry_count=retry_count,
                    rate_limit_events=rate_limit_events,
                )
            if response.status_code == 429:
                rate_limit_events += 1
            diagnostics = ScanDiagnostics(
                http_requests=http_requests,
                retry_count=retry_count,
                rate_limit_events=rate_limit_events,
            )
            if response.status_code not in _TRANSIENT_STATUS_CODES:
                raise AshbyHttpError(
                    f"Ashby request failed with non-retryable HTTP {response.status_code}",
                    diagnostics=diagnostics,
                )
            if attempt + 1 == self.max_attempts:
                raise AshbyHttpError(
                    f"Ashby request exhausted retries after HTTP {response.status_code}",
                    diagnostics=diagnostics,
                )
            retry_count += 1
            await self._retry_or_raise(
                attempt,
                retry_after=_retry_after_seconds(response.headers, utc_now=self.utc_now),
                diagnostics=ScanDiagnostics(
                    http_requests=http_requests,
                    retry_count=retry_count,
                    rate_limit_events=rate_limit_events,
                ),
            )
        raise AssertionError("retry loop must return a response or raise")

    async def _retry_or_raise(
        self, attempt: int, *, retry_after: float | None, diagnostics: ScanDiagnostics
    ) -> None:
        try:
            await self._sleep_before_retry(attempt, retry_after=retry_after)
        except Exception as error:
            raise AshbyAdapterError(
                "Ashby retry scheduling failed", diagnostics=diagnostics
            ) from error

    async def _sleep_before_retry(self, attempt: int, *, retry_after: float | None) -> None:
        delay = retry_after if retry_after is not None else self.retry_jitter(attempt)
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not isfinite(delay)
            or delay < 0
        ):
            raise AshbyAdapterError("retry delay must be a non-negative number")
        await self.sleep(min(float(delay), _MAX_RETRY_DELAY_SECONDS))


def _retry_after_seconds(headers: Mapping[str, str], *, utc_now: UtcClock) -> float | None:
    for name, value in headers.items():
        if name.lower() != "retry-after":
            continue
        try:
            seconds = float(value)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                now = utc_now()
            except (TypeError, ValueError):
                return None
            if (
                now.tzinfo is None
                or now.utcoffset() is None
                or retry_at.tzinfo is None
                or retry_at.utcoffset() is None
            ):
                return None
            seconds = (retry_at - now).total_seconds()
        if not isfinite(seconds) or seconds < 0:
            return None
        return min(seconds, _MAX_RETRY_DELAY_SECONDS)
    return None


def _listing_state(item: object) -> bool | None:
    if not isinstance(item, Mapping):
        return None
    listed = item.get("isListed")
    return listed if isinstance(listed, bool) else None


def _posting_from_item(
    *, config: AshbySourceConfig, item: object, fetched_at: datetime
) -> RawJobPosting | None:
    if not isinstance(item, Mapping):
        return None
    job_url = item.get("jobUrl")
    if not isinstance(job_url, str) or not job_url.strip():
        return None
    return RawJobPosting(
        source_key=config.key,
        source_kind="ashby",
        external_id=job_url,
        source_url=job_url,
        apply_url=_nonblank_string(item.get("applyUrl")),
        payload=cast(Mapping[str, Any], item),
        fetched_at=fetched_at,
    )


def _nonblank_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
