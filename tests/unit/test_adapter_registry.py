"""Tests for explicit configuration-to-adapter registry resolution."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import pytest

from career_os.adapters import AdapterRegistration, AdapterRegistry, AdapterRegistryError
from career_os.config.sources import GreenhouseSourceConfig, LeverSourceConfig, SourcesConfig
from career_os.domain import FetchContext, FetchEvent, ScanOutcome, ScanStatus, SourceAdapter


class _FakeAdapter:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.validate_calls = 0
        self.fetch_calls = 0

    async def validate(self) -> None:
        self.validate_calls += 1

    async def fetch(self, context: FetchContext) -> AsyncIterator[FetchEvent]:
        self.fetch_calls += 1
        yield ScanOutcome(ScanStatus.COMPLETE)


def _greenhouse_config() -> GreenhouseSourceConfig:
    return GreenhouseSourceConfig(
        key="example",
        kind="greenhouse",
        company_name="Example",
        board_token="example-board",
    )


def _lever_config() -> LeverSourceConfig:
    return LeverSourceConfig(
        key="lever-example",
        kind="lever",
        company_name="Lever Example",
        site="lever-example",
    )


def _greenhouse_registration(
    factory: Callable[[GreenhouseSourceConfig], SourceAdapter],
) -> AdapterRegistration:
    return AdapterRegistration.for_config(
        kind="greenhouse", config_type=GreenhouseSourceConfig, factory=factory
    )


def _lever_registration(
    factory: Callable[[LeverSourceConfig], SourceAdapter],
) -> AdapterRegistration:
    return AdapterRegistration.for_config(
        kind="lever", config_type=LeverSourceConfig, factory=factory
    )


def test_build_resolves_a_validated_config_without_invoking_adapter_network_methods() -> None:
    seen_configs: list[GreenhouseSourceConfig] = []
    created_adapters: list[_FakeAdapter] = []

    def factory(source_config: GreenhouseSourceConfig) -> SourceAdapter:
        seen_configs.append(source_config)
        adapter = _FakeAdapter(source_config.kind)
        created_adapters.append(adapter)
        return adapter

    registry = AdapterRegistry((_greenhouse_registration(factory),))

    adapter = registry.build(_greenhouse_config())

    assert isinstance(adapter, SourceAdapter)
    assert seen_configs == [_greenhouse_config()]
    assert created_adapters[0].validate_calls == 0
    assert created_adapters[0].fetch_calls == 0
    assert registry.registered_kinds == frozenset({"greenhouse"})


def test_registry_rejects_an_unknown_registration_kind() -> None:
    with pytest.raises(AdapterRegistryError, match="unknown adapter registration kind"):
        AdapterRegistration.for_config(
            kind="unknown",
            config_type=GreenhouseSourceConfig,
            factory=lambda source_config: _FakeAdapter(source_config.kind),
        )


def test_registry_rejects_duplicate_registration_kinds() -> None:
    def factory(source_config: GreenhouseSourceConfig) -> SourceAdapter:
        return _FakeAdapter(source_config.kind)

    registration = _greenhouse_registration(factory)

    with pytest.raises(AdapterRegistryError, match="duplicate adapter registration kind"):
        AdapterRegistry((registration, registration))


def test_build_rejects_a_missing_registered_kind_before_invoking_any_factory() -> None:
    factory_calls = 0

    def factory(source_config: GreenhouseSourceConfig) -> SourceAdapter:
        nonlocal factory_calls
        factory_calls += 1
        return _FakeAdapter(source_config.kind)

    registry = AdapterRegistry((_greenhouse_registration(factory),))

    with pytest.raises(AdapterRegistryError, match="no adapter factory registered for kind: lever"):
        registry.build(_lever_config())

    assert factory_calls == 0


def test_build_rejects_a_factory_that_returns_a_mismatched_adapter_kind() -> None:
    def mismatched_factory(source_config: GreenhouseSourceConfig) -> SourceAdapter:
        return _FakeAdapter("lever")

    registry = AdapterRegistry((_greenhouse_registration(mismatched_factory),))

    with pytest.raises(AdapterRegistryError, match="adapter factory kind mismatch"):
        registry.build(_greenhouse_config())


def test_build_all_validates_every_config_kind_before_constructing_any_adapter() -> None:
    factory_calls = 0

    def factory(source_config: GreenhouseSourceConfig) -> SourceAdapter:
        nonlocal factory_calls
        factory_calls += 1
        return _FakeAdapter(source_config.kind)

    registry = AdapterRegistry((_greenhouse_registration(factory),))
    sources_config = SourcesConfig(sources=[_greenhouse_config(), _lever_config()])

    with pytest.raises(
        AdapterRegistryError, match=r"no adapter factory registered for kind\(s\): lever"
    ):
        registry.build_all(sources_config)

    assert factory_calls == 0


def test_build_all_preserves_source_order_and_task_seven_contract() -> None:
    def greenhouse_factory(source_config: GreenhouseSourceConfig) -> SourceAdapter:
        return _FakeAdapter(source_config.kind)

    def lever_factory(source_config: LeverSourceConfig) -> SourceAdapter:
        return _FakeAdapter(source_config.kind)

    registry = AdapterRegistry(
        (
            _greenhouse_registration(greenhouse_factory),
            _lever_registration(lever_factory),
        )
    )
    adapters = registry.build_all(SourcesConfig(sources=[_greenhouse_config(), _lever_config()]))

    assert [adapter.kind for adapter in adapters] == ["greenhouse", "lever"]
    assert all(isinstance(adapter, SourceAdapter) for adapter in adapters)


def test_fake_adapter_remains_usable_with_the_scan_contract() -> None:
    async def collect_terminal_event() -> FetchEvent:
        adapter = _FakeAdapter("greenhouse")
        context = FetchContext(run_id="run-1", fetched_at=datetime.now(UTC))
        return await anext(adapter.fetch(context))

    assert asyncio.run(collect_terminal_event()) == ScanOutcome(ScanStatus.COMPLETE)


def test_build_rejects_a_kind_config_class_mismatch_without_calling_its_factory() -> None:
    factory_calls = 0

    def lever_factory(source_config: LeverSourceConfig) -> SourceAdapter:
        nonlocal factory_calls
        factory_calls += 1
        return _FakeAdapter(source_config.kind)

    registration = AdapterRegistration.for_config(
        kind="greenhouse", config_type=LeverSourceConfig, factory=lever_factory
    )
    registry = AdapterRegistry((registration,))

    with pytest.raises(AdapterRegistryError, match="configuration type mismatch"):
        registry.build(_greenhouse_config())

    assert factory_calls == 0


def test_build_rejects_a_factory_returning_a_non_adapter() -> None:
    def invalid_factory(source_config: GreenhouseSourceConfig) -> object:
        return object()

    registration = AdapterRegistration.for_config(
        kind="greenhouse",
        config_type=GreenhouseSourceConfig,
        factory=invalid_factory,  # type: ignore[arg-type]
    )
    registry = AdapterRegistry((registration,))

    with pytest.raises(AdapterRegistryError, match="returned an invalid adapter"):
        registry.build(_greenhouse_config())
