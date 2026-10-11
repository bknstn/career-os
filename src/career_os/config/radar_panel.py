"""Validated, content-addressed Career Radar collection panel manifests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from career_os.config.sources import ConfigurationError, SourceConfig, SourcesConfig
from career_os.domain.canonical import content_hash


class _UniqueKeyYamlLoader(yaml.SafeLoader):
    """Reject duplicate mapping keys so every manifest field has one meaning."""


def _construct_unique_mapping(
    loader: _UniqueKeyYamlLoader, node: MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in mapping:
                raise yaml.YAMLError("duplicate mapping key")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        except TypeError as error:
            raise yaml.YAMLError("mapping keys must be hashable") from error
    return mapping


_UniqueKeyYamlLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


class _PanelSourceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_key: str = Field(min_length=1)
    company_key: str = Field(min_length=1)
    source_kind: str = Field(min_length=1)


class _PanelDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    panel_version: str = Field(min_length=1)
    sources: list[_PanelSourceDocument] = Field(min_length=1)

    @model_validator(mode="after")
    def source_keys_are_unique(self) -> _PanelDocument:
        keys = [source.source_key for source in self.sources]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"duplicate source_key value(s): {', '.join(duplicates)}")
        return self


@dataclass(frozen=True, slots=True)
class RadarPanelSource:
    """One stable company/source identity in a versioned collection panel."""

    source_key: str
    company_key: str
    source_kind: str


@dataclass(frozen=True, slots=True)
class RadarPanelManifestIdentity:
    """Semantic identity of a manifest, independent of mutable source config."""

    panel_version: str
    sources: tuple[RadarPanelSource, ...]
    manifest_digest: str

    @property
    def canonical_manifest(self) -> dict[str, object]:
        """Return a fresh canonical JSON representation of the manifest."""
        return {
            "panel_version": self.panel_version,
            "sources": [
                {
                    "company_key": source.company_key,
                    "source_key": source.source_key,
                    "source_kind": source.source_kind,
                }
                for source in self.sources
            ],
        }


@dataclass(frozen=True, slots=True)
class RadarPanelManifest:
    """A validated panel with immutable semantic digests, independent of YAML layout."""

    panel_version: str
    sources: tuple[RadarPanelSource, ...]
    source_config_digests: Mapping[str, str]
    manifest_digest: str
    panel_digest: str

    @property
    def canonical_manifest(self) -> dict[str, object]:
        """Return a fresh JSON-ready canonical representation of the panel file."""
        return RadarPanelManifestIdentity(
            panel_version=self.panel_version,
            sources=self.sources,
            manifest_digest=self.manifest_digest,
        ).canonical_manifest

    @classmethod
    def load_identity(cls, path: str | Path) -> RadarPanelManifestIdentity:
        """Load and validate semantic manifest fields without source configuration."""
        manifest_path = Path(path)
        try:
            text = manifest_path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise ConfigurationError(
                f"Radar panel manifest does not exist: {manifest_path}"
            ) from error
        except OSError as error:
            raise ConfigurationError(
                f"Could not read Radar panel manifest: {manifest_path}"
            ) from error
        except UnicodeDecodeError as error:
            raise ConfigurationError(
                f"Radar panel manifest was not valid UTF-8: {manifest_path}"
            ) from error

        try:
            payload = yaml.load(text, Loader=_UniqueKeyYamlLoader)
        except yaml.YAMLError as error:
            raise ConfigurationError(
                f"Malformed YAML in Radar panel manifest: {manifest_path}"
            ) from error
        if not isinstance(payload, dict):
            raise ConfigurationError(
                "Radar panel manifest must contain a mapping at the document root"
            )
        try:
            document = _PanelDocument.model_validate(payload)
        except ValidationError as error:
            details = "; ".join(
                f"{'.'.join(str(part) for part in item['loc']) or 'root'}: {item['msg']}"
                for item in error.errors()
            )
            raise ConfigurationError(f"Invalid Radar panel manifest: {details}") from error

        sources = tuple(
            sorted(
                (
                    RadarPanelSource(
                        source_key=entry.source_key,
                        company_key=entry.company_key,
                        source_kind=entry.source_kind,
                    )
                    for entry in document.sources
                ),
                key=lambda source: source.source_key,
            )
        )
        canonical_manifest = {
            "panel_version": document.panel_version,
            "sources": [
                {
                    "company_key": source.company_key,
                    "source_key": source.source_key,
                    "source_kind": source.source_kind,
                }
                for source in sources
            ],
        }
        manifest_bytes = json.dumps(
            canonical_manifest,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return RadarPanelManifestIdentity(
            panel_version=document.panel_version,
            sources=sources,
            manifest_digest=hashlib.sha256(manifest_bytes).hexdigest(),
        )

    @classmethod
    def load(cls, path: str | Path, sources_config: SourcesConfig) -> RadarPanelManifest:
        """Load, validate, and digest a manifest against every declared typed source."""
        identity = cls.load_identity(path)

        configured_sources = {source.key: source for source in sources_config.sources}
        source_config_digests: dict[str, str] = {}
        for panel_source in identity.sources:
            source_config = configured_sources.get(panel_source.source_key)
            if source_config is None:
                raise ConfigurationError(
                    f"Radar panel source is not configured: {panel_source.source_key}"
                )
            if source_config.kind != panel_source.source_kind:
                raise ConfigurationError(
                    "Radar panel source kind does not match configuration: "
                    f"{panel_source.source_key}"
                )
            source_config_digests[panel_source.source_key] = content_hash(
                source_config.model_dump(mode="json")
            )

        immutable_source_digests = MappingProxyType(dict(sorted(source_config_digests.items())))
        panel_digest = content_hash(
            {
                "manifest_digest": identity.manifest_digest,
                "source_config_digests": dict(immutable_source_digests),
            }
        )
        return cls(
            panel_version=identity.panel_version,
            sources=identity.sources,
            source_config_digests=immutable_source_digests,
            manifest_digest=identity.manifest_digest,
            panel_digest=panel_digest,
        )

    def require_sources(self, sources: tuple[SourceConfig, ...]) -> None:
        """Fail if an explicit collection selection contains a source outside this panel."""
        panel_keys = {source.source_key for source in self.sources}
        outside = sorted({source.key for source in sources} - panel_keys)
        if outside:
            raise ConfigurationError(
                "selected source key(s) are outside the Radar panel: " + ", ".join(outside)
            )
