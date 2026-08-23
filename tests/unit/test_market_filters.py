"""Pure market schema and PostgreSQL query-construction tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from career_os.config.markets import MarketFilters, load_market_filters
from career_os.config.sources import ConfigurationError
from career_os.persistence.market_queries import build_market_job_ids_query

EXAMPLE_PATH = Path("config/market.example.yaml")
INITIAL_PATH = Path("config/market.initial.yaml")


def test_checked_in_market_example_validates_against_current_schema() -> None:
    filters = load_market_filters(EXAMPLE_PATH)

    assert filters.name == "senior-backend-poland-eu"
    assert filters.role_families == ("backend_engineering", "platform_engineering")
    assert filters.country_codes == ("PL",)
    assert filters.include_remote is True
    assert filters.remote_scopes == ("poland", "eu", "europe")


def test_checked_in_initial_market_pins_the_real_company_sample() -> None:
    filters = load_market_filters(INITIAL_PATH)

    assert filters.name == "senior-backend-platform-poland-eu-initial-20"
    assert len(filters.company_names) == 20
    assert {"gitlab", "hostinger", "kestra"} <= set(filters.company_names)


def test_market_file_normalizes_filters_and_forbids_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "market.yaml"
    path.write_text(
        """
        market:
          name: backend-pl
          title_include: [Backend, Platform]
          title_exclude: [Frontend]
          role_families: [backend_engineering]
          seniorities: [senior]
          country_codes: [pl]
          include_remote: true
          remote_scopes: [EU, Poland]
          company_names: [Example Inc.]
          active_only: true
        """,
        encoding="utf-8",
    )

    filters = load_market_filters(path)

    assert filters.title_include == ("backend", "platform")
    assert filters.country_codes == ("PL",)
    assert filters.remote_scopes == ("eu", "poland")
    assert filters.company_names == ("example inc.",)

    path.write_text("market: {name: all, unexpected: true}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="unexpected"):
        load_market_filters(path)


def test_filter_normalization_matches_postgresql_lower_not_unicode_casefold() -> None:
    filters = MarketFilters(name="unicode", company_names=("STRAẞE GmbH",))

    assert filters.company_names == ("straße gmbh",)
    assert filters.company_names != ("strasse gmbh",)


@pytest.mark.parametrize(
    "values",
    [
        {"name": "duplicate", "title_include": ["Backend", "backend"]},
        {"name": "duplicate", "country_codes": ["PL", "pl"]},
        {"name": "implicit-global", "include_remote": True, "remote_scopes": ["EU"]},
        {"name": "missing-scope", "country_codes": ["PL"], "include_remote": True},
        {"name": "unused-scope", "remote_scopes": ["EU"]},
    ],
)
def test_market_schema_rejects_ambiguous_or_duplicate_filters(values: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        MarketFilters.model_validate(values)


def test_query_is_join_free_and_uses_exists_any_include_and_none_exclude() -> None:
    filters = MarketFilters(
        name="backend-pl",
        title_include=("backend", "platform"),
        title_exclude=("frontend", "mobile"),
        country_codes=("PL",),
        include_remote=True,
        remote_scopes=("eu", "poland"),
    )

    sql = str(
        build_market_job_ids_query(filters).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert " JOIN " not in sql
    assert "EXISTS (SELECT 1" in sql
    assert "strpos(lower(job_postings.title_normalized), 'backend') > 0" in sql
    assert "strpos(lower(job_postings.title_normalized), 'platform') > 0" in sql
    assert "strpos(lower(job_postings.title_normalized), 'frontend') = 0" in sql
    assert "job_postings.workplace_type = 'remote'" in sql
    assert "lower(btrim(job_postings.remote_scope)) IN ('eu', 'poland')" in sql


def test_empty_filter_lists_add_no_restriction_and_active_only_is_explicit() -> None:
    active_sql = str(
        build_market_job_ids_query(MarketFilters(name="active")).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    all_sql = str(
        build_market_job_ids_query(MarketFilters(name="all", active_only=False)).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "job_postings.status = 'active'" in active_sql
    assert "WHERE" not in all_sql
