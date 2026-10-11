"""Opt-in panel source identity must be checked before an adapter can fetch."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from career_os.application.ingestion import (
    GREENHOUSE_NORMALIZER_VERSION,
    NO_TAXONOMY_VERSION,
    IngestionService,
    greenhouse_candidate,
)
from career_os.config.radar_panel import RadarPanelManifest
from career_os.config.sources import GreenhouseSourceConfig, SourcesConfig
from career_os.domain import FetchContext, FetchEvent, ScanOutcome, ScanStatus
from career_os.persistence import RadarPanelVersionConflictError


class _NoopCoordinator:
    @contextmanager
    def hold(self, *_args: object, **_kwargs: object):
        yield None


class _CountingAdapter:
    kind = "greenhouse"

    def __init__(self) -> None:
        self.validate_calls = 0
        self.fetch_calls = 0

    async def validate(self) -> None:
        self.validate_calls += 1

    async def fetch(self, _context: FetchContext) -> AsyncIterator[FetchEvent]:
        self.fetch_calls += 1
        if False:
            yield ScanOutcome(ScanStatus.COMPLETE)  # pragma: no cover - never fetched.


def test_direct_service_rejects_source_config_drift_before_adapter_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "panel.yaml"
    manifest_path.write_text(
        "panel_version: radar-v1\nsources:\n  - source_key: greenhouse\n"
        "    company_key: example\n    source_kind: greenhouse\n",
        encoding="utf-8",
    )
    original = GreenhouseSourceConfig(
        key="greenhouse",
        kind="greenhouse",
        company_name="Example",
        board_token="public-board",
    )
    changed = original.model_copy(update={"board_token": "changed-board"})
    panel = RadarPanelManifest.load(manifest_path, SourcesConfig(sources=[original]))
    adapter = _CountingAdapter()
    monkeypatch.setattr(
        "career_os.application.ingestion.SourceCollectionCoordinator",
        lambda **_kwargs: _NoopCoordinator(),
    )
    service = IngestionService(
        session_factory=sessionmaker(),
        logger=logging.getLogger("radar-capture-unit-test"),
        request_timeout_seconds=1.0,
        normalizer_version=GREENHOUSE_NORMALIZER_VERSION,
        taxonomy_version=NO_TAXONOMY_VERSION,
        candidate_factory=greenhouse_candidate,
    )

    with pytest.raises(RadarPanelVersionConflictError, match="source config drifted"):
        asyncio.run(service.collect(source_config=changed, adapter=adapter, radar_panel=panel))

    assert adapter.validate_calls == 0
    assert adapter.fetch_calls == 0
