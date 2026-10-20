"""Adapter construction boundary and concrete public-source clients."""

from career_os.adapters.ashby import (
    AshbyAdapter,
    AshbyAdapterError,
    AshbyHttpClient,
    AshbyHttpError,
    AshbyHttpResponse,
    AshbyTransportError,
)
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
from career_os.adapters.research_http import (
    ResearchFetcher,
    ResearchFetchError,
    ResearchFetchResult,
    ResearchRequestBudget,
)
from career_os.adapters.workable_public import (
    WorkablePublicAdapter,
    WorkablePublicAdapterError,
    WorkablePublicHttpClient,
    WorkablePublicHttpError,
    WorkablePublicHttpResponse,
    WorkablePublicTransportError,
)

__all__ = [
    "AdapterFactory",
    "AdapterRegistration",
    "AdapterRegistry",
    "AdapterRegistryError",
    "AshbyAdapter",
    "AshbyAdapterError",
    "AshbyHttpClient",
    "AshbyHttpError",
    "AshbyHttpResponse",
    "AshbyTransportError",
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
    "ResearchFetchError",
    "ResearchFetchResult",
    "ResearchFetcher",
    "ResearchRequestBudget",
    "WorkablePublicAdapter",
    "WorkablePublicAdapterError",
    "WorkablePublicHttpClient",
    "WorkablePublicHttpError",
    "WorkablePublicHttpResponse",
    "WorkablePublicTransportError",
]
