"""Real PostgreSQL coverage for the read-only HTTP API and report reconciliation."""

from __future__ import annotations

import os
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from test_markdown_report_e2e import JOB_IDS, _seed_demo

from career_os.api.app import ApiDependencies, create_app
from career_os.config.skills import load_skill_taxonomy
from career_os.db import (
    create_database_engine,
    create_session_factory,
    database_url_from_settings,
    session_scope,
)
from career_os.persistence.integration_safety import (
    CAREER_OS_TABLE_NAMES,
    assert_disposable_test_database,
    assert_initial_schema_is_absent,
)
from career_os.persistence.models import CollectionRun, JobVersion
from career_os.settings import load_settings

pytestmark = pytest.mark.integration
PROJECT_ROOT = Path(__file__).resolve().parents[2]


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


def test_real_read_api_filters_cursor_detail_readiness_and_report_reconciliation(
    session_factory: sessionmaker[Session],
) -> None:
    urls, _ = _seed_demo(session_factory)
    run_id = UUID(int=3001)
    with session_scope(session_factory) as session:
        session.add(
            CollectionRun(
                id=run_id,
                source_id=UUID(int=1),
                status="succeeded",
                started_at=datetime(2026, 8, 21, 11, tzinfo=UTC),
                finished_at=datetime(2026, 8, 21, 11, 1, tzinfo=UTC),
                items_fetched=3,
                normalizer_version="demo-source-v1",
                taxonomy_version="1.0.0",
            )
        )
        session.add(
            JobVersion(
                id=UUID(int=3002),
                job_id=JOB_IDS[0],
                observed_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
                content_hash="demo-hash-1",
                canonical_snapshot={"title_raw": "Senior Backend Engineer"},
                raw_payload={"private": "must-not-leak"},
                changed_fields=["title_raw"],
            )
        )

    taxonomy = load_skill_taxonomy(PROJECT_ROOT / "config/taxonomy/skills.v1.yaml")
    client = TestClient(
        create_app(
            ApiDependencies(
                session_factory_provider=lambda: session_factory,
                taxonomy_provider=lambda: taxonomy,
            )
        )
    )

    assert client.get("/health/ready").json() == {"status": "ready"}
    sources = client.get("/v1/sources").json()
    assert [item["key"] for item in sources] == ["alpha", "beta"]
    assert all("config" not in item for item in sources)
    assert client.get(f"/v1/runs/{run_id}").json()["counts"]["items_fetched"] == 3
    assert client.get(f"/v1/runs/{UUID(int=9999)}").status_code == 404

    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get("/v1/jobs", params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 7

    filtered = client.get(
        "/v1/jobs",
        params={
            "status": "active",
            "company": "Alpha Systems",
            "role_family": "backend_engineering",
            "seniority": "senior",
            "country_code": "PL",
            "skill": "python",
        },
    ).json()
    assert [item["id"] for item in filtered["items"]] == [str(JOB_IDS[1]), str(JOB_IDS[0])]
    first_page = client.get("/v1/jobs", params={"limit": 1, "status": "active"}).json()
    incompatible = client.get(
        "/v1/jobs",
        params={"limit": 1, "status": "inactive", "cursor": first_page["next_cursor"]},
    )
    assert incompatible.status_code == 400

    detail = client.get(f"/v1/jobs/{JOB_IDS[0]}").json()
    assert detail["source_key"] == "alpha"
    assert [item["skill_id"] for item in detail["skill_evidence"]] == [
        "kafka",
        "postgresql",
        "python",
    ]
    assert detail["locations"][0]["country_code"] == "PL"
    assert detail["versions"][0]["changed_fields"] == ["title_raw"]
    assert "raw_latest" not in detail and "raw_payload" not in str(detail)
    assert "raw" not in detail["locations"][0]
    assert "raw_text" not in detail["compensation"][0]

    report_request = {
        "market": {
            "name": "senior-backend-poland-eu",
            "title_include": ["backend", "platform"],
            "title_exclude": ["frontend"],
            "role_families": ["backend_engineering", "platform_engineering"],
            "seniorities": ["senior", "staff"],
            "country_codes": ["PL"],
            "include_remote": True,
            "remote_scopes": ["EU"],
        },
        "profile": {
            "name": "current-backend",
            "target_role_families": ["backend_engineering"],
            "seniority": "senior",
            "skills": [
                {"id": "python", "level": "strong", "years": "7"},
                {"id": "terraform", "level": "working"},
            ],
        },
    }
    response = client.post("/v1/market-reports", json=report_request)
    assert response.status_code == 200, response.text
    report = response.json()
    expected_ids = [str(job_id) for job_id in JOB_IDS]
    assert report["filters"] == {
        **report_request["market"],
        "remote_scopes": ["eu"],
        "company_names": [],
        "active_only": True,
    }
    assert report["profile"] == {
        **report_request["profile"],
        "skills": [
            {"id": "python", "level": "strong", "years": "7"},
            {"id": "terraform", "level": "working", "years": None},
        ],
    }
    assert report["market"]["matching_job_ids"] == expected_ids
    assert report["market"]["matching_job_count"] == 6
    assert report["market"]["skill_denominator_job_ids"] == expected_ids
    assert len(report["market"]["skill_denominator_job_ids"]) == 6
    assert report["market"]["compensation_job_ids"] == [
        str(JOB_IDS[0]),
        str(JOB_IDS[1]),
        str(JOB_IDS[3]),
    ]
    assert [item["job_id"] for item in report["jobs"]] == expected_ids
    assert [item["source_url"] for item in report["jobs"]] == [urls[job_id] for job_id in JOB_IDS]
    compensation = report["market"]["compensation"]
    assert {component for group in compensation for component in group["component_ids"]} == {
        str(UUID(int=value)) for value in range(2001, 2005)
    }
    assert all(group["denominator_job_ids"] == expected_ids for group in compensation)
    assert all(set(group["job_ids"]) <= set(expected_ids) for group in compensation)
    assert report["comparison"]["matching_job_ids"] == expected_ids
    assert report["comparison"]["skill_denominator_job_ids"] == expected_ids
    assert [item["value"] for item in report["comparison"]["covered_skills"]] == ["python"]
    kafka_gap = report["comparison"]["gaps"][0]
    assert kafka_gap["skill_id"] == "kafka"
    assert kafka_gap["demand"]["job_count"] == 5
    assert kafka_gap["demand"]["denominator"] == 6
    assert kafka_gap["rank_score"] > 0
    coverage = report["comparison"]["job_skill_coverage"]
    assert [item["job_id"] for item in coverage] == expected_ids
    assert all(
        set(item["covered_skill_ids"]) <= set(item["mentioned_skill_ids"]) for item in coverage
    )
    assert {item["job_id"] for item in report["comparison"]["representative_jobs"]} <= set(
        expected_ids
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
