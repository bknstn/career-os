"""Offline invariants for the reviewed D2 source panel."""

from pathlib import Path

from career_os.config.sources import load_sources_config

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_market_iteration_panel_is_explicit_unique_and_adapter_supported() -> None:
    sources = load_sources_config(PROJECT_ROOT / "config/sources.market-iteration.yaml").sources

    assert len(sources) == 20
    assert len({source.key for source in sources}) == 20
    assert len({source.company_name.casefold() for source in sources}) == 20
    assert {source.kind for source in sources} == {"ashby", "greenhouse"}


def test_market_iteration_panel_retains_only_productive_d1_boards() -> None:
    initial = load_sources_config(PROJECT_ROOT / "config/sources.initial-market.yaml")
    revised = load_sources_config(PROJECT_ROOT / "config/sources.market-iteration.yaml")
    initial_keys = {source.key for source in initial.sources}
    revised_keys = {source.key for source in revised.sources}

    retained = {"gitlab-greenhouse", "hostinger-ashby", "kestra-ashby"}
    assert initial_keys & revised_keys == retained
