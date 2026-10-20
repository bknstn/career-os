"""Bounded HTTP tests for the allowlisted research acquisition boundary."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from career_os.adapters.research_http import (
    RESEARCH_USER_AGENT,
    ResearchFetcher,
    ResearchFetchError,
    ResearchRequestBudget,
)
from career_os.config.research_sources import ResearchSourceRegistry


def _registry() -> ResearchSourceRegistry:
    return ResearchSourceRegistry.load(
        Path(__file__).resolve().parents[2] / "config/radar/research.sources.v1.yaml"
    )


class _Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes = b"") -> None:
        self.status = status
        self.headers = headers
        self._body = body

    def getheader(self, name: str) -> str | None:
        return self.headers.get(name)

    def read(self, amount: int = -1) -> bytes:
        body, self._body = self._body[:amount], self._body[amount:]
        return body


class _PartialFailureResponse(_Response):
    def __init__(self) -> None:
        super().__init__(200, {"Content-Type": "text/html"})
        self.calls = 0

    def read(self, amount: int = -1) -> bytes:
        self.calls += 1
        if self.calls == 1:
            return b"part"
        raise OSError("connection reset after partial body")


class _Connection:
    sock = None

    def __init__(self, response: _Response) -> None:
        self.response = response
        self.request_arguments: tuple[Any, ...] | None = None

    def request(self, *args: Any, **kwargs: Any) -> None:
        self.request_arguments = (args, kwargs)

    def getresponse(self) -> _Response:
        return self.response

    def close(self) -> None:
        return None


def _public_resolver(host: str, port: int, *, type: int) -> list[tuple[object, ...]]:
    assert host and port == 443 and type == socket.SOCK_STREAM
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def test_fetcher_pins_public_destination_and_preserves_original_body() -> None:
    connection = _Connection(
        _Response(
            200,
            {"Content-Type": "text/html; charset=utf-8", "ETag": '"v1"'},
            b"<article>Research</article>",
        )
    )
    fetcher = ResearchFetcher(
        request_budget=ResearchRequestBudget(4),
        resolver=_public_resolver,
        connection_factory=lambda *args, **kwargs: connection,
    )

    result = fetcher.fetch(
        _registry().get("anthropic_economics"),
        "https://www.anthropic.com/research/labor-market-impacts",
        maximum_bytes=1024,
        deadline=10**12,
    )

    assert result.body == b"<article>Research</article>"
    assert result.retrieved_url == result.requested_url
    assert result.etag == '"v1"'
    assert result.request_count == 1
    assert connection.request_arguments is not None
    assert connection.request_arguments[0][0] == "GET"
    assert connection.request_arguments[1]["headers"]["Accept-Encoding"] == "identity"
    assert connection.request_arguments[1]["headers"]["User-Agent"] == RESEARCH_USER_AGENT


def test_fetcher_rejects_private_dns_answer_before_connecting() -> None:
    def private_resolver(host: str, port: int, *, type: int) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    fetcher = ResearchFetcher(request_budget=ResearchRequestBudget(4), resolver=private_resolver)
    with pytest.raises(ResearchFetchError, match="dns_resolved_non_public_address") as error:
        fetcher.fetch(
            _registry().get("anthropic_economics"),
            "https://www.anthropic.com/research/labor-market-impacts",
            maximum_bytes=1024,
            deadline=10**12,
        )
    assert error.value.outcome == "blocked"


def test_fetcher_rejects_cross_host_redirect_before_following_it() -> None:
    responses: Iterator[_Response] = iter(
        [_Response(302, {"Location": "https://attacker.example/steal"})]
    )
    connections: list[_Connection] = []

    def connection_factory(*args: Any, **kwargs: Any) -> _Connection:
        connection = _Connection(next(responses))
        connections.append(connection)
        return connection

    fetcher = ResearchFetcher(
        request_budget=ResearchRequestBudget(4),
        resolver=_public_resolver,
        connection_factory=connection_factory,
    )
    with pytest.raises(ResearchFetchError, match="unsafe_redirect_or_destination") as error:
        fetcher.fetch(
            _registry().get("anthropic_economics"),
            "https://www.anthropic.com/research/labor-market-impacts",
            maximum_bytes=1024,
            deadline=10**12,
        )
    assert error.value.outcome == "blocked"
    assert len(connections) == 1


def test_fetcher_stops_after_bounded_body_size() -> None:
    connection = _Connection(
        _Response(200, {"Content-Type": "text/html", "Content-Length": "5"}, b"12345")
    )
    fetcher = ResearchFetcher(
        request_budget=ResearchRequestBudget(4),
        resolver=_public_resolver,
        connection_factory=lambda *args, **kwargs: connection,
    )
    with pytest.raises(ResearchFetchError, match="declared_body_over_limit") as error:
        fetcher.fetch(
            _registry().get("anthropic_economics"),
            "https://www.anthropic.com/research/labor-market-impacts",
            maximum_bytes=4,
            deadline=10**12,
        )
    assert error.value.outcome == "oversize"


def test_unsupported_media_reports_downloaded_bytes_and_status() -> None:
    connection = _Connection(_Response(200, {"Content-Type": "image/png"}, b"not-html"))
    fetcher = ResearchFetcher(
        request_budget=ResearchRequestBudget(4),
        resolver=_public_resolver,
        connection_factory=lambda *args, **kwargs: connection,
        retries=0,
    )

    with pytest.raises(ResearchFetchError, match="unsupported_media_type") as error:
        fetcher.fetch(
            _registry().get("anthropic_economics"),
            "https://www.anthropic.com/research/labor-market-impacts",
            maximum_bytes=1024,
            deadline=10**12,
        )

    assert error.value.http_status == 200
    assert error.value.bytes_received == len(b"not-html")


def test_partial_read_bytes_are_carried_across_retry_and_failure() -> None:
    responses = iter(
        [
            _PartialFailureResponse(),
            _Response(200, {"Content-Type": "text/html"}, b"ok"),
        ]
    )
    fetcher = ResearchFetcher(
        request_budget=ResearchRequestBudget(4),
        resolver=_public_resolver,
        connection_factory=lambda *args, **kwargs: _Connection(next(responses)),
        retry_delay_seconds=0,
    )

    result = fetcher.fetch(
        _registry().get("anthropic_economics"),
        "https://www.anthropic.com/research/labor-market-impacts",
        maximum_bytes=8,
        deadline=10**12,
    )

    assert result.body == b"ok"
    assert result.bytes_received == len(b"partok")
    assert result.request_count == 2

    failing_fetcher = ResearchFetcher(
        request_budget=ResearchRequestBudget(2),
        resolver=_public_resolver,
        connection_factory=lambda *args, **kwargs: _Connection(_PartialFailureResponse()),
        retries=0,
    )
    with pytest.raises(ResearchFetchError) as error:
        failing_fetcher.fetch(
            _registry().get("anthropic_economics"),
            "https://www.anthropic.com/research/labor-market-impacts",
            maximum_bytes=8,
            deadline=10**12,
        )
    assert error.value.bytes_received == len(b"part")
