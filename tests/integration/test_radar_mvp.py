"""Real PostgreSQL checks for Radar snapshot, immutable inputs and legacy fallback."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker
from test_markdown_report_e2e import EXCLUDED_JOB_ID, JOB_IDS, _seed_demo
from test_read_api import PROJECT_ROOT
from test_read_api import session_factory as session_factory

from career_os.api.app import ApiDependencies, create_app
from career_os.api.radar_mvp import _read_session
from career_os.config.radar_panel import RadarPanelManifestIdentity, RadarPanelSource
from career_os.config.skills import load_skill_taxonomy
from career_os.db import session_scope
from career_os.persistence.models import (
    CollectionRun,
    JobPosting,
    RadarPanelVersion,
    RadarScanObservation,
    Source,
)
from career_os.persistence.radar_mvp import RadarMvpReadRepository

pytestmark = pytest.mark.integration


def test_radar_real_api_reextracts_exact_text_filters_paginates_and_remains_read_only(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_demo(session_factory)
    taxonomy = load_skill_taxonomy(PROJECT_ROOT / "config/taxonomy/skills.v1.yaml")
    client = TestClient(
        create_app(
            ApiDependencies(
                session_factory_provider=lambda: session_factory,
                taxonomy_provider=lambda: taxonomy,
            )
        )
    )
    with _read_session(lambda: session_factory) as session:
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        assert session.scalar(text("SHOW transaction_isolation")) == "repeatable read"

    initial = client.get("/radar/api/snapshot").json()
    assert initial["summary"]["posting_count"] == 7
    assert next(row for row in initial["skills"] if row["id"] == "python")["count"] == 7
    # Persisted JobSkill has PostgreSQL, but displayed immutable text does not.
    assert next(row for row in initial["skills"] if row["id"] == "postgresql")["count"] == 0
    assert initial["history"]["status"] == "insufficient"
    assert initial["history"]["delta_pp"] is None
    for period in (30, 90, 180):
        response = client.get("/radar/api/snapshot", params={"period_days": period})
        assert response.status_code == 200
        assert response.json()["history"]["period_days"] == period
    ids = []
    for page in range(1, 5):
        response = client.get("/radar/api/snapshot", params={"page": page, "page_size": 2})
        assert response.status_code == 200
        ids.extend(item["id"] for item in response.json()["jobs"]["items"])
    assert len(ids) == len(set(ids)) == 7
    assert set(ids) == {str(job_id) for job_id in (*JOB_IDS, EXCLUDED_JOB_ID)}

    with session_scope(session_factory) as session:
        job = session.get(JobPosting, JOB_IDS[0])
        assert job is not None
        job.description_text = "Rust Rust <script>alert('text only')</script>"
        job.source_url = "javascript:alert(1)"
        job.apply_url = "https://name:password@example.test/private"
    detail_response = client.get(f"/radar/api/jobs/{JOB_IDS[0]}")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["description"].startswith("Rust Rust <script>")
    assert detail["job"]["source_url"] is detail["job"]["apply_url"] is None
    assert {row["id"] for row in detail["skill_evidence"]} == {"rust"}
    assert detail["role_evidence"]
    assert "raw_latest" not in detail_response.text
    assert "board_token" not in detail_response.text
    changed = client.get("/radar/api/snapshot").json()
    assert changed["provenance"]["input_digest"] != initial["provenance"]["input_digest"]
    assert next(row for row in changed["skills"] if row["id"] == "python")["count"] == 6
    rust = client.get("/radar/api/snapshot", params={"skill": "rust"}).json()
    assert [row["id"] for row in rust["jobs"]["items"]] == [str(JOB_IDS[0])]

    with session_scope(session_factory) as session:
        source = session.get(Source, UUID(int=2))
        job = session.get(JobPosting, JOB_IDS[1])
        assert source is not None and job is not None
        source.enabled = False
        job.status = "inactive"
    assert client.get("/radar/api/snapshot").json()["summary"]["posting_count"] == 2
    assert (
        client.get("/radar/api/snapshot", params={"include_inactive": True}).json()["summary"][
            "posting_count"
        ]
        == 3
    )
    all_rows = client.get(
        "/radar/api/snapshot", params={"include_inactive": True, "enabled_only": False}
    ).json()
    assert all_rows["summary"]["posting_count"] == 7
    empty = client.get("/radar/api/snapshot", params={"q": "no-such-phrase-xyz"}).json()
    assert empty["jobs"]["items"] == []
    assert empty["summary"]["posting_count"] == 0
    assert all(row["share"] is None for row in empty["skills"])
    assert client.get("/radar/api/snapshot", params={"source": "nonexistent"}).status_code == 422
    assert client.get(f"/radar/api/jobs/{UUID(int=9999)}").status_code == 404
    assert client.get("/radar/api/methodology").status_code == 200
    assert client.get("/radar/").status_code == 200
    assert client.get("/v1/jobs").status_code == 200
    # Reading all these routes did not overwrite current text or the saved skill table.
    with session_scope(session_factory) as session:
        assert session.scalar(text("SELECT count(*) FROM job_postings")) == 7
        assert session.scalar(text("SELECT count(*) FROM job_skills WHERE skill_id = 'rust'")) == 0


def test_radar_repository_uses_only_exact_complete_immutable_panel_scans_and_legacy_fallback(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_demo(session_factory)
    cutoff = datetime(2026, 10, 17, tzinfo=UTC)
    identity = RadarPanelManifestIdentity(
        panel_version="mvp-repository-test-v1",
        sources=(RadarPanelSource("alpha", "stable-alpha", "greenhouse"),),
        manifest_digest="a" * 64,
    )
    with session_scope(session_factory) as session:
        session.add(
            RadarPanelVersion(
                panel_version=identity.panel_version,
                manifest_digest=identity.manifest_digest,
                panel_digest="b" * 64,
                canonical_manifest=identity.canonical_manifest,
                source_config_digests={"alpha": "c" * 64},
            )
        )
        session.flush()
        for index, (status, digest, days) in enumerate(
            [
                ("succeeded", "b" * 64, 1),
                ("partial", "b" * 64, 2),
                ("succeeded", "d" * 64, 3),
                ("succeeded", "b" * 64, 361),
            ]
        ):
            finished = cutoff - timedelta(days=days)
            run = CollectionRun(
                id=UUID(int=4000 + index),
                source_id=UUID(int=1),
                status=status,
                started_at=finished - timedelta(minutes=1),
                finished_at=finished,
                normalizer_version="test-v1",
                taxonomy_version="1.0.2",
                radar_panel_version=identity.panel_version,
                radar_manifest_digest=identity.manifest_digest,
                radar_panel_digest=digest,
            )
            session.add(run)
            session.flush()
            session.add(
                RadarScanObservation(
                    run_id=run.id,
                    external_id="immutable-1",
                    observed_at=finished,
                    title="Immutable Backend Engineer",
                    description="Go services",
                )
            )
        source = session.get(Source, UUID(int=1))
        assert source is not None
        source.enabled = False
    with _read_session(lambda: session_factory) as session:
        inputs = RadarMvpReadRepository(session).load_history_inputs(
            identity=identity, cutoff=cutoff, lookback_days=360
        )
        assert inputs.reason is None
        assert inputs.panel is not None
        assert [scan.run_id for scan in inputs.scans] == [UUID(int=4000)]
        assert inputs.scans[0].observations[0].title == "Immutable Backend Engineer"
        assert inputs.scans[0].observations[0].description == "Go services"

    command.downgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "20260821_0003")
    with _read_session(lambda: session_factory) as session:
        inputs = RadarMvpReadRepository(session).load_history_inputs(
            identity=identity, cutoff=cutoff, lookback_days=360
        )
        assert inputs.panel is None
        assert inputs.reason == "missing_history_tables"
