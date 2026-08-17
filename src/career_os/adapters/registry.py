"""Explicit, immutable resolution of validated source configs to adapters."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeVar

from career_os.config.sources import (
    KNOWN_SOURCE_KINDS,
    SourceConfig,
    SourceConfigBase,
    SourcesConfig,
)
from career_os.domain import SourceAdapter

ConfigT = TypeVar("ConfigT", bound=SourceConfigBase)
type AdapterFactory[Config: SourceConfigBase] = Callable[[Config], SourceAdapter]
type _ErasedAdapterFactory = Callable[[SourceConfigBase], SourceAdapter]


class AdapterRegistryError(ValueError):
    """Raised when a validated source cannot safely resolve to an adapter."""


@dataclass(frozen=True, slots=True)
class AdapterRegistration:
    """A config-class-specific factory registered under one source kind."""

    kind: str
    config_type: type[SourceConfigBase]
    _factory: _ErasedAdapterFactory

    def __post_init__(self) -> None:
        if self.kind not in KNOWN_SOURCE_KINDS:
            raise AdapterRegistryError(f"unknown adapter registration kind: {self.kind}")

    @classmethod
    def for_config(
        cls,
        *,
        kind: str,
        config_type: type[ConfigT],
        factory: AdapterFactory[ConfigT],
    ) -> AdapterRegistration:
        """Associate a concrete config class with its naturally typed factory."""

        def erased_factory(source_config: SourceConfigBase) -> SourceAdapter:
            if not isinstance(source_config, config_type):
                raise AdapterRegistryError(
                    "adapter registration configuration type mismatch: "
                    f"registered={config_type.__name__}, received={type(source_config).__name__}"
                )
            return factory(source_config)

        return cls(kind=kind, config_type=config_type, _factory=erased_factory)

    def build(self, source_config: SourceConfig) -> SourceAdapter:
        """Verify the config class before invoking the registered factory."""
        if not isinstance(source_config, self.config_type):
            raise AdapterRegistryError(
                "adapter registration configuration type mismatch: "
                f"registered={self.config_type.__name__}, received={type(source_config).__name__}"
            )
        return self._factory(source_config)


class AdapterRegistry:
    """Resolve already-validated configurations without global registrations.

    Factories construct adapters only; they must not perform network I/O.  The
    registry deliberately does not call adapter ``validate`` or ``fetch`` so
    all configuration and factory-contract failures happen before collection.
    """

    def __init__(self, registrations: Iterable[AdapterRegistration]) -> None:
        registrations_by_kind: dict[str, AdapterRegistration] = {}
        for registration in registrations:
            if registration.kind in registrations_by_kind:
                raise AdapterRegistryError(
                    f"duplicate adapter registration kind: {registration.kind}"
                )
            registrations_by_kind[registration.kind] = registration
        self._registrations: Mapping[str, AdapterRegistration] = MappingProxyType(
            registrations_by_kind
        )

    @property
    def registered_kinds(self) -> frozenset[str]:
        """Return the kinds currently available for explicit construction."""
        return frozenset(self._registrations)

    def validate_sources(self, sources_config: SourcesConfig) -> None:
        """Ensure every validated configured kind has a registered factory.

        This checks the complete file before any factory is invoked, preventing
        a partial set of constructed adapters when a later source is unknown.
        """
        missing_kinds = sorted(
            {
                source.kind
                for source in sources_config.sources
                if source.kind not in self._registrations
            }
        )
        if missing_kinds:
            joined_kinds = ", ".join(missing_kinds)
            raise AdapterRegistryError(f"no adapter factory registered for kind(s): {joined_kinds}")

    def build(self, source_config: SourceConfig) -> SourceAdapter:
        """Construct one adapter and verify that it matches the config kind."""
        registration = self._registrations.get(source_config.kind)
        if registration is None:
            raise AdapterRegistryError(
                f"no adapter factory registered for kind: {source_config.kind}"
            )

        adapter = registration.build(source_config)
        if not isinstance(adapter, SourceAdapter):
            raise AdapterRegistryError(
                f"adapter factory for kind {source_config.kind} returned an invalid adapter"
            )
        if adapter.kind != source_config.kind:
            raise AdapterRegistryError(
                "adapter factory kind mismatch: "
                f"registered/configured={source_config.kind}, returned={adapter.kind}"
            )
        return adapter

    def build_all(self, sources_config: SourcesConfig) -> tuple[SourceAdapter, ...]:
        """Construct adapters for a fully covered source configuration file."""
        self.validate_sources(sources_config)
        return tuple(self.build(source_config) for source_config in sources_config.sources)
