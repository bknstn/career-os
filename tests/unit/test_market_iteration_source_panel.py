"""Offline invariants for the reviewed D8 source panel."""

from pathlib import Path

from career_os.config.markets import load_market_filters
from career_os.config.sources import load_sources_config

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_market_iteration_panel_is_explicit_unique_and_adapter_supported() -> None:
    sources = load_sources_config(PROJECT_ROOT / "config/sources.market-iteration.yaml").sources
    enabled = tuple(source for source in sources if source.enabled)
    disabled = tuple(source for source in sources if not source.enabled)

    assert len(sources) == 42
    assert len(enabled) == 20
    assert len(disabled) == 22
    assert len({source.key for source in sources}) == 42
    assert len({source.company_name.casefold() for source in sources}) == 42
    assert {source.kind for source in enabled} == {"ashby", "greenhouse"}


def test_market_iteration_panel_retains_only_productive_d1_boards() -> None:
    initial = load_sources_config(PROJECT_ROOT / "config/sources.initial-market.yaml")
    revised = load_sources_config(PROJECT_ROOT / "config/sources.market-iteration.yaml")
    initial_keys = {source.key for source in initial.sources}
    enabled_keys = {source.key for source in revised.sources if source.enabled}
    disabled_keys = {source.key for source in revised.sources if not source.enabled}

    retained = {"gitlab-greenhouse", "hostinger-ashby", "kestra-ashby"}
    assert initial_keys & enabled_keys == retained
    assert disabled_keys & initial_keys == initial_keys - retained


def test_market_iteration_panel_applies_only_the_five_predeclared_d8_replacements() -> None:
    sources = load_sources_config(PROJECT_ROOT / "config/sources.market-iteration.yaml")
    enabled_keys = {source.key for source in sources.sources if source.enabled}
    disabled_keys = {source.key for source in sources.sources if not source.enabled}

    assert {
        "p2p-org-ashby",
        "checkly-ashby",
        "chattermill-ashby",
        "hauler-hero-ashby",
        "wundergraph-ashby",
    } <= enabled_keys
    assert {
        "apollo-io-greenhouse",
        "tripadvisor-greenhouse",
        "vonage-greenhouse",
        "zowie-ashby",
        "skelar-ashby",
    } <= disabled_keys


def test_market_iteration_filter_tracks_the_revised_companies_and_calibrated_titles() -> None:
    sources = load_sources_config(PROJECT_ROOT / "config/sources.market-iteration.yaml")
    market = load_market_filters(PROJECT_ROOT / "config/market.market-iteration.yaml")

    assert market.name == "senior-backend-platform-poland-eu-market-iteration-v2"
    assert market.title_include == ("backend", "back-end", "platform", "infrastructure")
    assert market.seniorities == ("senior", "lead", "staff", "principal")
    assert set(market.company_names) == {
        source.company_name.casefold() for source in sources.sources if source.enabled
    }
