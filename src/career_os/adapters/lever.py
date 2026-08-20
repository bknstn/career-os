"""Lever Postings API adapter with conservative offset pagination.

Lever's documented public endpoint returns a JSON array and has no documented
total-count field.  A scan is complete only after a successful page contains
fewer records than the requested limit.  Consequently a full page is always
followed by another request (which may be the empty terminal page).
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

from career_os.config.sources import LeverSourceConfig
from career_os.domain import (
    FetchContext,
    FetchEvent,
    RawJobPosting,
    ScanDiagnostics,
    ScanFailedError,
    ScanOutcome,
    ScanStatus,
)

_GLOBAL_BASE_URL: Final = "https://api.lever.co/v0/postings"
_EU_BASE_URL: Final = "https://api.eu.lever.co/v0/postings"
_DEFAULT_USER_AGENT: Final = "career-os/0.1.0 LeverPostingsAdapter"
_TRANSIENT_STATUS_CODES: Final = frozenset({408, 429, 500, 502, 503, 504})
_MAX_RETRY_DELAY_SECONDS: Final = 10.0
_MAX_PAGE_SIZE: Final = 100
_MAX_PAGES: Final = 1_000

type AsyncSleep = Callable[[float], Awaitable[None]]
type RetryJitter = Callable[[int], float]
type UtcClock = Callable[[], datetime]


class LeverAdapterError(ScanFailedError):
    """Base error for an unusable Lever request or adapter configuration."""


class LeverTransportError(LeverAdapterError):
    """A network-level failure prevented a usable HTTP response."""


class LeverHttpError(LeverAdapterError):
    """A non-successful or exhausted HTTP response prevented a complete scan."""


@dataclass(frozen=True, slots=True)
class LeverHttpResponse:
    """Minimal injectable HTTP response shape for deterministic adapter tests."""

    status_code: int
    headers: Mapping[str, str]
    json_body: object
    json_error: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not isinstance(self.status_code, int):
            raise TypeError("status_code must be an integer")
        object.__setattr__(self, "headers", dict(self.headers))


class LeverHttpClient(Protocol):
    """Narrow async transport boundary used by this adapter only."""

    async def get(
        self, *, url: str, headers: Mapping[str, str], timeout_seconds: float
    ) -> LeverHttpResponse:
        """Return an HTTP response or raise :class:`LeverTransportError`."""


@dataclass(slots=True)
class UrllibLeverHttpClient:
    """Small stdlib transport that keeps adapter networking asynchronous to callers."""

    async def get(
        self, *, url: str, headers: Mapping[str, str], timeout_seconds: float
    ) -> LeverHttpResponse:
        try:
            return await asyncio.to_thread(
                _blocking_get,
                url=url,
                headers=headers,
                timeout_seconds=timeout_seconds,
            )
        except (OSError, TimeoutError) as error:
            raise LeverTransportError("Lever request transport failure") from error


def _blocking_get(
    *, url: str, headers: Mapping[str, str], timeout_seconds: float
) -> LeverHttpResponse:
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
) -> LeverHttpResponse:
    try:
        decoded: object = json.loads(
            body.decode("utf-8"), parse_constant=_reject_nonstandard_json_constant
        )
    except (UnicodeDecodeError, ValueError):
        return LeverHttpResponse(
            status_code=status_code,
            headers=headers,
            json_body=None,
            json_error="response body was not valid UTF-8 JSON",
        )
    return LeverHttpResponse(status_code=status_code, headers=headers, json_body=decoded)


def _reject_nonstandard_json_constant(constant: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant: {constant}")


def _default_jitter(attempt: int) -> float:
    return random.uniform(0.0, min(1.0 * 2**attempt, _MAX_RETRY_DELAY_SECONDS))


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class LeverAdapter:
    """Stream all configured Lever postings and one terminal completeness outcome."""

    config: LeverSourceConfig
    http_client: LeverHttpClient = field(default_factory=UrllibLeverHttpClient)
    sleep: AsyncSleep = asyncio.sleep
    retry_jitter: RetryJitter = _default_jitter
    utc_now: UtcClock = _utc_now
    max_attempts: int = 3
    page_size: int = _MAX_PAGE_SIZE
    max_pages: int = _MAX_PAGES
    user_agent: str = _DEFAULT_USER_AGENT

    kind: ClassVar[str] = "lever"

    async def validate(self) -> None:
        if self.config.kind != self.kind:
            raise LeverAdapterError("Lever adapter requires lever configuration")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise LeverAdapterError("max_attempts must be an integer")
        if self.max_attempts < 1:
            raise LeverAdapterError("max_attempts must be at least one")
        if isinstance(self.page_size, bool) or not isinstance(self.page_size, int):
            raise LeverAdapterError("page_size must be an integer")
        if not 1 <= self.page_size <= _MAX_PAGE_SIZE:
            raise LeverAdapterError(f"page_size must be between 1 and {_MAX_PAGE_SIZE}")
        if isinstance(self.max_pages, bool) or not isinstance(self.max_pages, int):
            raise LeverAdapterError("max_pages must be an integer")
        if not 1 <= self.max_pages <= _MAX_PAGES:
            raise LeverAdapterError(f"max_pages must be between 1 and {_MAX_PAGES}")
        if not isinstance(self.user_agent, str) or not self.user_agent.strip():
            raise LeverAdapterError("user_agent must be a non-blank string")

    async def fetch(self, context: FetchContext) -> AsyncIterator[FetchEvent]:
        """Fetch pages until the documented short-page completion condition holds."""
        diagnostics = ScanDiagnostics()
        seen_external_ids: set[str] = set()
        incomplete_reasons: list[str] = []
        skip = 0
        pages_fetched = 0
        while True:
            try:
                response, page_diagnostics = await self._request_page(context, skip=skip)
            except ScanFailedError as error:
                raise error.with_diagnostics(
                    _merge_diagnostics(diagnostics, error.diagnostics)
                ) from error
            diagnostics = _merge_diagnostics(diagnostics, page_diagnostics)
            if response.json_error is not None:
                yield ScanOutcome(
                    ScanStatus.PARTIAL,
                    incomplete_reason=_incomplete_reason(
                        incomplete_reasons,
                        f"Lever page at skip {skip}: {response.json_error}",
                    ),
                    diagnostics=diagnostics,
                )
                return
            if not isinstance(response.json_body, list):
                yield ScanOutcome(
                    ScanStatus.PARTIAL,
                    incomplete_reason=_incomplete_reason(
                        incomplete_reasons,
                        f"Lever page at skip {skip} was not a JSON array",
                    ),
                    diagnostics=diagnostics,
                )
                return

            page = response.json_body
            accepted_items = 0
            for index, item in enumerate(page):
                posting = _posting_from_item(
                    config=self.config, item=item, fetched_at=context.fetched_at
                )
                if posting is None:
                    incomplete_reasons.append(f"page skip {skip} item {index} was malformed")
                    diagnostics = _with_rejected_item(diagnostics)
                    continue
                if posting.external_id in seen_external_ids:
                    incomplete_reasons.append(
                        f"page skip {skip} item {index} duplicated an external ID"
                    )
                    diagnostics = _with_rejected_item(diagnostics)
                    continue
                seen_external_ids.add(posting.external_id)
                accepted_items += 1
                yield posting

            pages_fetched += 1
            if len(page) < self.page_size:
                if incomplete_reasons:
                    yield ScanOutcome(
                        ScanStatus.PARTIAL,
                        incomplete_reason="; ".join(incomplete_reasons),
                        diagnostics=diagnostics,
                    )
                else:
                    yield ScanOutcome(ScanStatus.COMPLETE, diagnostics=diagnostics)
                return
            if accepted_items == 0:
                incomplete_reasons.append(f"Lever pagination made no progress at skip {skip}")
                yield ScanOutcome(
                    ScanStatus.PARTIAL,
                    incomplete_reason="; ".join(incomplete_reasons),
                    diagnostics=diagnostics,
                )
                return
            if pages_fetched >= self.max_pages:
                incomplete_reasons.append(
                    f"Lever pagination reached the {self.max_pages}-page traversal limit"
                )
                yield ScanOutcome(
                    ScanStatus.PARTIAL,
                    incomplete_reason="; ".join(incomplete_reasons),
                    diagnostics=diagnostics,
                )
                return
            skip += self.page_size

    async def _request_page(
        self, context: FetchContext, *, skip: int
    ) -> tuple[LeverHttpResponse, ScanDiagnostics]:
        base_url = _EU_BASE_URL if self.config.region == "eu" else _GLOBAL_BASE_URL
        url = (
            f"{base_url}/{quote(self.config.site, safe='')}?skip={skip}"
            f"&limit={self.page_size}&mode=json"
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
                    if isinstance(error, LeverTransportError)
                    else LeverTransportError(
                        "Lever request transport failure", diagnostics=diagnostics
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
                raise LeverHttpError(
                    f"Lever request failed with non-retryable HTTP {response.status_code}",
                    diagnostics=diagnostics,
                )
            if attempt + 1 == self.max_attempts:
                raise LeverHttpError(
                    f"Lever request exhausted retries after HTTP {response.status_code}",
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
            raise LeverAdapterError(
                "Lever retry scheduling failed", diagnostics=diagnostics
            ) from error

    async def _sleep_before_retry(self, attempt: int, *, retry_after: float | None) -> None:
        delay = retry_after if retry_after is not None else self.retry_jitter(attempt)
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not isfinite(delay)
            or delay < 0
        ):
            raise LeverAdapterError("retry delay must be a non-negative number")
        await self.sleep(min(float(delay), _MAX_RETRY_DELAY_SECONDS))


def _merge_diagnostics(left: ScanDiagnostics, right: ScanDiagnostics) -> ScanDiagnostics:
    return ScanDiagnostics(
        http_requests=left.http_requests + right.http_requests,
        retry_count=left.retry_count + right.retry_count,
        rate_limit_events=left.rate_limit_events + right.rate_limit_events,
        rejected_items=left.rejected_items + right.rejected_items,
    )


def _with_rejected_item(diagnostics: ScanDiagnostics) -> ScanDiagnostics:
    return ScanDiagnostics(
        http_requests=diagnostics.http_requests,
        retry_count=diagnostics.retry_count,
        rate_limit_events=diagnostics.rate_limit_events,
        rejected_items=diagnostics.rejected_items + 1,
    )


def _incomplete_reason(previous: list[str], terminal_reason: str) -> str:
    """Keep earlier unsafe evidence visible when a later page prevents traversal."""
    return "; ".join((*previous, terminal_reason))


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


def _posting_from_item(
    *, config: LeverSourceConfig, item: object, fetched_at: datetime
) -> RawJobPosting | None:
    if not isinstance(item, Mapping):
        return None
    raw_id = item.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return None
    return RawJobPosting(
        source_key=config.key,
        source_kind="lever",
        external_id=raw_id,
        source_url=_nonblank_string(item.get("hostedUrl")),
        apply_url=_nonblank_string(item.get("applyUrl")),
        payload=cast(Mapping[str, Any], item),
        fetched_at=fetched_at,
    )


def _nonblank_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
