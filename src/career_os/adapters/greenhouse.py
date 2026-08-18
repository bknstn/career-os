"""Greenhouse Job Board API adapter.

The public endpoint is ``/v1/boards/{board_token}/jobs?content=true``.  The
adapter deliberately emits source-native records only; canonical cleanup and
persistence remain downstream responsibilities.
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

from career_os.config.sources import GreenhouseSourceConfig
from career_os.domain import FetchContext, FetchEvent, RawJobPosting, ScanOutcome, ScanStatus

_BASE_URL: Final = "https://boards-api.greenhouse.io/v1/boards"
_DEFAULT_USER_AGENT: Final = "career-os/0.1.0 GreenhouseJobBoardAdapter"
_TRANSIENT_STATUS_CODES: Final = frozenset({408, 429, 500, 502, 503, 504})
# Cap server hints deliberately: one unavailable public board must not hold a
# collection worker longer than the bounded backoff policy permits.
_MAX_RETRY_DELAY_SECONDS: Final = 10.0

type AsyncSleep = Callable[[float], Awaitable[None]]
type RetryJitter = Callable[[int], float]
type UtcClock = Callable[[], datetime]


class GreenhouseAdapterError(RuntimeError):
    """Base error for an unusable Greenhouse request or adapter configuration."""


class GreenhouseTransportError(GreenhouseAdapterError):
    """A network-level failure prevented a usable HTTP response."""


class GreenhouseHttpError(GreenhouseAdapterError):
    """A non-successful or exhausted HTTP response prevented a complete scan."""


@dataclass(frozen=True, slots=True)
class GreenhouseHttpResponse:
    """Minimal injectable HTTP response shape for deterministic adapter tests."""

    status_code: int
    headers: Mapping[str, str]
    json_body: object
    json_error: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not isinstance(self.status_code, int):
            raise TypeError("status_code must be an integer")
        object.__setattr__(self, "headers", dict(self.headers))


class GreenhouseHttpClient(Protocol):
    """Narrow async transport boundary used by this adapter only."""

    async def get(
        self, *, url: str, headers: Mapping[str, str], timeout_seconds: float
    ) -> GreenhouseHttpResponse:
        """Return an HTTP response or raise :class:`GreenhouseTransportError`."""


@dataclass(slots=True)
class UrllibGreenhouseHttpClient:
    """Small stdlib transport that keeps adapter networking asynchronous to callers."""

    async def get(
        self, *, url: str, headers: Mapping[str, str], timeout_seconds: float
    ) -> GreenhouseHttpResponse:
        """Run the blocking stdlib request in a worker thread."""
        try:
            return await asyncio.to_thread(
                _blocking_get,
                url=url,
                headers=headers,
                timeout_seconds=timeout_seconds,
            )
        except (OSError, TimeoutError) as error:
            raise GreenhouseTransportError("Greenhouse request transport failure") from error


def _blocking_get(
    *, url: str, headers: Mapping[str, str], timeout_seconds: float
) -> GreenhouseHttpResponse:
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
) -> GreenhouseHttpResponse:
    try:
        decoded: object = json.loads(
            body.decode("utf-8"), parse_constant=_reject_nonstandard_json_constant
        )
    except (UnicodeDecodeError, ValueError):
        return GreenhouseHttpResponse(
            status_code=status_code,
            headers=headers,
            json_body=None,
            json_error="response body was not valid UTF-8 JSON",
        )
    return GreenhouseHttpResponse(status_code=status_code, headers=headers, json_body=decoded)


def _reject_nonstandard_json_constant(constant: str) -> NoReturn:
    """Reject JavaScript numeric extensions that Python's JSON parser accepts."""
    raise ValueError(f"non-standard JSON constant: {constant}")


def _default_jitter(attempt: int) -> float:
    """Return bounded jitter for retry attempt zero onward."""
    return random.uniform(0.0, min(1.0 * 2**attempt, _MAX_RETRY_DELAY_SECONDS))


def _utc_now() -> datetime:
    """Return the current UTC time for parsing HTTP-date retry headers."""
    return datetime.now(UTC)


@dataclass(slots=True)
class GreenhouseAdapter:
    """Stream one configured Greenhouse board and a terminal completeness outcome."""

    config: GreenhouseSourceConfig
    http_client: GreenhouseHttpClient = field(default_factory=UrllibGreenhouseHttpClient)
    sleep: AsyncSleep = asyncio.sleep
    retry_jitter: RetryJitter = _default_jitter
    utc_now: UtcClock = _utc_now
    max_attempts: int = 3
    user_agent: str = _DEFAULT_USER_AGENT

    kind: ClassVar[str] = "greenhouse"

    async def validate(self) -> None:
        """Reject invalid runtime seams before a network request is attempted."""
        if self.config.kind != self.kind:
            raise GreenhouseAdapterError("Greenhouse adapter requires greenhouse configuration")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise GreenhouseAdapterError("max_attempts must be an integer")
        if self.max_attempts < 1:
            raise GreenhouseAdapterError("max_attempts must be at least one")
        if not isinstance(self.user_agent, str) or not self.user_agent.strip():
            raise GreenhouseAdapterError("user_agent must be a non-blank string")

    async def fetch(self, context: FetchContext) -> AsyncIterator[FetchEvent]:
        """Yield valid source records then an unambiguous terminal scan outcome."""
        response = await self._request(context)
        if response.json_error is not None:
            yield ScanOutcome(ScanStatus.PARTIAL, incomplete_reason=response.json_error)
            return
        if not isinstance(response.json_body, Mapping):
            yield ScanOutcome(
                ScanStatus.PARTIAL,
                incomplete_reason="Greenhouse response root was not an object",
            )
            return
        jobs = response.json_body.get("jobs")
        if not isinstance(jobs, list):
            yield ScanOutcome(
                ScanStatus.PARTIAL,
                incomplete_reason="Greenhouse response did not contain a jobs list",
            )
            return

        seen_external_ids: set[str] = set()
        incomplete_reasons = _completeness_reasons(response.json_body, jobs)
        for index, item in enumerate(jobs):
            posting = _posting_from_item(
                config=self.config, item=item, fetched_at=context.fetched_at
            )
            if posting is None:
                incomplete_reasons.append(f"job item {index} was malformed")
                continue
            if posting.external_id in seen_external_ids:
                incomplete_reasons.append(f"job item {index} duplicated an external ID")
                continue
            seen_external_ids.add(posting.external_id)
            yield posting

        if incomplete_reasons:
            yield ScanOutcome(ScanStatus.PARTIAL, incomplete_reason="; ".join(incomplete_reasons))
        else:
            yield ScanOutcome(ScanStatus.COMPLETE)

    async def _request(self, context: FetchContext) -> GreenhouseHttpResponse:
        url = f"{_BASE_URL}/{quote(self.config.board_token, safe='')}/jobs?content=true"
        headers = {"Accept": "application/json", "User-Agent": self.user_agent.strip()}
        for attempt in range(self.max_attempts):
            try:
                response = await self.http_client.get(
                    url=url,
                    headers=headers,
                    timeout_seconds=context.timeout_seconds,
                )
            except GreenhouseTransportError:
                if attempt + 1 == self.max_attempts:
                    raise
                await self._sleep_before_retry(attempt, retry_after=None)
                continue
            if 200 <= response.status_code < 300:
                return response
            if response.status_code not in _TRANSIENT_STATUS_CODES:
                raise GreenhouseHttpError(
                    f"Greenhouse request failed with non-retryable HTTP {response.status_code}"
                )
            if attempt + 1 == self.max_attempts:
                raise GreenhouseHttpError(
                    f"Greenhouse request exhausted retries after HTTP {response.status_code}"
                )
            await self._sleep_before_retry(
                attempt,
                retry_after=_retry_after_seconds(response.headers, utc_now=self.utc_now),
            )
        raise AssertionError("retry loop must return a response or raise")

    async def _sleep_before_retry(self, attempt: int, *, retry_after: float | None) -> None:
        delay = retry_after if retry_after is not None else self.retry_jitter(attempt)
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not isfinite(delay)
            or delay < 0
        ):
            raise GreenhouseAdapterError("retry delay must be a non-negative number")
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


def _posting_from_item(
    *, config: GreenhouseSourceConfig, item: object, fetched_at: datetime
) -> RawJobPosting | None:
    if not isinstance(item, Mapping):
        return None
    raw_id = item.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
        return None
    external_id = str(raw_id).strip()
    if not external_id:
        return None
    absolute_url = item.get("absolute_url")
    url = absolute_url.strip() if isinstance(absolute_url, str) and absolute_url.strip() else None
    return RawJobPosting(
        source_key=config.key,
        source_kind="greenhouse",
        external_id=external_id,
        source_url=url,
        apply_url=url,
        payload=cast(Mapping[str, Any], item),
        fetched_at=fetched_at,
    )


def _completeness_reasons(response: Mapping[str, object], jobs: list[object]) -> list[str]:
    """Return deterministic reasons why a valid body cannot prove a full scan."""
    meta = response.get("meta")
    if not isinstance(meta, Mapping):
        return ["Greenhouse response meta was not an object"]
    total = meta.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return ["Greenhouse response meta.total was not a non-negative integer"]
    if total != len(jobs):
        return ["Greenhouse response meta.total did not match jobs list length"]
    return []
