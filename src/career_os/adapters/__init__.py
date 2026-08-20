"""Adapter construction boundary and concrete public-source clients."""

from career_os.adapters.greenhouse import (
    GreenhouseAdapter,
    GreenhouseAdapterError,
    GreenhouseHttpClient,
    GreenhouseHttpError,
    GreenhouseHttpResponse,
    GreenhouseTransportError,
)
from career_os.adapters.lever import (
    LeverAdapter,
    LeverAdapterError,
    LeverHttpClient,
    LeverHttpError,
    LeverHttpResponse,
    LeverTransportError,
)
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
    "GreenhouseAdapter",
    "GreenhouseAdapterError",
    "GreenhouseHttpClient",
    "GreenhouseHttpError",
    "GreenhouseHttpResponse",
    "GreenhouseTransportError",
    "LeverAdapter",
    "LeverAdapterError",
    "LeverHttpClient",
    "LeverHttpError",
    "LeverHttpResponse",
    "LeverTransportError",
]
