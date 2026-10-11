"""Independent pure-oracle checks for the fixed R1 role aggregation contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from career_os.config.radar_panel import RadarPanelSource
from career_os.domain.canonical import content_hash
from career_os.radar.aggregate import (
    RadarObservationInput,
    RadarPanelInput,
    RadarScanInput,
    build_radar_result,
    idempotence_key,
)


def test_weekly_matched_snapshots_deduplicate_postings_and_compute_percent_points() -> None:
    panel = _panel(5)
    cutoff = datetime(2026, 10, 11, tzinfo=UTC)
    previous_backend = [4, 4, 4, 4, 4]
    current_backend = [5, 5, 5, 5, 4]
    scans = _balanced_scans(panel, cutoff, previous_backend, current_backend)

    result = build_radar_result(panel, cutoff, scans)
    reversed_result = build_radar_result(panel, cutoff, tuple(reversed(scans)))
    backend = result["roles"]["backend_engineering"]

    assert result["run_quality_state"] == "provisional"
    assert result["matched_pair_count"] == 60
    assert result["expected_pair_count"] == 65
    assert result["matched_pair_coverage"] == round(60 / 65, 9)
    assert backend["previous"]["count"] == 20
    assert backend["current"]["count"] == 24
    assert backend["previous"]["share"] == 0.1
    assert backend["current"]["share"] == 0.12
    assert backend["delta_pp"] == 2.0
    assert backend["role_quality_state"] == "provisional"
    assert result["input_digest"] == reversed_result["input_digest"]
    assert idempotence_key(result) == idempotence_key(reversed_result)

    redundant = _scan(
        panel.sources[0],
        cutoff - timedelta(days=180) + timedelta(days=2),
        "redundant-later-run",
        (("source-0-posting-0", "Backend Engineer"),),
    )
    with_redundant = build_radar_result(panel, cutoff, (*scans, redundant))
    assert with_redundant["input_digest"] == result["input_digest"]
    assert idempotence_key(with_redundant) == idempotence_key(result)


def test_windows_are_half_open_and_the_thirteenth_slot_is_six_days() -> None:
    panel = _panel(1)
    cutoff = datetime(2026, 10, 11, tzinfo=UTC)
    previous_edge = cutoff - timedelta(days=180)
    comparison_edge = cutoff - timedelta(days=90)
    scans = (
        _scan(
            panel.sources[0],
            previous_edge,
            "prev-edge",
            (("left", "Backend Engineer"),),
        ),
        _scan(
            panel.sources[0],
            comparison_edge,
            "current-edge",
            (("middle", "Data Engineer"),),
        ),
        _scan(
            panel.sources[0],
            cutoff,
            "cutoff-edge",
            (("right", "Backend Engineer"),),
        ),
    )

    result = build_radar_result(panel, cutoff, scans)
    previous_slots = result["windows"]["previous"]["expected_slots"]
    current_slots = result["windows"]["current"]["expected_slots"]

    assert previous_slots[0]["run_id"] == str(scans[0].run_id)
    assert current_slots[0]["run_id"] == str(scans[1].run_id)
    assert previous_slots[-1]["ends_at"] == current_slots[0]["starts_at"]
    assert current_slots[-1]["starts_at"] == (cutoff - timedelta(days=6)).isoformat().replace(
        "+00:00", "Z"
    )
    assert current_slots[-1]["ends_at"] == cutoff.isoformat().replace("+00:00", "Z")
    assert all(item["run_id"] != str(scans[2].run_id) for item in result["selected_inputs"])


def test_no_matched_evidence_is_null_but_observed_empty_scan_is_zero() -> None:
    panel = _panel(1)
    cutoff = datetime(2026, 10, 11, tzinfo=UTC)
    previous_only = _scan(
        panel.sources[0],
        cutoff - timedelta(days=179),
        "old",
        (("known", "Backend Engineer"),),
    )
    no_pair = build_radar_result(panel, cutoff, (previous_only,))
    assert no_pair["run_quality_state"] == "insufficient"
    assert no_pair["windows"]["previous"]["denominator"] is None
    assert no_pair["windows"]["current"]["denominator"] is None
    assert not _gate(no_pair, "missing_history")["passed"]

    current_empty = _scan(
        panel.sources[0],
        cutoff - timedelta(days=89),
        "empty",
        (),
    )
    observed_zero = build_radar_result(panel, cutoff, (previous_only, current_empty))
    backend = observed_zero["roles"]["backend_engineering"]
    assert observed_zero["windows"]["previous"]["denominator"] == 1
    assert observed_zero["windows"]["current"]["denominator"] == 0
    assert backend["current"]["count"] == 0
    assert backend["current"]["share"] is None
    assert observed_zero["windows"]["current"]["unclassified_share"] is None
    assert any(
        gate["code"] == "zero_denominator" and gate["window"] == "current"
        for gate in observed_zero["gates"]["run"]
    )


def test_latest_observation_is_classified_and_company_keys_do_not_deduplicate_postings() -> None:
    one_company = _panel(2, shared_company=True)
    cutoff = datetime(2026, 10, 11, tzinfo=UTC)
    before = cutoff - timedelta(days=179)
    after = cutoff - timedelta(days=172)
    previous_a = _scan(one_company.sources[0], before, "a-first", (("same", "Backend Engineer"),))
    previous_b = _scan(one_company.sources[0], after, "a-last", (("same", "Product Manager"),))
    previous_second_board = _scan(
        one_company.sources[1], before, "b-first", (("same", "Backend Engineer"),)
    )
    current_a = _scan(
        one_company.sources[0],
        cutoff - timedelta(days=89),
        "current-a",
        (("same", "Backend Engineer"),),
    )
    current_b = _scan(
        one_company.sources[1],
        cutoff - timedelta(days=89),
        "current-b",
        (("same", "Backend Engineer"),),
    )
    current_a_last = _scan(
        one_company.sources[0],
        cutoff - timedelta(days=82),
        "current-a-last",
        (("same", "Backend Engineer"),),
    )

    result = build_radar_result(
        one_company,
        cutoff,
        (previous_a, previous_b, previous_second_board, current_a, current_b, current_a_last),
    )
    current = result["windows"]["current"]

    assert result["windows"]["previous"]["out_of_scope_count"] == 1
    assert result["windows"]["previous"]["denominator"] == 1
    prior_classifications = result["windows"]["previous"]["classifications"]
    assert next(item for item in prior_classifications if item["source_key"] == "source-0")[
        "title"
    ] == ("Product Manager")
    assert current["denominator"] == 2
    assert current["role_counts"]["backend_engineering"]["count"] == 2
    assert current["role_counts"]["backend_engineering"]["company_count"] == 1


def test_equal_timestamp_uses_smallest_run_id_and_unclassified_remains_in_denominator() -> None:
    panel = _panel(1)
    cutoff = datetime(2026, 10, 11, tzinfo=UTC)
    first_at = cutoff - timedelta(days=179)
    lower = _scan(
        panel.sources[0], first_at, "tie-lower", (("x", "Software Engineer"),), run_id=UUID(int=1)
    )
    higher = _scan(
        panel.sources[0], first_at, "tie-higher", (("x", "Backend Engineer"),), run_id=UUID(int=2)
    )
    current = _scan(
        panel.sources[0],
        cutoff - timedelta(days=89),
        "current",
        (("x", "Software Engineer"),),
    )

    result = build_radar_result(panel, cutoff, (higher, lower, current))
    previous = result["windows"]["previous"]

    assert previous["classifications"][0]["run_id"] == str(min(lower.run_id, higher.run_id))
    assert previous["denominator"] == 1
    assert previous["unclassified_count"] == 1
    assert previous["unclassified_share"] == 1.0
    assert not next(
        gate
        for gate in result["gates"]["run"]
        if gate["code"] == "unclassified_above_threshold" and gate["window"] == "previous"
    )["passed"]


def test_quality_thresholds_are_inclusive_and_coverage_is_matched_by_board_slot() -> None:
    panel = _panel(10)
    cutoff = datetime(2026, 10, 11, tzinfo=UTC)
    full = _balanced_scans(panel, cutoff, [2] * 10, [2] * 10)
    reduced = tuple(
        scan
        for scan in full
        if not (
            scan.source_key in {"source-0", "source-1", "source-2"}
            and scan.run_id
            in {
                _scan_id("previous", int(scan.source_key[-1]), 11),
                _scan_id("current", int(scan.source_key[-1]), 11),
            }
        )
    )

    exact_ninety = build_radar_result(panel, cutoff, reduced)
    assert exact_ninety["matched_pair_count"] == 117
    assert exact_ninety["matched_pair_coverage"] == 0.9
    assert exact_ninety["run_quality_state"] == "provisional"
    for source_key in ("source-0", "source-1", "source-2"):
        assert exact_ninety["board_coverage"][source_key]["matched_slots"] == 11

    below_ninety = build_radar_result(
        panel,
        cutoff,
        tuple(
            scan
            for scan in reduced
            if scan.run_id
            not in {
                _scan_id("previous", 0, 10),
                _scan_id("current", 0, 10),
            }
        ),
    )
    assert below_ninety["matched_pair_count"] == 116
    assert below_ninety["run_quality_state"] == "insufficient"
    assert below_ninety["board_coverage"]["source-0"]["matched_slots"] == 10


def test_sample_company_concentration_and_unclassified_limits_accept_exact_thresholds() -> None:
    panel = _panel(5)
    cutoff = datetime(2026, 10, 11, tzinfo=UTC)
    scans: list[RadarScanInput] = []
    for source_index, source in enumerate(panel.sources):
        for window_name, start in (
            ("previous", cutoff - timedelta(days=180)),
            ("current", cutoff - timedelta(days=90)),
        ):
            specs = tuple(
                (
                    f"{source.source_key}-posting-{index}",
                    "Backend Engineer"
                    if index < (8 if source_index == 0 else 3)
                    else "Software Engineer"
                    if 32 <= index < 40
                    else "Data Engineer",
                )
                for index in range(40)
            )
            for ordinal in range(12):
                scans.append(
                    _scan(
                        source,
                        start + timedelta(days=ordinal * 7 + 1),
                        f"edge-{window_name}-{source_index}-{ordinal}",
                        specs,
                    )
                )

    result = build_radar_result(panel, cutoff, tuple(scans))
    backend = result["roles"]["backend_engineering"]

    assert result["run_quality_state"] == "provisional"
    assert result["windows"]["previous"]["unclassified_count"] == 40
    assert result["windows"]["previous"]["unclassified_share"] == 0.2
    assert backend["previous"]["count"] == 20
    assert backend["previous"]["company_count"] == 5
    assert backend["previous"]["largest_company_share"] == 0.4
    assert backend["role_quality_state"] == "provisional"

    changed_scans = tuple(
        RadarScanInput(
            run_id=scan.run_id,
            source_key=scan.source_key,
            source_kind=scan.source_kind,
            finished_at=scan.finished_at,
            observations=tuple(
                RadarObservationInput(
                    external_id=observation.external_id,
                    observed_at=observation.observed_at,
                    title=(
                        "Software Engineer"
                        if scan.source_key == "source-4"
                        and observation.external_id == "source-4-posting-31"
                        else observation.title
                    ),
                    description=observation.description,
                )
                for observation in scan.observations
            ),
        )
        for scan in scans
    )
    over_unclassified = build_radar_result(panel, cutoff, changed_scans)
    assert over_unclassified["windows"]["previous"]["unclassified_count"] == 41
    assert over_unclassified["run_quality_state"] == "insufficient"


def _scan_id(window_name: str, source_index: int, ordinal: int) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"https://example.test/runs/{window_name}-{source_index}-{ordinal}",
    )


def _panel(count: int, *, shared_company: bool = False) -> RadarPanelInput:
    sources = tuple(
        RadarPanelSource(
            source_key=f"source-{index}",
            company_key="shared-company" if shared_company else f"company-{index}",
            source_kind="greenhouse",
        )
        for index in range(count)
    )
    canonical = {
        "panel_version": "r1-v1",
        "sources": [
            {
                "source_key": source.source_key,
                "company_key": source.company_key,
                "source_kind": source.source_kind,
            }
            for source in sources
        ],
    }
    return RadarPanelInput(
        panel_version="r1-v1",
        manifest_digest=content_hash(canonical),
        panel_digest=content_hash({"manifest": canonical, "config": "frozen"}),
        source_config_digests={source.source_key: "a" * 64 for source in sources},
        canonical_manifest=canonical,
        sources=sources,
    )


def _balanced_scans(
    panel: RadarPanelInput,
    cutoff: datetime,
    previous_backend_by_company: list[int],
    current_backend_by_company: list[int],
) -> tuple[RadarScanInput, ...]:
    scans: list[RadarScanInput] = []
    previous_start = cutoff - timedelta(days=180)
    current_start = cutoff - timedelta(days=90)
    for source_index, source in enumerate(panel.sources):
        for window_name, window_start, backend_count in (
            ("previous", previous_start, previous_backend_by_company[source_index]),
            ("current", current_start, current_backend_by_company[source_index]),
        ):
            specs = tuple(
                (
                    f"{source.source_key}-posting-{index}",
                    "Backend Engineer" if index < backend_count else "Data Engineer",
                )
                for index in range(40)
            )
            for ordinal in range(12):
                scans.append(
                    _scan(
                        source,
                        window_start + timedelta(days=ordinal * 7 + 1),
                        f"{window_name}-{source_index}-{ordinal}",
                        specs,
                    )
                )
    return tuple(scans)


def _scan(
    source: RadarPanelSource,
    finished_at: datetime,
    run_label: str,
    postings: tuple[tuple[str, str], ...],
    *,
    run_id: UUID | None = None,
) -> RadarScanInput:
    run_id = run_id or uuid5(NAMESPACE_URL, f"https://example.test/runs/{run_label}")
    return RadarScanInput(
        run_id=run_id,
        source_key=source.source_key,
        source_kind=source.source_kind,
        finished_at=finished_at,
        observations=tuple(
            RadarObservationInput(
                external_id=external_id,
                observed_at=finished_at,
                title=role_title,
                description="",
            )
            for external_id, role_title in postings
        ),
    )


def _gate(result: dict[str, object], code: str) -> dict[str, object]:
    return next(gate for gate in result["gates"]["run"] if gate["code"] == code)
