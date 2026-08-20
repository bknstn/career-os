"""Credential-free Workable public endpoint with deliberately weak completeness proof.

This uses only ``https://www.workable.com/api/accounts/{subdomain}?details=true``;
it never uses Workable's authenticated SPI. Workable documents this alternate
endpoint at https://help.workable.com/hc/en-us/articles/115012771647-Using-the-
Workable-API-to-create-a-careers-page. The public response has no version, total,
pagination, or completeness marker, so a structurally valid jobs list is the
current (weaker) complete-scan assumption.
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

from career_os.config.sources import WorkablePublicSourceConfig
from career_os.domain import (
    FetchContext,
    FetchEvent,
    RawJobPosting,
    ScanDiagnostics,
    ScanFailedError,
    ScanOutcome,
    ScanStatus,
)

_BASE_URL: Final = "https://www.workable.com/api/accounts"
_DEFAULT_USER_AGENT: Final = "career-os/0.1.0 WorkablePublicAdapter"
_TRANSIENT_STATUS_CODES: Final = frozenset({408, 429, 500, 502, 503, 504})
_MAX_RETRY_DELAY_SECONDS: Final = 10.0
type AsyncSleep = Callable[[float], Awaitable[None]]
type RetryJitter = Callable[[int], float]
type UtcClock = Callable[[], datetime]


class WorkablePublicAdapterError(ScanFailedError):
    """Base error for invalid Workable adapter configuration or collection failure."""


class WorkablePublicTransportError(WorkablePublicAdapterError):
    """A network-level failure prevented a usable Workable response."""


class WorkablePublicHttpError(WorkablePublicAdapterError):
    """A non-success or exhausted Workable HTTP response prevented collection."""


@dataclass(frozen=True, slots=True)
class WorkablePublicHttpResponse:
    """Minimal injectable HTTP response shape for deterministic adapter tests."""

    status_code: int
    headers: Mapping[str, str]
    json_body: object
    json_error: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not isinstance(self.status_code, int):
            raise TypeError("status_code must be an integer")
        object.__setattr__(self, "headers", dict(self.headers))


class WorkablePublicHttpClient(Protocol):
    async def get(
        self, *, url: str, headers: Mapping[str, str], timeout_seconds: float
    ) -> WorkablePublicHttpResponse:
        """Return a response or raise a transport error."""


@dataclass(slots=True)
class UrllibWorkablePublicHttpClient:
    """Stdlib client offloaded to a thread to preserve an async boundary."""

    async def get(
        self, *, url: str, headers: Mapping[str, str], timeout_seconds: float
    ) -> WorkablePublicHttpResponse:
        try:
            return await asyncio.to_thread(
                _blocking_get, url=url, headers=headers, timeout_seconds=timeout_seconds
            )
        except (OSError, TimeoutError) as error:
            raise WorkablePublicTransportError(
                "Workable public request transport failure"
            ) from error


def _blocking_get(
    *, url: str, headers: Mapping[str, str], timeout_seconds: float
) -> WorkablePublicHttpResponse:
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
            headers=dict(error.headers.items()) if error.headers else {},
            body=error.read(),
        )


def _response_from_bytes(
    *, status_code: int, headers: Mapping[str, str], body: bytes
) -> WorkablePublicHttpResponse:
    try:
        decoded: object = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError):
        return WorkablePublicHttpResponse(
            status_code=status_code,
            headers=headers,
            json_body=None,
            json_error="response body was not valid UTF-8 JSON",
        )
    return WorkablePublicHttpResponse(
        status_code=status_code,
        headers=headers,
        json_body=decoded,
    )


def _reject_constant(constant: str) -> NoReturn:
    raise ValueError(constant)


def _jitter(attempt: int) -> float:
    return random.uniform(0, min(2**attempt, _MAX_RETRY_DELAY_SECONDS))


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class WorkablePublicAdapter:
    """Fetch one public Workable account response and a terminal scan outcome.

    The credential-free endpoint documents neither pagination nor a completeness
    marker. Consequently, a successful object with a structurally safe jobs list
    is the deliberately weaker completion proof used here.
    """

    config: WorkablePublicSourceConfig
    http_client: WorkablePublicHttpClient = field(default_factory=UrllibWorkablePublicHttpClient)
    sleep: AsyncSleep = asyncio.sleep
    retry_jitter: RetryJitter = _jitter
    utc_now: UtcClock = _now
    max_attempts: int = 3
    user_agent: str = _DEFAULT_USER_AGENT
    kind: ClassVar[str] = "workable_public"

    async def validate(self) -> None:
        if self.config.kind != self.kind:
            raise WorkablePublicAdapterError(
                "Workable public adapter requires workable_public configuration"
            )
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise WorkablePublicAdapterError("max_attempts must be an integer")
        if self.max_attempts < 1:
            raise WorkablePublicAdapterError("max_attempts must be at least one")
        if not isinstance(self.user_agent, str) or not self.user_agent.strip():
            raise WorkablePublicAdapterError("user_agent must be a non-blank string")

    async def fetch(self, context: FetchContext) -> AsyncIterator[FetchEvent]:
        response, diagnostics = await self._request(context)
        if response.json_error is not None:
            yield ScanOutcome(
                status=ScanStatus.PARTIAL,
                incomplete_reason=response.json_error,
                diagnostics=diagnostics,
            )
            return
        if not isinstance(response.json_body, Mapping):
            yield ScanOutcome(
                status=ScanStatus.PARTIAL,
                incomplete_reason="Workable public response root was not an object",
                diagnostics=diagnostics,
            )
            return
        jobs = response.json_body.get("jobs")
        if not isinstance(jobs, list):
            yield ScanOutcome(
                status=ScanStatus.PARTIAL,
                incomplete_reason="Workable public response did not contain a jobs list",
                diagnostics=diagnostics,
            )
            return
        seen: set[str] = set()
        reasons: list[str] = []
        rejected = 0
        for index, item in enumerate(jobs):
            posting = _posting(self.config, item, context.fetched_at)
            if posting is None:
                reasons.append(f"job item {index} was malformed")
                rejected += 1
                continue
            if posting.external_id in seen:
                reasons.append(f"job item {index} duplicated a shortcode")
                rejected += 1
                continue
            seen.add(posting.external_id)
            yield posting
        final = ScanDiagnostics(
            http_requests=diagnostics.http_requests,
            retry_count=diagnostics.retry_count,
            rate_limit_events=diagnostics.rate_limit_events,
            rejected_items=rejected,
        )
        if reasons:
            yield ScanOutcome(
                status=ScanStatus.PARTIAL,
                incomplete_reason="; ".join(reasons),
                diagnostics=final,
            )
        else:
            yield ScanOutcome(status=ScanStatus.COMPLETE, diagnostics=final)

    async def _request(
        self, context: FetchContext
    ) -> tuple[WorkablePublicHttpResponse, ScanDiagnostics]:
        url = f"{_BASE_URL}/{quote(self.config.account_subdomain, safe='')}?details=true"
        headers = {"Accept": "application/json", "User-Agent": self.user_agent.strip()}
        requests = retries = rates = 0
        for attempt in range(self.max_attempts):
            try:
                requests += 1
                response = await self.http_client.get(
                    url=url, headers=headers, timeout_seconds=context.timeout_seconds
                )
            except Exception as error:
                diag = ScanDiagnostics(requests, retries, rates)
                wrapped = (
                    error
                    if isinstance(error, WorkablePublicTransportError)
                    else WorkablePublicTransportError(
                        "Workable public request transport failure", diagnostics=diag
                    )
                )
                if attempt + 1 == self.max_attempts:
                    raise wrapped.with_diagnostics(diag) from error
                retries += 1
                await self._retry(attempt, None, ScanDiagnostics(requests, retries, rates))
                continue
            if 200 <= response.status_code < 300:
                return response, ScanDiagnostics(requests, retries, rates)
            if response.status_code == 429:
                rates += 1
            diag = ScanDiagnostics(requests, retries, rates)
            if response.status_code not in _TRANSIENT_STATUS_CODES:
                raise WorkablePublicHttpError(
                    "Workable public request failed with non-retryable HTTP "
                    f"{response.status_code}",
                    diagnostics=diag,
                )
            if attempt + 1 == self.max_attempts:
                raise WorkablePublicHttpError(
                    f"Workable public request exhausted retries after HTTP {response.status_code}",
                    diagnostics=diag,
                )
            retries += 1
            await self._retry(
                attempt,
                _retry_after(response.headers, self.utc_now),
                ScanDiagnostics(requests, retries, rates),
            )
        raise AssertionError("bounded Workable retry loop ended without a response or failure")

    async def _retry(
        self, attempt: int, retry_after: float | None, diagnostics: ScanDiagnostics
    ) -> None:
        try:
            delay = retry_after if retry_after is not None else self.retry_jitter(attempt)
            if (
                isinstance(delay, bool)
                or not isinstance(delay, (int, float))
                or not isfinite(delay)
                or delay < 0
            ):
                raise ValueError
            await self.sleep(min(float(delay), _MAX_RETRY_DELAY_SECONDS))
        except Exception as error:
            raise WorkablePublicAdapterError(
                "Workable public retry scheduling failed", diagnostics=diagnostics
            ) from error


def _retry_after(headers: Mapping[str, str], now: UtcClock) -> float | None:
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                seconds = float(value)
            except ValueError:
                try:
                    seconds = (parsedate_to_datetime(value) - now()).total_seconds()
                except (TypeError, ValueError):
                    return None
            return (
                min(seconds, _MAX_RETRY_DELAY_SECONDS)
                if isfinite(seconds) and seconds >= 0
                else None
            )
    return None


def _posting(
    config: WorkablePublicSourceConfig, item: object, fetched_at: datetime
) -> RawJobPosting | None:
    if not isinstance(item, Mapping):
        return None
    shortcode = item.get("shortcode")
    if not isinstance(shortcode, str) or not shortcode.strip():
        return None
    application_url = _text(item.get("application_url"))
    shortlink = _text(item.get("shortlink"))
    return RawJobPosting(
        source_key=config.key,
        source_kind="workable_public",
        external_id=shortcode,
        source_url=application_url or shortlink,
        apply_url=_text(item.get("url")),
        payload=cast(Mapping[str, Any], item),
        fetched_at=fetched_at,
    )


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
