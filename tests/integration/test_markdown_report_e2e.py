"""Stable PostgreSQL-to-CLI Markdown snapshot over a deterministic demo market."""

from __future__ import annotations

import os
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from career_os.cli import app
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
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TAXONOMY_VERSION = "1.0.2"
JOB_IDS = tuple(UUID(int=value) for value in range(101, 107))
EXCLUDED_JOB_ID = UUID(int=107)


@pytest.fixture
def session_factory() -> Generator[sessionmaker[Session], None, None]:
    if os.environ.get("RUN_DB_INTEGRATION") != "1":
        pytest.skip("set RUN_DB_INTEGRATION=1 to run PostgreSQL integration tests")
    settings = load_settings()
    if settings.database_url is None:
        pytest.skip("set CAREER_OS_DATABASE_URL to run PostgreSQL integration tests")
    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
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


def test_real_cli_report_matches_snapshot_and_reconciles_ids_and_urls(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    stored, excluded_url = _seed_demo(session_factory)
    with session_scope(session_factory) as session:
        excluded = session.get(JobPosting, EXCLUDED_JOB_ID)
        excluded_location = session.get(JobLocation, (EXCLUDED_JOB_ID, 0))
        assert excluded is not None
        assert excluded.status == "active"
        assert excluded.role_family == "backend_engineering"
        assert excluded.seniority == "senior"
        assert "backend" in excluded.title_normalized
        assert "frontend" in excluded.title_normalized
        assert excluded_location is not None and excluded_location.country_code == "PL"
    market_config = tmp_path / "market.yaml"
    market_config.write_text(
        """market:
  name: senior-backend-poland-eu
  title_include: [backend, platform]
  title_exclude: [frontend]
  role_families: [backend_engineering, platform_engineering]
  seniorities: [senior, staff]
  country_codes: [PL]
  include_remote: true
  remote_scopes: [EU]
  company_names: []
  active_only: true
""",
        encoding="utf-8",
    )
    profile_config = tmp_path / "profile.yaml"
    profile_config.write_text(
        """profile:
  name: current-backend
  target_role_families: [backend_engineering]
  seniority: senior
  skills:
    - {id: python, level: strong, years: 7}
    - {id: terraform, level: working}
""",
        encoding="utf-8",
    )
    output = tmp_path / "report.md"

    result = CliRunner().invoke(
        app,
        [
            "market",
            "report",
            "--market",
            str(market_config),
            "--profile",
            str(profile_config),
            "--taxonomy",
            str(PROJECT_ROOT / "config/taxonomy/skills.v1.yaml"),
            "--format",
            "markdown",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    rendered = output.read_text(encoding="utf-8")
    assert rendered.startswith("# Market Explorer result: senior-backend-poland-eu\n")
    assert "- Contract: `h1.hybrid-result.v1`" in rendered
    assert "- Opportunities: **available** (6 jobs)" in rendered
    assert "## Concrete opportunities" in rendered
    assert "## Panel-bounded observations (configured source panel)" in rendered
    assert rendered.index("## Concrete opportunities") < rendered.index(
        "## Panel-bounded observations (configured source panel)"
    )
    assert "## Demand" not in rendered
    assert "rank_score" not in rendered
    assert "Ranked uncovered mentions" not in rendered
    assert set(stored) == set(JOB_IDS)
    for job_id, source_url in stored.items():
        assert str(job_id) in rendered
        assert source_url in rendered
    assert str(EXCLUDED_JOB_ID) not in rendered
    assert excluded_url not in rendered
    with session_scope(session_factory) as session:
        rows = tuple(
            session.execute(
                select(JobPosting.id, JobPosting.source_url)
                .where(JobPosting.id.in_(JOB_IDS))
                .order_by(JobPosting.id)
            )
        )
    assert rows == tuple((job_id, stored[job_id]) for job_id in JOB_IDS)


def _seed_demo(session_factory: sessionmaker[Session]) -> tuple[dict[UUID, str], str]:
    source_ids = (UUID(int=1), UUID(int=2))
    observed = datetime(2026, 8, 21, 12, tzinfo=UTC)
    specs = (
        (
            "Alpha Systems",
            "Senior Backend Engineer",
            "backend_engineering",
            "senior",
            "onsite",
            None,
            "PL",
        ),
        (
            "Alpha Systems",
            "Senior Backend Developer",
            "backend_engineering",
            "senior",
            "onsite",
            None,
            "PL",
        ),
        (
            "Alpha Systems",
            "Staff Platform Engineer",
            "platform_engineering",
            "staff",
            "hybrid",
            None,
            "PL",
        ),
        (
            "Beta Cloud",
            "Senior Platform Engineer",
            "platform_engineering",
            "senior",
            "remote",
            "EU",
            None,
        ),
        (
            "Beta Cloud",
            "Senior Backend Engineer",
            "backend_engineering",
            "senior",
            "remote",
            "EU",
            "PL",
        ),
        (
            "Beta Cloud",
            "Staff Backend Engineer",
            "backend_engineering",
            "staff",
            "onsite",
            None,
            "PL",
        ),
    )
    with session_scope(session_factory) as session:
        sources = (
            Source(
                id=source_ids[0],
                key="alpha",
                kind="greenhouse",
                company_name="Alpha Systems",
                config={"board_token": "alpha"},
                enabled=True,
            ),
            Source(
                id=source_ids[1],
                key="beta",
                kind="lever",
                company_name="Beta Cloud",
                config={"site": "beta"},
                enabled=True,
            ),
        )
        session.add_all(sources)
        session.flush()
        urls: dict[UUID, str] = {}
        for index, (job_id, spec) in enumerate(zip(JOB_IDS, specs, strict=True)):
            company, title, role, seniority, workplace, scope, country = spec
            source_url = f"https://jobs.example.test/{company.split()[0].lower()}/{index + 1}"
            job = JobPosting(
                id=job_id,
                source_id=source_ids[0] if company == "Alpha Systems" else source_ids[1],
                external_id=f"demo-{index + 1}",
                company_name=company,
                title_raw=title,
                title_normalized=title.lower(),
                role_family=role,
                seniority=seniority,
                seniority_confidence=Decimal("1.000"),
                description_text="Python Kafka platform work.",
                workplace_type=workplace,
                remote_scope=scope,
                source_url=source_url,
                first_seen_at=observed,
                last_seen_at=observed + timedelta(minutes=index),
                status="active",
                content_hash=f"demo-hash-{index + 1}",
                raw_latest={"id": f"demo-{index + 1}"},
                normalizer_version="demo-source-v1",
                enrichment_normalizer_version=JOB_NORMALIZER_VERSION,
                taxonomy_version=TAXONOMY_VERSION,
            )
            session.add(job)
            session.flush()
            if country is not None:
                session.add(
                    JobLocation(
                        job_id=job_id,
                        ordinal=0,
                        raw=country,
                        country_code=country,
                        is_primary=True,
                        confidence=Decimal("1.000"),
                    )
                )
            urls[job_id] = source_url
        excluded_url = "https://jobs.example.test/beta/excluded-frontend"
        session.add(
            JobPosting(
                id=EXCLUDED_JOB_ID,
                source_id=source_ids[1],
                external_id="demo-excluded-frontend",
                company_name="Beta Cloud",
                title_raw="Senior Frontend Backend Engineer",
                title_normalized="senior frontend backend engineer",
                role_family="backend_engineering",
                seniority="senior",
                seniority_confidence=Decimal("1.000"),
                description_text="Python frontend work.",
                workplace_type="onsite",
                source_url=excluded_url,
                first_seen_at=observed,
                last_seen_at=observed + timedelta(minutes=6),
                status="active",
                content_hash="demo-hash-excluded-frontend",
                raw_latest={"id": "demo-excluded-frontend"},
                normalizer_version="demo-source-v1",
                enrichment_normalizer_version=JOB_NORMALIZER_VERSION,
                taxonomy_version=TAXONOMY_VERSION,
            )
        )
        session.flush()
        session.add(
            JobLocation(
                job_id=EXCLUDED_JOB_ID,
                ordinal=0,
                raw="PL",
                country_code="PL",
                is_primary=True,
                confidence=Decimal("1.000"),
            )
        )
        _seed_skills(session)
        _seed_compensation(session)
    return urls, excluded_url


def _seed_skills(session: Session) -> None:
    skills_by_job = {
        JOB_IDS[0]: ("python", "kafka", "postgresql"),
        JOB_IDS[1]: ("python", "kafka"),
        JOB_IDS[2]: ("python", "kafka"),
        JOB_IDS[3]: ("python", "kafka"),
        JOB_IDS[4]: ("python", "kafka"),
        JOB_IDS[5]: ("python", "redis"),
    }
    for job_id, skill_ids in skills_by_job.items():
        for skill_id in skill_ids:
            session.add(
                JobSkill(
                    id=UUID(int=1000 + job_id.int * 10 + len(skill_id)),
                    job_id=job_id,
                    skill_id=skill_id,
                    matched_text=skill_id.title(),
                    section="other",
                    method="alias",
                    confidence=Decimal("1.000"),
                    taxonomy_version=TAXONOMY_VERSION,
                )
            )


def _seed_compensation(session: Session) -> None:
    values = (
        (UUID(int=2001), JOB_IDS[0], "base_salary", "100000", "140000", "USD", "Poland"),
        (UUID(int=2002), JOB_IDS[1], "base_salary", "110000", "150000", "USD", "Poland"),
        (UUID(int=2003), JOB_IDS[3], "base_salary", "80000", "100000", "EUR", "EU"),
        (UUID(int=2004), JOB_IDS[3], "bonus", "10000", "20000", "EUR", "EU"),
    )
    for component_id, job_id, component, minimum, maximum, currency, tier in values:
        session.add(
            JobCompensation(
                id=component_id,
                job_id=job_id,
                component_type=component,
                min_value=Decimal(minimum),
                max_value=Decimal(maximum),
                currency=currency,
                interval="year",
                location_tier=tier,
                raw_text=f"{minimum}-{maximum} {currency}",
                source_method="structured_source",
                confidence=Decimal("1.000"),
            )
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
