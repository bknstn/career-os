"""Opt-in live contract smoke tests; excluded from default offline execution."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from career_os.adapters import AshbyAdapter, GreenhouseAdapter, LeverAdapter, WorkablePublicAdapter
from career_os.application.ingestion import (
    ashby_candidate,
    greenhouse_candidate,
    lever_candidate,
    workable_public_candidate,
)
from career_os.config.sources import (
    AshbySourceConfig,
    GreenhouseSourceConfig,
    LeverSourceConfig,
    WorkablePublicSourceConfig,
)
from career_os.domain import FetchContext, FetchEvent, RawJobPosting, ScanOutcome, ScanStatus

pytestmark = pytest.mark.live


def _require_opt_in() -> None:
    if os.environ.get("RUN_LIVE_SMOKE") != "1":
        pytest.skip("set RUN_LIVE_SMOKE=1 to call public ATS endpoints")


async def _events(stream: AsyncIterator[FetchEvent]) -> tuple[FetchEvent, ...]:
    return tuple([event async for event in stream])


def _fetch(adapter: object) -> tuple[tuple[RawJobPosting, ...], ScanOutcome]:
    _require_opt_in()

    async def run() -> tuple[tuple[RawJobPosting, ...], ScanOutcome]:
        await adapter.validate()  # type: ignore[attr-defined]
        events = await _events(
            adapter.fetch(  # type: ignore[attr-defined]
                FetchContext(
                    run_id="task-28-live-smoke",
                    fetched_at=datetime.now(UTC),
                    timeout_seconds=30,
                )
            )
        )
        postings = tuple(event for event in events if isinstance(event, RawJobPosting))
        outcomes = tuple(event for event in events if isinstance(event, ScanOutcome))
        assert len(outcomes) == 1
        return postings, outcomes[0]

    postings, outcome = asyncio.run(run())
    assert outcome.status is ScanStatus.COMPLETE, outcome.incomplete_reason
    assert postings
    assert outcome.diagnostics.http_requests >= 1
    return postings, outcome


def test_live_greenhouse_contract_and_mapper() -> None:
    config = GreenhouseSourceConfig(
        key="live-greenhouse",
        kind="greenhouse",
        company_name="Stripe",
        board_token=os.environ.get("LIVE_GREENHOUSE_BOARD", "stripe"),
    )
    postings, outcome = _fetch(GreenhouseAdapter(config=config))
    candidates = tuple(greenhouse_candidate(config, posting) for posting in postings)
    assert all(candidate.description_text for candidate in candidates)
    _report("greenhouse", config.board_token, postings, outcome)


def test_live_lever_contract_pagination_and_mapper() -> None:
    config = LeverSourceConfig(
        key="live-lever",
        kind="lever",
        company_name="Palantir",
        site=os.environ.get("LIVE_LEVER_SITE", "palantir"),
    )
    postings, outcome = _fetch(LeverAdapter(config=config, page_size=25))
    candidates = tuple(lever_candidate(config, posting) for posting in postings)
    assert all(candidate.description_text for candidate in candidates)
    expected_requests = 2 if len(postings) >= 25 else 1
    assert outcome.diagnostics.http_requests >= expected_requests
    _report("lever", config.site, postings, outcome)


def test_live_ashby_contract_compensation_and_mapper() -> None:
    config = AshbySourceConfig(
        key="live-ashby",
        kind="ashby",
        company_name="Ashby",
        board_name=os.environ.get("LIVE_ASHBY_BOARD", "Ashby"),
        include_compensation=True,
    )
    postings, outcome = _fetch(AshbyAdapter(config=config))
    candidates = tuple(ashby_candidate(config, posting) for posting in postings)
    assert all(candidate.description_text for candidate in candidates)
    _report("ashby", config.board_name, postings, outcome)


def test_live_workable_public_contract_and_mapper() -> None:
    config = WorkablePublicSourceConfig(
        key="live-workable",
        kind="workable_public",
        company_name="Borrowell",
        account_subdomain=os.environ.get("LIVE_WORKABLE_ACCOUNT", "borrowell"),
    )
    postings, outcome = _fetch(WorkablePublicAdapter(config=config))
    candidates = tuple(workable_public_candidate(config, posting) for posting in postings)
    assert all(candidate.description_text for candidate in candidates)
    _report("workable_public", config.account_subdomain, postings, outcome)


def _report(
    kind: str,
    board: str,
    postings: tuple[RawJobPosting, ...],
    outcome: ScanOutcome,
) -> None:
    diagnostics = outcome.diagnostics
    print(
        f"{kind} board={board} status={outcome.status.value} postings={len(postings)} "
        f"requests={diagnostics.http_requests} retries={diagnostics.retry_count} "
        f"rate_limits={diagnostics.rate_limit_events} rejected={diagnostics.rejected_items}"
    )
