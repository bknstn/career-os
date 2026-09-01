"""Real PostgreSQL coverage for the read-only HTTP API and report reconciliation."""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
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
                taxonomy_version="1.0.2",
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
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert 'career_os_http_latest_requests{source_key="alpha",source_kind="greenhouse"}' in (
        metrics.text
    )
    assert "job_id" not in metrics.text and "url=" not in metrics.text
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
    schema = json.loads(
        (PROJECT_ROOT / "docs/h1-hybrid-result-contract.schema.json").read_text(encoding="utf-8")
    )
    schema_errors = list(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(report)
    )
    assert schema_errors == [], [(list(error.path), error.message) for error in schema_errors]
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
    assert report["contract_version"] == "h1.hybrid-result.v1"
    assert report["scope"] == {
        "kind": "configured_source_panel",
        "name": report_request["market"]["name"],
        "target_universe": None,
    }
    assert report["quality"]["state"] == "evidence_limited"
    assert report["quality"]["claim_policy"] == {
        "opportunities": "available",
        "panel_observations": "qualified",
        "broad_market_claims": "disabled",
        "skill_semantics": "mentions_only",
        "fit_probability": "not_provided",
    }
    assert report["opportunities"]["job_ids"] == expected_ids
    assert report["opportunities"]["count"] == 6
    assert [item["job_id"] for item in report["opportunities"]["items"]] == expected_ids
    assert [item["source"]["url"] for item in report["opportunities"]["items"]] == [
        urls[job_id] for job_id in JOB_IDS
    ]
    sample = report["observations"]["sample"]
    assert sample["job_ids"] == expected_ids
    assert sample["job_count"] == 6
    assert sample["skill_denominator_job_ids"] == expected_ids
    assert sample["compensation_job_ids"] == [
        str(JOB_IDS[0]),
        str(JOB_IDS[1]),
        str(JOB_IDS[3]),
    ]
    compensation = report["observations"]["compensation"]
    assert {component for group in compensation for component in group["component_ids"]} == {
        str(UUID(int=value)) for value in range(2001, 2005)
    }
    assert all(group["denominator_job_ids"] == expected_ids for group in compensation)
    assert all(set(group["job_ids"]) <= set(expected_ids) for group in compensation)
    assert [item["skill_id"] for item in report["observations"]["skills"]] == [
        "kafka",
        "postgresql",
        "python",
        "redis",
    ]
    profile_mentions = report["observations"]["profile_mentions"]
    assert profile_mentions["covered_skill_ids"] == ["python"]
    assert profile_mentions["neutral_skill_ids"] == ["terraform"]
    assert all("rank_score" not in str(item) for item in report["observations"]["skills"])

    universe_request = {
        "target_universe": {
            "name": "senior-backend-poland-eu-target",
            "role_families": ["backend_engineering", "platform_engineering"],
            "seniorities": ["senior", "staff"],
            "source_panel": [
                {"source_key": "alpha", "company_name": "Alpha Systems"},
                {"source_key": "beta", "company_name": "Beta Cloud"},
            ],
            "geography": {
                "country_codes": ["PL"],
                "include_remote": True,
                "remote_scopes": ["EU"],
            },
            "exclusions": {"title_contains": ["frontend"]},
        },
        "profile": report_request["profile"],
    }
    universe_response = client.post("/v1/target-universe-reports", json=universe_request)
    assert universe_response.status_code == 200, universe_response.text
    universe_report = universe_response.json()
    assert universe_report["contract_version"] == "h2.target-universe.v1"
    assert "filters" not in universe_report
    assert universe_report["scope"] == {
        "kind": "target_universe",
        "name": "senior-backend-poland-eu-target",
        "target_universe": {
            **universe_request["target_universe"],
            "source_panel": [
                {"source_key": "alpha", "company_name": "Alpha Systems"},
                {"source_key": "beta", "company_name": "Beta Cloud"},
            ],
            "geography": {
                **universe_request["target_universe"]["geography"],
                "remote_scopes": ["eu"],
            },
            "exclusions": {"title_contains": ["frontend"]},
        },
    }
    assert universe_report["observations"]["scope"] == "target_universe_configured_source_panel"
    assert universe_report["quality"]["claim_policy"] == {
        "opportunities": "available",
        "target_universe_observations": "qualified",
        "broad_market_claims": "disabled",
        "skill_semantics": "mentions_only",
        "fit_probability": "not_provided",
    }
    assert universe_report["opportunities"]["job_ids"] == expected_ids
    assert universe_report["observations"]["sample"]["job_ids"] == expected_ids
    assert universe_report["observations"]["sample"]["source_coverage"] == {
        "configured_source_count": 2,
        "configured_source_keys": ["alpha", "beta"],
        "matching_source_count": 2,
        "matching_source_keys": ["alpha", "beta"],
        "source_share": 1.0,
        "job_ids_by_source": [
            {"source_key": "alpha", "job_ids": [str(job_id) for job_id in JOB_IDS[:3]]},
            {"source_key": "beta", "job_ids": [str(job_id) for job_id in JOB_IDS[3:]]},
        ],
    }
    assert _target_universe_schema_errors(universe_report) == []


def _target_universe_schema_errors(payload: dict[str, object]) -> list[object]:
    h1 = json.loads(
        (PROJECT_ROOT / "docs/h1-hybrid-result-contract.schema.json").read_text(encoding="utf-8")
    )
    h2 = json.loads(
        (PROJECT_ROOT / "docs/h2-target-universe-result-contract.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(h2)
    registry = Registry().with_resources(
        (
            (h1["$id"], Resource.from_contents(h1)),
            (h2["$id"], Resource.from_contents(h2)),
        )
    )
    return list(
        Draft202012Validator(h2, registry=registry, format_checker=FormatChecker()).iter_errors(
            payload
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
