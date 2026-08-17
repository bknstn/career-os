"""Adapter construction boundary; concrete source clients arrive in later tasks."""

from career_os.adapters.registry import (
    AdapterFactory,
    AdapterRegistration,
    AdapterRegistry,
    AdapterRegistryError,
)

__all__ = [
    "AdapterFactory",
    "AdapterRegistration",
    "AdapterRegistry",
    "AdapterRegistryError",
]
