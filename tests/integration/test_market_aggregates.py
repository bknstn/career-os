"""PostgreSQL reconciliation coverage for market filters and aggregates."""

from __future__ import annotations

import os
from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from career_os.application.market import MarketAggregateService, MarketDataConsistencyError
from career_os.config.markets import MarketFilters
from career_os.db import (
    create_database_engine,
    create_session_factory,
    database_url_from_settings,
    session_scope,
)
from career_os.normalization.jobs import JOB_NORMALIZER_VERSION
from career_os.persistence.integration_safety import (
    CAREER_OS_TABLE_NAMES,
    assert_disposable_test_database,
    assert_initial_schema_is_absent,
)
from career_os.persistence.models import JobCompensation, JobLocation, JobPosting, JobSkill, Source
from career_os.settings import load_settings

pytestmark = pytest.mark.integration
TAXONOMY_VERSION = "1.0.0"


@pytest.fixture
def session_factory() -> Generator[sessionmaker[Session], None, None]:
    if os.environ.get("RUN_DB_INTEGRATION") != "1":
        pytest.skip("set RUN_DB_INTEGRATION=1 to run PostgreSQL integration tests")
    settings = load_settings()
    if settings.database_url is None:
        pytest.skip("set CAREER_OS_DATABASE_URL to run PostgreSQL integration tests")
    project_root = Path(__file__).resolve().parents[2]
    alembic_config = Config(str(project_root / "alembic.ini"))
    engine = create_database_engine(settings)
    upgraded = False
    try:
        assert_disposable_test_database(database_url_from_settings(settings))
        assert_initial_schema_is_absent(inspect(engine).get_table_names())
        command.upgrade(alembic_config, "head")
        upgraded = True
        yield create_session_factory(engine)
    finally:
        _return_database_to_empty(engine, alembic_config, upgraded)


def test_every_market_aggregate_reconciles_to_exact_postgresql_job_ids(
    session_factory: sessionmaker[Session],
) -> None:
    ids = _seed_market(session_factory)
    filters = MarketFilters(
        name="backend-pl-eu",
        title_include=("backend", "platform"),
        title_exclude=("frontend",),
        role_families=("backend_engineering", "platform_engineering"),
        seniorities=("senior", "staff"),
        country_codes=("PL",),
        include_remote=True,
        remote_scopes=("Poland", "EU"),
    )

    with session_scope(session_factory) as session:
        result = MarketAggregateService(
            session, taxonomy_version=TAXONOMY_VERSION, normalizer_version=JOB_NORMALIZER_VERSION
        ).calculate(filters)

    expected = tuple(sorted((ids["local"], ids["remote_eu"], ids["empty_description"])))
    described = tuple(sorted((ids["local"], ids["remote_eu"])))
    assert result.matching_job_ids == expected
    assert result.matching_job_count == 3
    assert result.skill_denominator_job_ids == described
    assert result.company_count == 2
    assert {bucket.value: bucket.job_ids for bucket in result.skills} == {
        "python": described,
        "sql": (ids["local"],),
    }
    assert all(bucket.denominator_job_ids == described for bucket in result.skills)
    assert {bucket.value: bucket.job_ids for bucket in result.countries} == {
        "PL": tuple(sorted((ids["local"], ids["empty_description"])))
    }
    assert result.countries[0].denominator_job_ids == result.countries[0].job_ids
    assert result.compensation_job_ids == tuple(sorted((ids["local"], ids["remote_eu"])))
    assert result.compensation_coverage == Decimal("2") / Decimal("3")

    groups = {
        (item.component_type, item.currency, item.interval, item.location_tier): item
        for item in result.compensation
    }
    assert set(groups) == {
        ("base_salary", "EUR", "year", "EU"),
        ("base_salary", "USD", "year", "Poland"),
    }
    usd = groups[("base_salary", "USD", "year", "Poland")]
    assert usd.component_count == 2
    assert usd.job_count == 1
    assert usd.job_ids == (ids["local"],)
    assert (usd.min_value, usd.max_value) == (Decimal("100000.00"), Decimal("150000.00"))
    assert all(item.denominator_job_ids == expected for item in result.compensation)
    assert ids["remote_global"] not in result.matching_job_ids
    assert ids["inactive"] not in result.matching_job_ids
    assert ids["frontend"] not in result.matching_job_ids
    assert ids["germany"] not in result.matching_job_ids

    with session_scope(session_factory) as session:
        job = session.get(JobPosting, ids["local"])
        assert job is not None
        job.taxonomy_version = "stale"
    with (
        session_scope(session_factory) as session,
        pytest.raises(MarketDataConsistencyError, match=str(ids["local"])),
    ):
        MarketAggregateService(
            session,
            taxonomy_version=TAXONOMY_VERSION,
            normalizer_version=JOB_NORMALIZER_VERSION,
        ).calculate(filters)


def _seed_market(session_factory: sessionmaker[Session]) -> dict[str, UUID]:
    observed = datetime(2026, 8, 21, 10, tzinfo=UTC)
    specs = {
        "local": (
            "Example Inc.",
            "Senior Backend Engineer",
            "backend_engineering",
            "senior",
            "active",
            "onsite",
            None,
            "PL",
            "Build Python and SQL systems.",
        ),
        "remote_eu": (
            "Remote Co",
            "Staff Platform Engineer",
            "platform_engineering",
            "staff",
            "active",
            "remote",
            "EU",
            None,
            "Build Python platforms.",
        ),
        "empty_description": (
            "Example Inc.",
            "Senior Backend Developer",
            "backend_engineering",
            "senior",
            "active",
            "onsite",
            None,
            "PL",
            "",
        ),
        "remote_global": (
            "Remote Co",
            "Senior Backend Engineer",
            "backend_engineering",
            "senior",
            "active",
            "remote",
            "Global",
            None,
            "Build Python.",
        ),
        "inactive": (
            "Example Inc.",
            "Senior Backend Engineer",
            "backend_engineering",
            "senior",
            "inactive",
            "onsite",
            None,
            "PL",
            "Build Python.",
        ),
        "frontend": (
            "Example Inc.",
            "Senior Frontend Backend Engineer",
            "backend_engineering",
            "senior",
            "active",
            "onsite",
            None,
            "PL",
            "Build Python.",
        ),
        "germany": (
            "Example Inc.",
            "Senior Backend Engineer",
            "backend_engineering",
            "senior",
            "active",
            "onsite",
            None,
            "DE",
            "Build Python.",
        ),
    }
    with session_scope(session_factory) as session:
        sources: dict[str, Source] = {}
        for company in {spec[0] for spec in specs.values()}:
            source = Source(
                key=company.casefold().replace(" ", "-"),
                kind="greenhouse",
                company_name=company,
                config={"board_token": company},
                enabled=True,
            )
            session.add(source)
            sources[company] = source
        session.flush()
        ids: dict[str, UUID] = {}
        for external_id, spec in specs.items():
            company, title, role, seniority, status, workplace, scope, country, description = spec
            job = JobPosting(
                source_id=sources[company].id,
                external_id=external_id,
                company_name=company,
                title_raw=title,
                title_normalized=title,
                role_family=role,
                seniority=seniority,
                seniority_confidence=Decimal("1.000"),
                description_text=description,
                workplace_type=workplace,
                remote_scope=scope,
                first_seen_at=observed,
                last_seen_at=observed,
                inactive_at=observed if status == "inactive" else None,
                status=status,
                content_hash=f"hash-{external_id}",
                raw_latest={"id": external_id},
                normalizer_version="source-v1",
                enrichment_normalizer_version=JOB_NORMALIZER_VERSION,
                taxonomy_version=TAXONOMY_VERSION,
            )
            session.add(job)
            session.flush()
            ids[external_id] = job.id
            if country:
                session.add(
                    JobLocation(
                        job_id=job.id,
                        ordinal=0,
                        raw=country,
                        country_code=country,
                        is_primary=True,
                        confidence=Decimal("1.000"),
                    )
                )
        _seed_evidence(session, ids)
        return ids


def _seed_evidence(session: Session, ids: dict[str, UUID]) -> None:
    for matched in ("Python", "python"):
        session.add(_skill(ids["local"], "python", matched))
    session.add(_skill(ids["local"], "sql", "SQL"))
    session.add(_skill(ids["remote_eu"], "python", "Python"))
    session.add(_skill(ids["empty_description"], "go", "Go"))
    session.add_all(
        [
            _comp(ids["local"], "100000", "140000", "USD", "Poland"),
            _comp(ids["local"], "110000", "150000", "USD", "Poland"),
            _comp(ids["local"], "90000", "120000", None, "Poland"),
            _comp(ids["remote_eu"], "80000", "100000", "EUR", "EU"),
        ]
    )


def _skill(job_id: UUID, skill_id: str, matched_text: str) -> JobSkill:
    return JobSkill(
        job_id=job_id,
        skill_id=skill_id,
        matched_text=matched_text,
        section="other",
        method="alias",
        confidence=Decimal("1.000"),
        taxonomy_version=TAXONOMY_VERSION,
    )


def _comp(
    job_id: UUID, minimum: str, maximum: str, currency: str | None, tier: str
) -> JobCompensation:
    return JobCompensation(
        job_id=job_id,
        component_type="base_salary",
        min_value=Decimal(minimum),
        max_value=Decimal(maximum),
        currency=currency,
        interval="year",
        location_tier=tier,
        raw_text=f"{minimum}-{maximum}",
        source_method="structured_source",
        confidence=Decimal("1.000"),
    )


def _return_database_to_empty(engine: Engine, alembic_config: Config, upgraded: bool) -> None:
    try:
        if upgraded:
            command.downgrade(alembic_config, "base")
            with engine.begin() as connection:
                connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
            assert not (
                set(inspect(engine).get_table_names())
                & (CAREER_OS_TABLE_NAMES | {"alembic_version"})
            )
    finally:
        engine.dispose()
