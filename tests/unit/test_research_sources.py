"""The checked-in research registry is strict, finite, and read-only to load."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from career_os.config.research_sources import (
    ResearchSource,
    ResearchSourceRegistry,
    _UniqueKeyYamlLoader,
)


def test_checked_in_registry_has_eight_enabled_pilot_publishers_and_four_disabled() -> None:
    project_root = Path(__file__).resolve().parents[2]
    registry = ResearchSourceRegistry.load(project_root / "config/radar/research.sources.v1.yaml")

    assert registry.version == "research-sources-v2"
    assert len(registry.enabled_sources) == 8
    assert len(registry.sources) == 12
    assert {source.key for source in registry.sources if not source.enabled} == {
        "world_economic_forum",
        "goldman_sachs",
        "google_deepmind",
        "bls",
    }
    assert sum(len(source.candidate_document_urls) for source in registry.enabled_sources) == 20
    assert (
        registry.canonical_document_url(
            "linkedin_economic_graph",
            "https://economicgraph.linkedin.com/research/labor-market-report-2026",
        )
        == registry.get("linkedin_economic_graph").candidate_document_urls[0]
    )
    assert registry.get("mckinsey_state_of_ai").candidate_document_urls == ()
    assert len(registry.digest) == 64


def test_registry_loader_rejects_duplicate_yaml_keys_without_writing_a_file() -> None:
    with pytest.raises(yaml.YAMLError, match="duplicate mapping key"):
        yaml.load("version: first\nversion: second\nsources: []\n", Loader=_UniqueKeyYamlLoader)


def test_source_registry_rejects_unlisted_hosts_and_disabled_discovery_urls() -> None:
    with pytest.raises(ValidationError, match="outside the exact source allowlist"):
        ResearchSource.model_validate(
            {
                "key": "sample",
                "publisher": "Sample Publisher",
                "enabled": True,
                "allowed_hosts": ["publisher.example"],
                "discovery_methods": ["html_index"],
                "discovery_urls": ["https://other.example/research"],
            }
        )
    with pytest.raises(ValidationError, match="disabled sources must not"):
        ResearchSource.model_validate(
            {
                "key": "sample",
                "publisher": "Sample Publisher",
                "enabled": False,
                "allowed_hosts": ["publisher.example"],
                "discovery_methods": [],
                "discovery_urls": ["https://publisher.example/research"],
            }
        )
