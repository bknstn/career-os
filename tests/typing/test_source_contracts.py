"""Static typing fixture for the public source-adapter protocol."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import assert_type

from career_os.adapters import AdapterRegistration, AdapterRegistry
from career_os.config.sources import GreenhouseSourceConfig
from career_os.domain import FetchContext, FetchEvent, ScanOutcome, ScanStatus, SourceAdapter


class TypedFakeAdapter:
    kind = "typed_fake"

    async def validate(self) -> None:
        return None

    async def fetch(self, context: FetchContext) -> AsyncIterator[FetchEvent]:
        yield ScanOutcome(ScanStatus.COMPLETE)


adapter: SourceAdapter = TypedFakeAdapter()
assert_type(adapter, SourceAdapter)
assert_type(
    TypedFakeAdapter().fetch(FetchContext(run_id="typing", fetched_at=datetime.now(UTC))),
    AsyncIterator[FetchEvent],
)


def test_typing_fixture_is_importable() -> None:
    assert adapter.kind == "typed_fake"


def build_typed_fake(source_config: GreenhouseSourceConfig) -> SourceAdapter:
    return TypedRegistryAdapter(source_config.kind)


class TypedRegistryAdapter(TypedFakeAdapter):
    def __init__(self, kind: str) -> None:
        self.kind = kind


registry = AdapterRegistry(
    (
        AdapterRegistration.for_config(
            kind="greenhouse",
            config_type=GreenhouseSourceConfig,
            factory=build_typed_fake,
        ),
    )
)
assert_type(
    registry.build(
        GreenhouseSourceConfig(
            key="typed",
            kind="greenhouse",
            company_name="Typed",
            board_token="typed-board",
        )
    ),
    SourceAdapter,
)
