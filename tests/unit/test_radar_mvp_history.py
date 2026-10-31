"""Contract-level tests for the versioned MVP history metric gates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import cache
from uuid import UUID, uuid5

import pytest

from career_os.application.radar_mvp_history import compare_mvp_history
from career_os.config.radar_panel import RadarPanelSource
from career_os.config.skills import SkillTaxonomy
from career_os.normalization.skills import SkillExtractor
from career_os.persistence.radar_mvp import RadarMvpSource
from career_os.radar.aggregate import (
    RadarObservationInput,
    RadarPanelInput,
    RadarScanInput,
)
from career_os.radar.mvp_roles import classify_mvp_role

_NAMESPACE = UUID("9e62bc8e-6e12-4d3d-8157-2e79d7a4f458")


def _history_fixture(period_days=30, descriptions=None, title=None):
    cutoff = datetime(2026, 10, 17, tzinfo=UTC)
    panel_sources = tuple(
        RadarPanelSource(
            source_key=f"board-{board}",
            company_key=f"company-{board}",
            source_kind="greenhouse",
        )
        for board in range(5)
    )
    panel = RadarPanelInput(
        panel_version="mvp-test-v1",
        manifest_digest="a" * 64,
        panel_digest="b" * 64,
        source_config_digests={},
        canonical_manifest={"panel_version": "mvp-test-v1"},
        sources=panel_sources,
    )
    scans: list[RadarScanInput] = []
    previous_start = cutoff - timedelta(days=period_days * 2)
    current_start = cutoff - timedelta(days=period_days)
    for board in range(5):
        source_key = f"board-{board}"
        observations = tuple(
            RadarObservationInput(
                external_id=f"posting-{board}-{index}",
                observed_at=cutoff - timedelta(days=1),
                title=("Backend Engineer" if index < 4 else "Technical Program Manager"),
                description="Build services with Python.",
            )
            for index in range(20)
        )
        for window_name, window_start in (
            ("previous", previous_start),
            ("current", current_start),
        ):
            for ordinal in range((period_days + 6) // 7):
                finished_at = window_start + timedelta(days=ordinal * 7 + 1)
                run_id = uuid5(_NAMESPACE, f"{source_key}-{window_name}-{ordinal}")
                run_observations = tuple(
                    RadarObservationInput(
                        external_id=item.external_id,
                        observed_at=finished_at,
                        title=title or item.title,
                        description=(
                            descriptions(window_name, index)
                            if descriptions is not None
                            else item.description
                        ),
                    )
                    for index, item in enumerate(observations)
                )
                scans.append(
                    RadarScanInput(
                        run_id=run_id,
                        source_key=source_key,
                        source_kind="greenhouse",
                        finished_at=finished_at,
                        observations=run_observations,
                    )
                )
    sources = tuple(
        RadarMvpSource(
            key=item.source_key,
            kind=item.source_kind,
            company_name=item.company_key,
            enabled=False,
            status="succeeded",
            last_success_at=cutoff,
            stale=False,
        )
        for item in panel_sources
    )
    # History tests need one skill; the full published corpus has its own regression test.
    taxonomy = SkillTaxonomy.model_validate(
        {
            "version": "1.0.0",
            "skills": [
                {"id": "python", "label": "Python", "category": "language", "aliases": ["Python"]}
            ],
        }
    )
    extractor = SkillExtractor(taxonomy)

    @cache
    def analyze(title: str, description: str):
        return (
            classify_mvp_role(title, description),
            extractor.extract(title=title, description=description),
        )

    return {
        "panel": panel,
        "scans": tuple(scans),
        "input_reason": None,
        "cutoff": cutoff,
        "period_days": period_days,
        "selected_source_keys": tuple(source.key for source in sources),
        "sources": sources,
        "q": None,
        "selected_occupation": None,
        "selected_specialty": None,
        "selected_skill": None,
        "occupations": (
            ("technical_engineering", "Technical engineering"),
            ("engineering_management", "Engineering management"),
            ("solutions_architecture", "Solutions architecture"),
            ("nontechnical", "Nontechnical"),
            ("unknown", "Unknown"),
        ),
        "specialties": (("backend_engineering", "Backend engineering"),),
        "skills": (("python", "Python"),),
        "analyze": analyze,
    }


def test_specialty_gate_uses_unknown_share_before_specialty_filter() -> None:
    arguments = _history_fixture()
    arguments["selected_specialty"] = "backend_engineering"

    result = compare_mvp_history(**arguments)
    specialty = next(
        metric
        for metric in result["metrics"]
        if metric["level"] == "specialty" and metric["id"] == "backend_engineering"
    )
    unknown_gates = [
        gate
        for gate in specialty["quality"]["gates"]
        if gate["id"] == "occupation_unknown_threshold"
    ]

    assert result["selected_metric"]["kind"] == "specialty"
    assert specialty["previous_count"] == specialty["previous_denominator"] == 20
    assert specialty["current_count"] == specialty["current_denominator"] == 20
    assert [gate["observed"] for gate in unknown_gates] == [0.8, 0.8]
    assert all(not gate["passed"] for gate in unknown_gates)
    assert specialty["status"] == "insufficient"
    assert specialty["delta_pp"] is None


def test_unfiltered_skill_metric_is_independent_of_unknown_occupation_gate() -> None:
    arguments = _history_fixture()
    arguments["selected_skill"] = "python"

    result = compare_mvp_history(**arguments)

    assert result["selected_metric"]["kind"] == "skill"
    assert result["selected_metric"]["id"] == "python"
    assert result["status"] == "provisional"
    assert result["previous_count"] == result["current_count"] == 100
    assert result["delta_pp"] == 0
    assert all(
        gate["id"] != "occupation_unknown_threshold" for gate in result["quality"]["common_gates"]
    )


@pytest.mark.parametrize("period_days", [30, 90, 180])
def test_valid_skill_comparison_uses_unfiltered_denominator_and_stable_company_keys(
    period_days: int,
) -> None:
    arguments = _history_fixture(
        period_days,
        descriptions=lambda window, index: (
            "Python services" if index < (4 if window == "previous" else 8) else "Build services"
        ),
    )
    arguments["selected_skill"] = "python"
    result = compare_mvp_history(**arguments)

    assert result["status"] == "provisional"
    assert result["direction"] == "up"
    assert (result["previous_count"], result["current_count"]) == (20, 40)
    metric = next(item for item in result["metrics"] if item["level"] == "skill")
    assert metric["previous_denominator"] == metric["current_denominator"] == 100
    assert result["delta_pp"] == 20


def test_missing_history_is_null_but_measured_absence_is_zero() -> None:
    arguments = _history_fixture(descriptions=lambda window, index: "Build services")
    arguments["selected_skill"] = "python"
    measured = compare_mvp_history(**arguments)
    assert measured["previous_count"] == measured["current_count"] == 0
    assert measured["delta_pp"] is None  # own minimum sample gate fails

    arguments["scans"] = ()
    missing = compare_mvp_history(**arguments)
    assert missing["previous_count"] is missing["current_count"] is None
    assert missing["reason"] == "missing_history"
    assert missing["direction"] == "uncertain"


def test_broad_technical_skill_filter_does_not_require_a_known_specialty() -> None:
    arguments = _history_fixture(title="Software Engineer")
    arguments["selected_occupation"] = "technical_engineering"
    arguments["selected_skill"] = "python"
    result = compare_mvp_history(**arguments)
    assert result["status"] == "provisional"
    assert result["previous_count"] == result["current_count"] == 100
    skill = next(item for item in result["metrics"] if item["level"] == "skill")
    assert all(gate["id"] != "specialty_unresolved_threshold" for gate in skill["quality"]["gates"])


def test_unpaired_or_incomplete_coverage_does_not_emit_a_delta() -> None:
    arguments = _history_fixture()
    arguments["selected_skill"] = "python"
    # A whole board missing from the previous window breaks both coverage gates.
    boundary = arguments["cutoff"] - timedelta(days=30)
    arguments["scans"] = tuple(
        scan
        for scan in arguments["scans"]
        if scan.source_key != "board-0" or scan.finished_at >= boundary
    )
    result = compare_mvp_history(**arguments)
    assert result["status"] == "insufficient"
    assert result["delta_pp"] is None
    assert result["direction"] == "uncertain"


def test_specialty_history_respects_a_conflicting_occupation_filter() -> None:
    arguments = _history_fixture(title="Backend Engineer")
    arguments["selected_occupation"] = "engineering_management"
    arguments["selected_specialty"] = "backend_engineering"
    result = compare_mvp_history(**arguments)
    assert result["selected_metric"]["kind"] == "specialty"
    assert result["previous_count"] == result["current_count"] == 0
    assert result["status"] == "insufficient"
    assert result["delta_pp"] is None


def test_history_uses_the_same_extended_title_classifier_as_current_inventory() -> None:
    arguments = _history_fixture(title="Junior HR Specialist")
    arguments["selected_occupation"] = "nontechnical"
    arguments["selected_skill"] = "python"
    result = compare_mvp_history(**arguments)
    assert result["status"] == "provisional"
    assert result["previous_count"] == result["current_count"] == 100
    assert result["provenance"]["role_classifier_version"] == "3.0.0"

    arguments["scans"] = ()
    missing = compare_mvp_history(**arguments)
    assert missing["previous_count"] is missing["current_count"] is None
    assert missing["provenance"]["role_classifier_version"] == "3.0.0"


def test_history_fingerprint_includes_role_classifier_version(monkeypatch) -> None:
    arguments = _history_fixture()
    initial = compare_mvp_history(**arguments)
    assert initial["provenance"] == compare_mvp_history(**arguments)["provenance"]
    monkeypatch.setattr(
        "career_os.application.radar_mvp_history.MVP_ROLE_CLASSIFIER_VERSION", "3.1.0"
    )
    revised = compare_mvp_history(**arguments)
    assert initial["provenance"]["input_digest"] != revised["provenance"]["input_digest"]
