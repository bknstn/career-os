"""Offline API construction, validation, cursor, and OpenAPI tests."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openapi_spec_validator import validate

from career_os.api.app import ApiDependencies, _prometheus_metrics, create_app
from career_os.api.cursors import InvalidCursorError, decode_job_cursor, encode_job_cursor
from career_os.api.research_products import create_research_products_router
from career_os.api.schemas import JobFilters
from career_os.application.operations import SourceOperationalStatus
from career_os.config.skills import load_skill_taxonomy
from career_os.config.sources import ConfigurationError
from career_os.db import DatabaseConfigurationError
from career_os.persistence.api_queries import JobCursorKey


def test_app_construction_openapi_and_liveness_are_offline() -> None:
    calls = 0

    def unavailable():
        nonlocal calls
        calls += 1
        raise AssertionError("offline operation initialized persistence")

    app = create_app(
        ApiDependencies(session_factory_provider=unavailable, taxonomy_provider=unavailable)
    )
    schema = app.openapi()
    validate(schema)
    client = TestClient(app)

    assert client.get("/health/live").json() == {"status": "live"}
    assert calls == 0
    assert set(schema["paths"]) == {
        "/health/live",
        "/health/ready",
        "/metrics",
        "/v1/sources",
        "/v1/runs",
        "/v1/runs/{run_id}",
        "/v1/jobs",
        "/v1/jobs/{job_id}",
        "/v1/market-reports",
        "/v1/target-universe-reports",
        "/radar/api/snapshot",
        "/radar/api/jobs/{job_id}",
        "/radar/api/methodology",
        "/radar/api/research/overview",
        "/radar/api/research/questions",
        "/radar/api/research/documents",
        "/radar/api/research/documents/{document_id}/versions/{version_id}",
        "/radar/api/research/claims",
        "/radar/api/research/products",
    }
    job_detail_schema = schema["components"]["schemas"]["JobDetailResponse"]
    serialized = str(job_detail_schema)
    assert "raw_latest" not in serialized
    assert '"config"' not in serialized
    assert "raw_text" not in serialized
    report_operation = str(schema["paths"]["/v1/market-reports"])
    assert "MarketReportResponse" in report_operation
    assert "MarketAggregatesResponse" not in report_operation
    assert "ProfileComparisonResponse" not in report_operation
    target_universe_operation = str(schema["paths"]["/v1/target-universe-reports"])
    assert "TargetUniverseReportResponse" in target_universe_operation
    assert "MarketReportResponse" not in target_universe_operation


def test_research_product_projection_is_read_only_and_defers_io_until_request(
    tmp_path: Path,
) -> None:
    calls = 0

    def unavailable():
        nonlocal calls
        calls += 1
        raise RuntimeError("isolated read failure")

    state_path = tmp_path / "monitor" / "state.json"
    app = FastAPI()
    app.include_router(
        create_research_products_router(
            session_factory_provider=unavailable,
            state_path=state_path,
        )
    )
    assert calls == 0
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/radar/api/research/products")

    assert response.status_code == 200
    assert response.json()["weekly"] is None
    assert response.json()["eligibility_check_status"] == "unavailable"
    assert calls == 1
    assert not state_path.exists()
    assert not state_path.parent.exists()


def test_research_page_assets_include_terminal_navigation_without_database_access() -> None:
    calls = 0

    def unavailable():
        nonlocal calls
        calls += 1
        raise AssertionError("static research navigation must not access persistence")

    client = TestClient(
        create_app(
            ApiDependencies(session_factory_provider=unavailable, taxonomy_provider=unavailable)
        )
    )
    page = client.get("/radar/")
    script = client.get("/radar/app.js?v=research-terminal-1")

    assert page.status_code == 200
    assert 'href="#/research" data-nav="research"' in page.text
    assert script.status_code == 200
    assert 'getJson("research/overview")' in script.text
    assert "Accepted current evidence" in script.text
    assert calls == 0


def test_radar_invalid_identifiers_are_rejected_before_database_access() -> None:
    calls = 0

    def unavailable():
        nonlocal calls
        calls += 1
        raise AssertionError("invalid Radar filters must be rejected before database access")

    taxonomy = load_skill_taxonomy(
        Path(__file__).resolve().parents[2] / "config/taxonomy/skills.v1.yaml"
    )
    client = TestClient(
        create_app(
            ApiDependencies(
                session_factory_provider=unavailable,
                taxonomy_provider=lambda: taxonomy,
            )
        )
    )

    for params in (
        {"occupation": "not_an_occupation"},
        {"skill": "not_a_skill"},
        {"period_days": 31},
    ):
        response = client.get("/radar/api/snapshot", params=params)
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid Radar filters"}
    assert calls == 0


def test_radar_period_query_strings_are_parsed_before_database_access() -> None:
    calls = 0

    def unavailable():
        nonlocal calls
        calls += 1
        raise DatabaseConfigurationError("private DSN")

    taxonomy = load_skill_taxonomy(
        Path(__file__).resolve().parents[2] / "config/taxonomy/skills.v1.yaml"
    )
    client = TestClient(
        create_app(
            ApiDependencies(
                session_factory_provider=unavailable,
                taxonomy_provider=lambda: taxonomy,
            )
        )
    )
    for period in (30, 90, 180):
        response = client.get("/radar/api/snapshot", params={"period_days": str(period)})
        assert response.status_code == 503
        assert response.json() == {"detail": "database unavailable"}
    assert calls == 3


def test_prometheus_metrics_are_low_cardinality_and_escape_source_labels() -> None:
    status = SourceOperationalStatus(
        source_key='example"source',
        source_kind="greenhouse",
        enabled=True,
        active_jobs=12,
        latest_status="succeeded",
        latest_finished_at=datetime(2026, 8, 23, 10, tzinfo=UTC),
        last_success_at=datetime(2026, 8, 23, 10, tzinfo=UTC),
        last_success_age_seconds=120.5,
        stale=False,
        consecutive_unsafe_runs=0,
        circuit_open=False,
        latest_items_fetched=12,
        latest_http_requests=2,
        latest_retry_count=1,
        latest_rate_limit_events=0,
        latest_failed_items=0,
        latest_normalization_errors=0,
    )

    rendered = _prometheus_metrics((status,))

    assert 'source_key="example\\"source"' in rendered
    assert "career_os_http_latest_requests" in rendered
    assert "career_os_source_last_success_age_seconds" in rendered
    assert "job_id" not in rendered
    assert "url=" not in rendered


def test_readiness_is_sanitized_and_validation_has_stable_body() -> None:
    def unavailable():
        raise DatabaseConfigurationError("secret-user:secret-password@private-host")

    app = create_app(
        ApiDependencies(session_factory_provider=unavailable, taxonomy_provider=unavailable)
    )
    client = TestClient(app)

    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
    assert "secret" not in response.text

    malformed = client.get("/v1/jobs/not-a-uuid")
    assert malformed.status_code == 422
    assert malformed.json() == {"detail": "request validation failed"}
    invalid_filter = client.get("/v1/jobs", params={"country_code": "pol"})
    assert invalid_filter.status_code == 422
    assert invalid_filter.json() == {"detail": "request validation failed"}

    def invalid_settings():
        raise ConfigurationError("secret DSN or invalid environment")

    invalid_client = TestClient(
        create_app(
            ApiDependencies(
                session_factory_provider=invalid_settings,
                taxonomy_provider=invalid_settings,
            )
        )
    )
    for path in ("/health/ready", "/v1/sources"):
        invalid_response = invalid_client.get(path)
        assert invalid_response.status_code == 503
        assert invalid_response.json() == {"detail": "database unavailable"}
        assert "secret" not in invalid_response.text


def test_job_cursor_is_versioned_filter_bound_and_stable_under_ties() -> None:
    filters = JobFilters(status="active", company="Example")
    key = JobCursorKey(
        last_seen_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        job_id=UUID(int=42),
    )
    cursor = encode_job_cursor(key, filters=filters)

    assert decode_job_cursor(cursor, filters=filters) == key
    assert cursor == encode_job_cursor(key, filters=filters)
    for bad in (cursor[:-1] + ("A" if cursor[-1] != "A" else "B"), "not-base64!"):
        try:
            decode_job_cursor(bad, filters=filters)
        except InvalidCursorError as error:
            assert str(error) == "invalid or filter-incompatible cursor"
        else:  # pragma: no cover - assertion branch
            raise AssertionError("invalid cursor was accepted")
    try:
        decode_job_cursor(cursor, filters=JobFilters(status="inactive", company="Example"))
    except InvalidCursorError:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("cursor was accepted with different filters")

    for field, value in (("v", True), ("i", True), ("t", 12), ("f", None)):
        envelope = _decoded_cursor(cursor)
        envelope["b"][field] = value
        canonical = json.dumps(envelope["b"], sort_keys=True, separators=(",", ":")).encode()
        envelope["c"] = hashlib.sha256(canonical).hexdigest()
        forged = (
            base64.urlsafe_b64encode(
                json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
            )
            .rstrip(b"=")
            .decode()
        )
        try:
            decode_job_cursor(forged, filters=filters)
        except InvalidCursorError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError(f"cursor accepted invalid {field}")

    try:
        encode_job_cursor(
            JobCursorKey(last_seen_at=datetime(2026, 8, 21, 12), job_id=UUID(int=1)),
            filters=filters,
        )
    except ValueError as error:
        assert str(error) == "cursor timestamp must be timezone-aware"
    else:  # pragma: no cover - assertion branch
        raise AssertionError("naive cursor timestamp was accepted")


def test_invalid_cursor_maps_to_400_before_database_access() -> None:
    calls = 0

    def unavailable():
        nonlocal calls
        calls += 1
        raise AssertionError("invalid cursor reached persistence")

    client = TestClient(
        create_app(
            ApiDependencies(session_factory_provider=unavailable, taxonomy_provider=unavailable)
        )
    )
    response = client.get("/v1/jobs", params={"cursor": "bad!"})

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid or filter-incompatible cursor"}
    assert calls == 0


def test_report_distinguishes_server_taxonomy_failure_from_unknown_profile_skill() -> None:
    database_calls = 0

    def no_database():
        nonlocal database_calls
        database_calls += 1
        raise AssertionError("invalid report reached persistence")

    request = {
        "market": {"name": "example"},
        "profile": {
            "name": "example",
            "target_role_families": ["backend_engineering"],
            "seniority": "senior",
            "skills": [{"id": "imaginary_db", "level": "working"}],
        },
    }

    def broken_taxonomy():
        raise ConfigurationError("secret local taxonomy path")

    broken_client = TestClient(
        create_app(
            ApiDependencies(
                session_factory_provider=no_database,
                taxonomy_provider=broken_taxonomy,
            )
        )
    )
    broken = broken_client.post("/v1/market-reports", json=request)
    assert broken.status_code == 503
    assert broken.json() == {"detail": "taxonomy unavailable"}
    assert "secret" not in broken.text

    taxonomy = load_skill_taxonomy(
        Path(__file__).resolve().parents[2] / "config/taxonomy/skills.v1.yaml"
    )
    unknown_client = TestClient(
        create_app(
            ApiDependencies(
                session_factory_provider=no_database,
                taxonomy_provider=lambda: taxonomy,
            )
        )
    )
    unknown = unknown_client.post("/v1/market-reports", json=request)
    assert unknown.status_code == 422
    assert unknown.json()["detail"].endswith("unknown taxonomy IDs: imaginary_db")
    assert database_calls == 0


def test_report_rejects_noncanonical_quality_review_before_infrastructure() -> None:
    calls = 0

    def unavailable():
        nonlocal calls
        calls += 1
        raise AssertionError("invalid review reached infrastructure")

    client = TestClient(
        create_app(
            ApiDependencies(
                session_factory_provider=unavailable,
                taxonomy_provider=unavailable,
            )
        )
    )
    response = client.post(
        "/v1/market-reports",
        json={
            "market": {"name": "example"},
            "profile": {
                "name": "example",
                "target_role_families": ["backend_engineering"],
                "seniority": "senior",
                "skills": [],
            },
            "quality_review": {
                "ref": "review.md#rows",
                "reviewed_job_ids": [str(UUID(int=2)), str(UUID(int=1))],
                "false_positive_job_ids": [],
            },
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "request validation failed"}
    assert calls == 0


def test_target_universe_report_rejects_an_implicit_boundary_before_infrastructure() -> None:
    calls = 0

    def unavailable():
        nonlocal calls
        calls += 1
        raise AssertionError("invalid target universe reached infrastructure")

    client = TestClient(
        create_app(
            ApiDependencies(
                session_factory_provider=unavailable,
                taxonomy_provider=unavailable,
            )
        )
    )
    response = client.post(
        "/v1/target-universe-reports",
        json={
            "target_universe": {
                "name": "example",
                "role_families": ["backend_engineering"],
                "seniorities": ["senior"],
                "source_panel": [],
                "geography": {"country_codes": ["PL"]},
            },
            "profile": {
                "name": "example",
                "target_role_families": ["backend_engineering"],
                "seniority": "senior",
                "skills": [],
            },
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "request validation failed"}
    assert calls == 0


def _decoded_cursor(cursor: str) -> dict[str, object]:
    padding = "=" * (-len(cursor) % 4)
    value = json.loads(base64.urlsafe_b64decode(cursor + padding))
    assert isinstance(value, dict)
    return value
