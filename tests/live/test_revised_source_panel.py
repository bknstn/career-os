"""Opt-in D2 live validation for every revised source-panel board."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from career_os.application.ingestion import (
    ashby_candidate,
    greenhouse_candidate,
    lever_candidate,
)
from career_os.application.job_enrichment import build_normalization_input
from career_os.cli import adapter_registry
from career_os.config.markets import MarketFilters, load_market_filters
from career_os.config.sources import (
    AshbySourceConfig,
    GreenhouseSourceConfig,
    LeverSourceConfig,
    SourceConfig,
    load_sources_config,
)
from career_os.domain import FetchContext, RawJobPosting, ScanOutcome, ScanStatus
from career_os.domain.canonical import CanonicalJobCandidate
from career_os.domain.normalization import JobNormalization
from career_os.normalization.jobs import normalize_job

pytestmark = pytest.mark.live
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCES_PATH = PROJECT_ROOT / "config/sources.market-iteration.yaml"
MARKET_PATH = PROJECT_ROOT / "config/market.initial.yaml"


def test_every_revised_source_board_has_a_complete_mappable_live_contract() -> None:
    if os.environ.get("RUN_D2_PANEL_LIVE") != "1":
        pytest.skip("set RUN_D2_PANEL_LIVE=1 to validate the revised public source panel")
    sources = load_sources_config(SOURCES_PATH)
    filters = load_market_filters(MARKET_PATH).model_copy(update={"company_names": ()})

    results = asyncio.run(_probe_all(tuple(sources.sources), filters))

    for result in sorted(results, key=lambda item: str(item["source_key"])):
        print(json.dumps(result, sort_keys=True))
    assert len(results) == 20
    assert all(result["status"] == "complete" for result in results)
    empty = [str(result["source_key"]) for result in results if int(result["postings"]) == 0]
    assert not empty, f"empty revised-panel boards: {', '.join(empty)}"


async def _probe_all(
    sources: tuple[SourceConfig, ...], filters: MarketFilters
) -> tuple[dict[str, object], ...]:
    return tuple(await asyncio.gather(*(_probe(source, filters) for source in sources)))


async def _probe(source: SourceConfig, filters: MarketFilters) -> dict[str, object]:
    adapter = adapter_registry().build(source)
    await adapter.validate()
    events = tuple(
        [
            event
            async for event in adapter.fetch(
                FetchContext(
                    run_id=f"d2-panel-{source.key}",
                    fetched_at=datetime.now(UTC),
                    timeout_seconds=30,
                )
            )
        ]
    )
    postings = tuple(event for event in events if isinstance(event, RawJobPosting))
    outcomes = tuple(event for event in events if isinstance(event, ScanOutcome))
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.status is ScanStatus.COMPLETE, outcome.incomplete_reason
    normalized = tuple(_normalize(source, posting) for posting in postings)
    counts = _funnel_counts(normalized, filters)
    return {
        "source_key": source.key,
        "kind": source.kind,
        "status": outcome.status.value,
        "postings": len(postings),
        **counts,
        "http_requests": outcome.diagnostics.http_requests,
        "retries": outcome.diagnostics.retry_count,
        "rate_limits": outcome.diagnostics.rate_limit_events,
        "rejected": outcome.diagnostics.rejected_items,
    }


def _normalize(source: SourceConfig, posting: RawJobPosting) -> JobNormalization:
    candidate = _candidate(source, posting)
    job = SimpleNamespace(
        title_raw=candidate.title_raw,
        description_text=candidate.description_text,
        raw_latest=_ordinary_json(posting.payload),
    )
    return normalize_job(build_normalization_input(job=job, source_kind=source.kind))  # type: ignore[arg-type]


def _candidate(source: SourceConfig, posting: RawJobPosting) -> CanonicalJobCandidate:
    if isinstance(source, GreenhouseSourceConfig):
        return greenhouse_candidate(source, posting)
    if isinstance(source, AshbySourceConfig):
        return ashby_candidate(source, posting)
    if isinstance(source, LeverSourceConfig):
        return lever_candidate(source, posting)
    raise AssertionError(f"unsupported revised-panel source: {source.kind}")


def _funnel_counts(values: tuple[JobNormalization, ...], filters: MarketFilters) -> dict[str, int]:
    title = tuple(
        value
        for value in values
        if any(term in value.title_normalized.lower() for term in filters.title_include)
        and all(term not in value.title_normalized.lower() for term in filters.title_exclude)
    )
    role = tuple(value for value in title if value.role_family.value in filters.role_families)
    seniority = tuple(value for value in role if value.seniority.value in filters.seniorities)
    geography_evidence = tuple(
        value
        for value in seniority
        if _local_match(value, filters)
        or (filters.include_remote and value.workplace_type.value == "remote")
    )
    eligible = tuple(
        value
        for value in geography_evidence
        if _local_match(value, filters)
        or (
            value.workplace_type.value == "remote"
            and value.remote_scope.value is not None
            and value.remote_scope.value.lower() in filters.remote_scopes
        )
    )
    return {
        "title_candidates": len(title),
        "accepted_roles": len(role),
        "accepted_seniorities": len(seniority),
        "geography_evidence": len(geography_evidence),
        "eligible": len(eligible),
    }


def _local_match(value: JobNormalization, filters: MarketFilters) -> bool:
    return any(location.country_code in filters.country_codes for location in value.locations)


def _ordinary_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _ordinary_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_ordinary_json(item) for item in value]
    return value
