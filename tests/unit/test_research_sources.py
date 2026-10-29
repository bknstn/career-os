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


def test_registry_has_eight_research_publishers_one_guidance_and_four_disabled() -> None:
    project_root = Path(__file__).resolve().parents[2]
    registry = ResearchSourceRegistry.load(project_root / "config/radar/research.sources.v1.yaml")

    assert registry.version == "research-sources-v4"
    assert len(registry.enabled_sources) == 9
    assert len(registry.sources) == 13
    assert {source.key for source in registry.sources if not source.enabled} == {
        "world_economic_forum",
        "goldman_sachs",
        "google_deepmind",
        "bls",
    }
    assert sum(len(source.candidate_document_urls) for source in registry.enabled_sources) == 21
    raised_cap_keys = {"openai_economic_research", "deloitte_state_of_ai"}
    assert {source.key: source.max_response_bytes for source in registry.sources} == {
        source.key: (25 * 1024 * 1024 if source.key in raised_cap_keys else 12 * 1024 * 1024)
        for source in registry.sources
    }
    anthropic = registry.get("anthropic_economics")
    assert anthropic.excluded_document_urls == ("https://www.anthropic.com/economic-index",)
    assert registry.excluded_document_urls("anthropic_economics") == frozenset(
        {"https://www.anthropic.com/economic-index"}
    )
    amazon = registry.get("amazon_engineering_guidance")
    assert amazon.publisher == "Amazon · Employer guidance"
    assert amazon.allowed_hosts == ("www.aboutamazon.com",)
    assert amazon.minimum_request_interval_seconds == 10
    assert amazon.discovery_required_term_groups[0] == ("ai", "artificial intelligence")
    role_terms = amazon.discovery_required_term_groups[1]
    assert "developers" in role_terms
    assert "software engineers" in role_terms
    assert "software-engineering" in role_terms
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


def test_source_registry_validates_excluded_document_urls_and_contradictions() -> None:
    base = {
        "key": "sample",
        "publisher": "Sample Publisher",
        "enabled": True,
        "allowed_hosts": ["publisher.example"],
        "discovery_methods": ["html_index"],
        "discovery_urls": ["https://publisher.example/research"],
    }

    with pytest.raises(ValidationError, match="outside the exact source allowlist"):
        ResearchSource.model_validate(
            {**base, "excluded_document_urls": ["https://other.example/dashboard"]}
        )
    with pytest.raises(ValidationError, match="unique after URL normalization"):
        ResearchSource.model_validate(
            {
                **base,
                "excluded_document_urls": [
                    "https://PUBLISHER.example/dashboard#first",
                    "https://publisher.example/dashboard#second",
                ],
            }
        )
    with pytest.raises(ValidationError, match="must not conflict with candidate URLs"):
        ResearchSource.model_validate(
            {
                **base,
                "candidate_document_urls": ["https://publisher.example/report"],
                "excluded_document_urls": ["https://publisher.example/report"],
            }
        )
    with pytest.raises(ValidationError, match="must not conflict with candidate URLs"):
        ResearchSource.model_validate(
            {
                **base,
                "candidate_document_urls": ["https://publisher.example/report"],
                "document_aliases": [
                    {
                        "alias_url": "https://publisher.example/report-page",
                        "canonical_url": "https://publisher.example/report",
                    }
                ],
                "excluded_document_urls": ["https://publisher.example/report-page"],
            }
        )


@pytest.mark.parametrize("interval", [-0.1, 60.1])
def test_source_registry_rejects_invalid_per_source_request_intervals(interval: float) -> None:
    with pytest.raises(ValidationError):
        ResearchSource.model_validate(
            {
                "key": "sample",
                "publisher": "Sample Publisher",
                "enabled": True,
                "allowed_hosts": ["publisher.example"],
                "discovery_methods": ["html_index"],
                "discovery_urls": ["https://publisher.example/research"],
                "minimum_request_interval_seconds": interval,
            }
        )
