"""Strict allowlisted source registry for the Career Radar research stream."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from career_os.domain.research import normalize_research_url


class ResearchConfigurationError(ValueError):
    """Raised when the checked-in research source registry is unsafe or invalid."""


class _UniqueKeyYamlLoader(yaml.SafeLoader):
    """Reject duplicate fields before they can acquire ambiguous meanings."""


def _construct_unique_mapping(
    loader: _UniqueKeyYamlLoader, node: MappingNode, deep: bool = False
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in result:
                raise yaml.YAMLError("duplicate mapping key")
            result[key] = loader.construct_object(value_node, deep=deep)
        except TypeError as error:
            raise yaml.YAMLError("mapping keys must be hashable") from error
    return result


_UniqueKeyYamlLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)

DiscoveryMethod = Literal["rss", "atom", "sitemap", "html_index", "single_document"]


class ResearchDocumentAlias(BaseModel):
    """An official URL that is the same publication as one selected body URL."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    alias_url: Annotated[str, Field(min_length=1)]
    canonical_url: Annotated[str, Field(min_length=1)]


class ResearchSource(BaseModel):
    """One publisher boundary with finite official URLs and request limits."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    key: Annotated[str, Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")]
    publisher: Annotated[str, Field(min_length=1)]
    enabled: bool
    allowed_hosts: tuple[Annotated[str, Field(min_length=1)], ...] = Field(min_length=1)
    discovery_methods: tuple[DiscoveryMethod, ...] = ()
    discovery_urls: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    candidate_document_urls: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    document_aliases: tuple[ResearchDocumentAlias, ...] = ()
    max_response_bytes: int = Field(default=12 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)

    @model_validator(mode="after")
    def validate_boundary(self) -> ResearchSource:
        hosts = [host.rstrip(".").lower() for host in self.allowed_hosts]
        if any("/" in host or ":" in host or "@" in host or host != host.strip() for host in hosts):
            raise ValueError("allowed_hosts must contain bare DNS names")
        if len(hosts) != len(set(hosts)):
            raise ValueError("allowed_hosts must be unique")
        if len(set(self.discovery_urls)) != len(self.discovery_urls):
            raise ValueError("discovery_urls must be unique")
        if len(set(self.candidate_document_urls)) != len(self.candidate_document_urls):
            raise ValueError("candidate_document_urls must be unique")
        aliases = [alias.alias_url for alias in self.document_aliases]
        if len(aliases) != len(set(aliases)):
            raise ValueError("document alias URLs must be unique")
        candidates = {
            normalize_research_url(url, allowed_hosts=tuple(hosts))
            for url in self.candidate_document_urls
        }
        for url in (*self.discovery_urls, *self.candidate_document_urls):
            normalize_research_url(url, allowed_hosts=tuple(hosts))
        for alias in self.document_aliases:
            normalized_alias = normalize_research_url(alias.alias_url, allowed_hosts=tuple(hosts))
            normalized_canonical = normalize_research_url(
                alias.canonical_url, allowed_hosts=tuple(hosts)
            )
            if normalized_alias == normalized_canonical:
                raise ValueError("a document alias must differ from its canonical URL")
            if normalized_canonical not in candidates:
                raise ValueError("document alias targets must be configured candidate URLs")
        if self.enabled and (not self.discovery_methods or not self.discovery_urls):
            raise ValueError("enabled sources require at least one discovery method and URL")
        if not self.enabled and (self.discovery_methods or self.discovery_urls):
            raise ValueError("disabled sources must not be contacted or define discovery URLs")
        return self


class ResearchSourceRegistryDocument(BaseModel):
    """The complete immutable registry document."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    version: Annotated[str, Field(min_length=1)]
    sources: tuple[ResearchSource, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def source_keys_are_unique(self) -> ResearchSourceRegistryDocument:
        keys = [source.key for source in self.sources]
        if len(keys) != len(set(keys)):
            raise ValueError("source keys must be unique")
        return self


class ResearchSourceRegistry:
    """Validated registry with a stable content digest for collection provenance."""

    def __init__(self, document: ResearchSourceRegistryDocument) -> None:
        self.document = document
        self._sources = {source.key: source for source in document.sources}
        self.digest = hashlib.sha256(
            json.dumps(
                document.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()

    @property
    def version(self) -> str:
        return self.document.version

    @property
    def sources(self) -> tuple[ResearchSource, ...]:
        return tuple(self.document.sources)

    @property
    def enabled_sources(self) -> tuple[ResearchSource, ...]:
        return tuple(source for source in self.document.sources if source.enabled)

    def get(self, key: str) -> ResearchSource:
        try:
            return self._sources[key]
        except KeyError as error:
            raise ResearchConfigurationError(f"Unknown research source: {key}") from error

    def canonical_document_url(self, source_key: str, value: str) -> str:
        """Return the registry-selected primary representation for an official alias."""
        source = self.get(source_key)
        allowed_hosts = tuple(host.rstrip(".").lower() for host in source.allowed_hosts)
        normalized = normalize_research_url(value, allowed_hosts=allowed_hosts)
        aliases = {
            normalize_research_url(
                alias.alias_url, allowed_hosts=allowed_hosts
            ): normalize_research_url(alias.canonical_url, allowed_hosts=allowed_hosts)
            for alias in source.document_aliases
        }
        return aliases.get(normalized, normalized)

    @classmethod
    def load(cls, path: str | Path) -> ResearchSourceRegistry:
        """Read and validate a local YAML file without performing network activity."""
        registry_path = Path(path)
        try:
            raw = registry_path.read_text(encoding="utf-8")
            payload = yaml.load(raw, Loader=_UniqueKeyYamlLoader)
        except FileNotFoundError as error:
            raise ResearchConfigurationError(f"Registry does not exist: {registry_path}") from error
        except (OSError, UnicodeDecodeError) as error:
            raise ResearchConfigurationError(f"Could not read registry: {registry_path}") from error
        except yaml.YAMLError as error:
            raise ResearchConfigurationError("Malformed YAML in research registry") from error
        if not isinstance(payload, Mapping):
            raise ResearchConfigurationError("Research registry root must be a mapping")
        try:
            return cls(ResearchSourceRegistryDocument.model_validate(payload))
        except ValidationError as error:
            details = "; ".join(
                f"{'.'.join(str(part) for part in item['loc']) or 'root'}: {item['msg']}"
                for item in error.errors()
            )
            raise ResearchConfigurationError(f"Invalid research registry: {details}") from error
