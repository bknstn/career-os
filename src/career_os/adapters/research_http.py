"""Bounded HTTPS acquisition for allowlisted research publishers."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from email.message import Message
from typing import Literal
from urllib.parse import urljoin, urlsplit

from career_os.config.research_sources import ResearchSource
from career_os.domain.research import ResearchMediaType, normalize_research_url

ResearchFetchOutcome = Literal[
    "http_error", "network_error", "blocked", "oversize", "unsupported_content"
]
RESEARCH_USER_AGENT = "CareerOSResearch/0.1"


class ResearchFetchError(RuntimeError):
    """A visible, classified result of a failed bounded source request."""

    def __init__(
        self,
        outcome: ResearchFetchOutcome,
        error_code: str,
        *,
        http_status: int | None = None,
        bytes_received: int = 0,
        request_count: int = 0,
    ) -> None:
        super().__init__(error_code)
        self.outcome = outcome
        self.error_code = error_code
        self.http_status = http_status
        self.bytes_received = bytes_received
        self.request_count = request_count


@dataclass(frozen=True, slots=True)
class ResearchFetchResult:
    """Original response bytes and bounded HTTP provenance."""

    requested_url: str
    retrieved_url: str
    status: int
    media_type: ResearchMediaType | None
    charset: str | None
    body: bytes
    bytes_received: int
    etag: str | None
    last_modified: str | None
    request_count: int


class ResearchRequestBudget:
    """A per-run wire-request counter shared by all sources and redirects."""

    def __init__(self, maximum: int) -> None:
        if maximum < 1:
            raise ValueError("request budget must be positive")
        self.maximum = maximum
        self.used = 0

    def reserve(self) -> None:
        self.ensure_available()
        self.used += 1

    def ensure_available(self) -> None:
        """Fail before waiting when the run has no remaining wire requests."""
        if self.used >= self.maximum:
            raise ResearchFetchError("blocked", "run_request_limit")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a previously validated public IP while retaining TLS hostname checks."""

    def __init__(
        self,
        hostname: str,
        address: str,
        *,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(hostname, 443, timeout=timeout, context=context)
        self._pinned_address = address
        self._research_context = context

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._pinned_address, self.port), timeout=self.timeout
        )
        self.sock = self._research_context.wrap_socket(raw_socket, server_hostname=self.host)


AddressResolver = Callable[..., Sequence[tuple[object, object, object, object, tuple[object, ...]]]]
ConnectionFactory = Callable[..., http.client.HTTPSConnection]


class ResearchFetcher:
    """Fetch only exact allowlisted HTTPS hosts with redirect, byte, and time caps."""

    def __init__(
        self,
        *,
        request_budget: ResearchRequestBudget,
        resolver: AddressResolver = socket.getaddrinfo,
        connection_factory: ConnectionFactory = _PinnedHTTPSConnection,
        timeout_seconds: float = 12.0,
        maximum_redirects: int = 4,
        retries: int = 1,
        retry_delay_seconds: float = 0.2,
        minimum_host_interval_seconds: float = 0.2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds <= 0 or maximum_redirects < 0 or retries < 0:
            raise ValueError("invalid research fetch limits")
        if minimum_host_interval_seconds < 0:
            raise ValueError("minimum host interval must not be negative")
        self.request_budget = request_budget
        self.resolver = resolver
        self.connection_factory = connection_factory
        self.timeout_seconds = timeout_seconds
        self.maximum_redirects = maximum_redirects
        self.retries = retries
        self.retry_delay_seconds = retry_delay_seconds
        self.minimum_host_interval_seconds = minimum_host_interval_seconds
        self.sleep = sleep
        self.context = ssl.create_default_context()
        self._last_host_request: dict[str, float] = {}

    def fetch(
        self,
        source: ResearchSource,
        value: str,
        *,
        maximum_bytes: int,
        deadline: float,
        allowed_hosts_override: tuple[str, ...] | None = None,
        accept_header: str = "text/html, application/pdf;q=0.9",
        allow_text_plain: bool = False,
    ) -> ResearchFetchResult:
        """Fetch a body or raise a classified error without following untrusted redirects."""
        if maximum_bytes < 1:
            raise ResearchFetchError("oversize", "run_byte_limit")
        allowed_hosts = tuple(
            host.lower().rstrip(".")
            for host in (allowed_hosts_override or tuple(source.allowed_hosts))
        )
        minimum_host_interval_seconds = max(
            self.minimum_host_interval_seconds,
            source.minimum_request_interval_seconds or 0.0,
        )
        try:
            requested = normalize_research_url(value, allowed_hosts=allowed_hosts)
        except ValueError as error:
            raise ResearchFetchError("blocked", "url_not_allowlisted") from error
        current = requested
        requests_for_fetch = 0
        transfer_bytes = 0

        for redirect_number in range(self.maximum_redirects + 1):
            try:
                current = normalize_research_url(current, allowed_hosts=allowed_hosts)
                parsed = urlsplit(current)
                hostname = parsed.hostname
            except (ValueError, ResearchFetchError) as error:
                raise ResearchFetchError(
                    "blocked", "unsafe_redirect_or_destination", request_count=requests_for_fetch
                ) from error
            if hostname is None:
                raise ResearchFetchError(
                    "blocked", "redirect_without_host", request_count=requests_for_fetch
                )
            try:
                address = self._public_address(hostname)
            except (ValueError, OSError, ResearchFetchError) as error:
                if isinstance(error, ResearchFetchError):
                    error.request_count = requests_for_fetch
                    error.bytes_received = max(error.bytes_received, transfer_bytes)
                    raise
                raise ResearchFetchError(
                    "blocked",
                    "unsafe_redirect_or_destination",
                    request_count=requests_for_fetch,
                    bytes_received=transfer_bytes,
                ) from error

            for retry_number in range(self.retries + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ResearchFetchError(
                        "network_error",
                        "run_deadline_exceeded",
                        request_count=requests_for_fetch,
                        bytes_received=transfer_bytes,
                    )
                try:
                    self.request_budget.ensure_available()
                except ResearchFetchError as error:
                    error.request_count = requests_for_fetch
                    error.bytes_received = max(error.bytes_received, transfer_bytes)
                    raise
                pause = self._host_pause(hostname, minimum_host_interval_seconds)
                if pause > 0:
                    if time.monotonic() + pause >= deadline:
                        raise ResearchFetchError(
                            "network_error",
                            "run_deadline_exceeded",
                            request_count=requests_for_fetch,
                            bytes_received=transfer_bytes,
                        )
                    self.sleep(pause)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ResearchFetchError(
                        "network_error",
                        "run_deadline_exceeded",
                        request_count=requests_for_fetch,
                        bytes_received=transfer_bytes,
                    )
                try:
                    self.request_budget.reserve()
                except ResearchFetchError as error:
                    error.request_count = requests_for_fetch
                    error.bytes_received = max(error.bytes_received, transfer_bytes)
                    raise
                requests_for_fetch += 1
                timeout = min(self.timeout_seconds, remaining)
                self._last_host_request[hostname] = time.monotonic()
                connection = self.connection_factory(
                    hostname, address, timeout=timeout, context=self.context
                )
                response_status: int | None = None
                try:
                    path = parsed.path or "/"
                    if parsed.query:
                        path = f"{path}?{parsed.query}"
                    connection.request(
                        "GET",
                        path,
                        headers={
                            "Accept": accept_header,
                            "Accept-Encoding": "identity",
                            "User-Agent": RESEARCH_USER_AGENT,
                        },
                    )
                    response = connection.getresponse()
                    response_status = response.status
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.getheader("Location")
                        if not location or redirect_number == self.maximum_redirects:
                            raise ResearchFetchError(
                                "http_error",
                                "redirect_limit_or_missing_location",
                                http_status=response.status,
                                request_count=requests_for_fetch,
                            )
                        current = urljoin(current, location)
                        break
                    if (
                        response.status in {408, 429} or 500 <= response.status <= 599
                    ) and retry_number < self.retries:
                        self.sleep(self.retry_delay_seconds)
                        continue
                    if response.status < 200 or response.status >= 300:
                        raise ResearchFetchError(
                            "http_error",
                            f"http_{response.status}",
                            http_status=response.status,
                            request_count=requests_for_fetch,
                        )
                    declared_length = response.getheader("Content-Length")
                    if declared_length is not None:
                        try:
                            if int(declared_length) > maximum_bytes - transfer_bytes:
                                raise ResearchFetchError(
                                    "oversize",
                                    "declared_body_over_limit",
                                    http_status=response.status,
                                    request_count=requests_for_fetch,
                                )
                        except ValueError:
                            pass
                    content_encoding = (
                        response.getheader("Content-Encoding") or "identity"
                    ).lower()
                    if content_encoding not in {"", "identity"}:
                        raise ResearchFetchError(
                            "unsupported_content",
                            "compressed_response_not_requested",
                            http_status=response.status,
                            request_count=requests_for_fetch,
                        )
                    if connection.sock is not None:
                        connection.sock.settimeout(
                            min(self.timeout_seconds, max(0.1, deadline - time.monotonic()))
                        )
                    chunks: list[bytes] = []
                    size = 0
                    while size <= maximum_bytes:
                        if time.monotonic() >= deadline:
                            raise ResearchFetchError(
                                "network_error",
                                "run_deadline_exceeded",
                                http_status=response.status,
                                bytes_received=size,
                                request_count=requests_for_fetch,
                            )
                        remaining_transfer = maximum_bytes - transfer_bytes
                        if remaining_transfer < 0:
                            raise ResearchFetchError(
                                "oversize",
                                "fetch_transfer_over_limit",
                                http_status=response.status,
                                bytes_received=transfer_bytes,
                                request_count=requests_for_fetch,
                            )
                        chunk = response.read(min(64 * 1024, remaining_transfer + 1))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        size += len(chunk)
                        transfer_bytes += len(chunk)
                        if transfer_bytes > maximum_bytes:
                            raise ResearchFetchError(
                                "oversize",
                                "fetch_transfer_over_limit",
                                http_status=response.status,
                                bytes_received=transfer_bytes,
                                request_count=requests_for_fetch,
                            )
                    if size > maximum_bytes:
                        raise ResearchFetchError(
                            "oversize",
                            "body_over_limit",
                            http_status=response.status,
                            bytes_received=transfer_bytes,
                            request_count=requests_for_fetch,
                        )
                    body = b"".join(chunks)
                    try:
                        media_type, charset = self._content_metadata(
                            response.getheader("Content-Type"),
                            body,
                            allow_text_plain=allow_text_plain,
                        )
                    except ResearchFetchError as error:
                        error.bytes_received = transfer_bytes
                        error.http_status = response.status
                        error.request_count = requests_for_fetch
                        raise
                    if not body and media_type is not None:
                        raise ResearchFetchError(
                            "unsupported_content",
                            "empty_body",
                            http_status=response.status,
                            bytes_received=transfer_bytes,
                            request_count=requests_for_fetch,
                        )
                    return ResearchFetchResult(
                        requested_url=requested,
                        retrieved_url=current,
                        status=response.status,
                        media_type=media_type,
                        charset=charset,
                        body=body,
                        bytes_received=transfer_bytes,
                        etag=response.getheader("ETag"),
                        last_modified=response.getheader("Last-Modified"),
                        request_count=requests_for_fetch,
                    )
                except ResearchFetchError as error:
                    error.bytes_received = max(error.bytes_received, transfer_bytes)
                    if error.http_status is None:
                        error.http_status = response_status
                    error.request_count = max(error.request_count, requests_for_fetch)
                    raise
                except (OSError, TimeoutError, http.client.HTTPException, ssl.SSLError) as error:
                    if retry_number < self.retries and time.monotonic() < deadline:
                        self.sleep(self.retry_delay_seconds)
                        continue
                    raise ResearchFetchError(
                        "network_error",
                        type(error).__name__.lower(),
                        request_count=requests_for_fetch,
                        http_status=response_status,
                        bytes_received=transfer_bytes,
                    ) from error
                finally:
                    connection.close()
            else:
                raise ResearchFetchError(
                    "http_error", "unexpected_redirect_flow", request_count=requests_for_fetch
                )
        raise ResearchFetchError(
            "http_error", "too_many_redirects", request_count=requests_for_fetch
        )

    def _public_address(self, hostname: str) -> str:
        try:
            resolved = self.resolver(hostname, 443, type=socket.SOCK_STREAM)
        except OSError as error:
            raise ResearchFetchError("network_error", "dns_resolution_failed") from error
        addresses: list[str] = []
        for record in resolved:
            try:
                address = str(record[4][0])
                parsed = ipaddress.ip_address(address.split("%", maxsplit=1)[0])
            except (IndexError, TypeError, ValueError):
                continue
            if not parsed.is_global:
                raise ResearchFetchError("blocked", "dns_resolved_non_public_address")
            addresses.append(address)
        if not addresses:
            raise ResearchFetchError("network_error", "dns_no_public_address")
        return addresses[0]

    def _host_pause(self, hostname: str, minimum_interval_seconds: float) -> float:
        if minimum_interval_seconds == 0:
            return 0.0
        last_request = self._last_host_request.get(hostname)
        if last_request is None:
            return 0.0
        ready_at = last_request + minimum_interval_seconds
        return max(0.0, ready_at - time.monotonic())

    @staticmethod
    def _content_metadata(
        value: str | None, body: bytes, *, allow_text_plain: bool = False
    ) -> tuple[ResearchMediaType | None, str | None]:
        header = Message()
        header["content-type"] = value or ""
        media = header.get_content_type().lower()
        charset = header.get_content_charset()
        if media in {"text/html", "application/xhtml+xml"}:
            return ResearchMediaType.HTML, charset
        if media == "text/plain" and allow_text_plain:
            return None, charset
        if media == "application/pdf" or (
            media == "application/octet-stream" and body.startswith(b"%PDF-")
        ):
            return ResearchMediaType.PDF, charset
        raise ResearchFetchError("unsupported_content", "unsupported_media_type")
