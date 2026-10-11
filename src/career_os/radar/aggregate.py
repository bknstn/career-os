"""Deterministic roles-only aggregation over immutable Radar scan samples."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from career_os.config.radar_panel import RadarPanelSource
from career_os.domain.canonical import content_hash
from career_os.radar.roles import (
    CLASSIFIER_VERSION,
    FAMILIES,
    ROLE_TAXONOMY,
    ClassificationEvidence,
    RoleClassification,
    classify_role,
)

METRIC_CONTRACT_VERSION: Final = "1.0.0"
AGGREGATION_ALGORITHM_VERSION: Final = "1.0.0"
WINDOW_DAYS: Final = 90
SLOT_DAYS: Final = 7
SLOT_COUNT: Final = 13
OVERALL_COVERAGE_THRESHOLD: Final = 0.90
BOARD_COVERAGE_THRESHOLD: Final = 0.80
UNCLASSIFIED_THRESHOLD: Final = 0.20
ROLE_POSTING_MINIMUM: Final = 20
ROLE_COMPANY_MINIMUM: Final = 5
ROLE_COMPANY_CONCENTRATION_MAXIMUM: Final = 0.40


@dataclass(frozen=True, slots=True)
class RadarObservationInput:
    """One captured title and description from a successful full scan."""

    external_id: str
    observed_at: datetime
    title: str
    description: str


@dataclass(frozen=True, slots=True)
class RadarScanInput:
    """A successful full scan with its exact immutable classifier inputs."""

    run_id: UUID
    source_key: str
    source_kind: str
    finished_at: datetime
    observations: tuple[RadarObservationInput, ...]


@dataclass(frozen=True, slots=True)
class RadarPanelInput:
    """Registered immutable panel identity required for one aggregate build."""

    panel_version: str
    manifest_digest: str
    panel_digest: str
    source_config_digests: dict[str, str]
    canonical_manifest: dict[str, object]
    sources: tuple[RadarPanelSource, ...]


@dataclass(frozen=True, slots=True)
class _SelectedSlot:
    source: RadarPanelSource
    ordinal: int
    starts_at: datetime
    ends_at: datetime
    scan: RadarScanInput | None


def normalize_cutoff(value: datetime) -> datetime:
    """Require an aware instant and return its exact UTC representation."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("cutoff must include a UTC offset")
    return value.astimezone(UTC)


def build_radar_result(
    panel: RadarPanelInput,
    cutoff: datetime,
    scans: tuple[RadarScanInput, ...],
) -> dict[str, object]:
    """Create stable JSON-ready aggregate output from selected frozen scan inputs."""
    normalized_cutoff = normalize_cutoff(cutoff)
    previous_start = normalized_cutoff - timedelta(days=WINDOW_DAYS * 2)
    previous_end = normalized_cutoff - timedelta(days=WINDOW_DAYS)
    current_start = previous_end
    windows: tuple[tuple[str, datetime, datetime], ...] = (
        ("previous", previous_start, previous_end),
        ("current", current_start, normalized_cutoff),
    )
    scans_by_source: dict[str, list[RadarScanInput]] = defaultdict(list)
    panel_sources = {source.source_key: source for source in panel.sources}
    for scan in scans:
        if scan.source_key not in panel_sources:
            raise ValueError(f"captured scan source is outside its panel: {scan.source_key}")
        if scan.source_kind != panel_sources[scan.source_key].source_kind:
            raise ValueError(f"captured scan source kind differs from panel: {scan.source_key}")
        if scan.finished_at.tzinfo is None or scan.finished_at.utcoffset() is None:
            raise ValueError("captured scan finished_at must be timezone-aware")
        scans_by_source[scan.source_key].append(scan)
    for source_scans in scans_by_source.values():
        source_scans.sort(key=lambda scan: (scan.finished_at.astimezone(UTC), str(scan.run_id)))

    selected: dict[str, tuple[_SelectedSlot, ...]] = {}
    for window_name, starts_at, ends_at in windows:
        window_slots: list[_SelectedSlot] = []
        for source in panel.sources:
            source_scans = scans_by_source[source.source_key]
            for ordinal in range(SLOT_COUNT):
                slot_start = starts_at + timedelta(days=ordinal * SLOT_DAYS)
                slot_end = min(slot_start + timedelta(days=SLOT_DAYS), ends_at)
                eligible = next(
                    (
                        scan
                        for scan in source_scans
                        if slot_start <= scan.finished_at.astimezone(UTC) < slot_end
                    ),
                    None,
                )
                window_slots.append(
                    _SelectedSlot(
                        source=source,
                        ordinal=ordinal,
                        starts_at=slot_start,
                        ends_at=slot_end,
                        scan=eligible,
                    )
                )
        selected[window_name] = tuple(window_slots)

    slot_maps = {
        name: {(slot.source.source_key, slot.ordinal): slot for slot in slots}
        for name, slots in selected.items()
    }
    matched_keys = tuple(
        sorted(
            key
            for key, previous in slot_maps["previous"].items()
            if previous.scan is not None and slot_maps["current"][key].scan is not None
        )
    )
    matched_mapping: list[dict[str, object]] = []
    for source_key, ordinal in matched_keys:
        previous_scan = slot_maps["previous"][(source_key, ordinal)].scan
        current_scan = slot_maps["current"][(source_key, ordinal)].scan
        assert previous_scan is not None and current_scan is not None
        matched_mapping.append(
            {
                "source_key": source_key,
                "ordinal": ordinal,
                "previous_run_id": str(previous_scan.run_id),
                "current_run_id": str(current_scan.run_id),
            }
        )

    selected_runs: dict[UUID, RadarScanInput] = {}
    for slots in selected.values():
        for slot in slots:
            if slot.scan is not None:
                selected_runs[slot.scan.run_id] = slot.scan

    selected_inputs = [
        _scan_payload(scan)
        for scan in sorted(selected_runs.values(), key=lambda item: str(item.run_id))
    ]
    slot_assignments = {
        window_name: [_slot_payload(slot) for slot in slots]
        for window_name, slots in selected.items()
    }
    input_digest = content_hash(
        {"slot_assignments": slot_assignments, "selected_inputs": selected_inputs}
    )
    expected_pair_count = len(panel.sources) * SLOT_COUNT
    matched_count = len(matched_keys)
    overall_coverage = _ratio(matched_count, expected_pair_count)
    board_coverage: dict[str, dict[str, int | float]] = {}
    for source in panel.sources:
        source_matched = sum(1 for key in matched_keys if key[0] == source.source_key)
        board_coverage[source.source_key] = {
            "matched_slots": source_matched,
            "expected_slots": SLOT_COUNT,
            "coverage": _ratio(source_matched, SLOT_COUNT),
        }

    window_results: dict[str, dict[str, object]] = {}
    for window_name, _, _ in windows:
        matched_runs: set[UUID] = set()
        for key in matched_keys:
            selected_scan = slot_maps[window_name][key].scan
            assert selected_scan is not None
            matched_runs.add(selected_scan.run_id)
        window_observations: dict[tuple[str, str], tuple[RadarObservationInput, UUID]] = {}
        for run_id in matched_runs:
            scan = selected_runs[run_id]
            for observation in scan.observations:
                identity = (scan.source_key, observation.external_id)
                previous = window_observations.get(identity)
                if previous is None or (
                    observation.observed_at.astimezone(UTC),
                    str(scan.run_id),
                ) > (previous[0].observed_at.astimezone(UTC), str(previous[1])):
                    window_observations[identity] = (observation, scan.run_id)
        classifications = _classify_observations(window_observations)
        window_results[window_name] = _window_aggregate(
            panel_sources=panel_sources,
            classifications=classifications,
            matched_count=matched_count,
            expected_count=expected_pair_count,
            board_coverage=board_coverage,
            selected_count=sum(1 for slot in selected[window_name] if slot.scan is not None),
            window_slots=selected[window_name],
        )
        window_results[window_name]["input_digest"] = content_hash(
            {
                "slot_assignments": {window_name: slot_assignments[window_name]},
                "selected_inputs": [
                    payload
                    for payload in selected_inputs
                    if any(
                        slot.scan is not None and str(slot.scan.run_id) == payload["run_id"]
                        for slot in selected[window_name]
                    )
                ],
            }
        )

    versions = {
        "metric_contract": METRIC_CONTRACT_VERSION,
        "aggregation_algorithm": AGGREGATION_ALGORITHM_VERSION,
        "classifier": CLASSIFIER_VERSION,
        "taxonomy": ROLE_TAXONOMY.version,
    }
    actual_versions: set[tuple[str, str]] = set()
    for window in window_results.values():
        window_classifications = window["classifications"]
        assert isinstance(window_classifications, list)
        for item in window_classifications:
            assert isinstance(item, dict)
            classification = item["classification"]
            assert isinstance(classification, dict)
            actual_versions.add(
                (str(classification["classifier_version"]), str(classification["taxonomy_version"]))
            )
    expected_versions = {(CLASSIFIER_VERSION, ROLE_TAXONOMY.version)}
    if actual_versions - expected_versions:
        raise ValueError("classifier output versions differ from the persisted build versions")

    run_gates = _run_gates(
        versions=versions,
        overall_coverage=overall_coverage,
        board_coverage=board_coverage,
        matched_count=matched_count,
        expected_count=expected_pair_count,
        windows=window_results,
    )
    run_quality_state = (
        "provisional" if all(gate["passed"] for gate in run_gates) else "insufficient"
    )
    roles = _role_comparisons(window_results, run_quality_state)

    return {
        "schema_version": "1.0.0",
        "status": "provisional" if run_quality_state == "provisional" else "insufficient",
        "run_quality_state": run_quality_state,
        "panel": {
            "panel_version": panel.panel_version,
            "manifest_digest": panel.manifest_digest,
            "panel_digest": panel.panel_digest,
            "source_config_digests": dict(sorted(panel.source_config_digests.items())),
            "manifest": panel.canonical_manifest,
        },
        "cutoff_utc": _timestamp(normalized_cutoff),
        "metric": {
            "id": "distinct_observed_posting_share",
            "definition": "Distinct source postings observed in selected matched weekly snapshots.",
            "window_days": WINDOW_DAYS,
            "slot_days": SLOT_DAYS,
            "slot_count": SLOT_COUNT,
            "identity": ["source_key", "external_id"],
        },
        "versions": versions,
        "windows": window_results,
        "matched_pair_count": matched_count,
        "expected_pair_count": expected_pair_count,
        "matched_pair_coverage": overall_coverage,
        "board_coverage": board_coverage,
        "matched_pairs": matched_mapping,
        "selected_inputs": selected_inputs,
        "input_digest": input_digest,
        "gates": {"run": run_gates},
        "roles": roles,
    }


def idempotence_key(payload: dict[str, object]) -> str:
    """Hash exact metric, panel, window, version, and selected-input identity."""
    panel = payload["panel"]
    cutoff = payload["cutoff_utc"]
    windows = payload["windows"]
    versions = payload["versions"]
    assert isinstance(panel, dict)
    assert isinstance(windows, dict)
    assert isinstance(versions, dict)
    return content_hash(
        {
            "metric_contract_version": versions["metric_contract"],
            "aggregation_algorithm_version": versions["aggregation_algorithm"],
            "classifier_version": versions["classifier"],
            "taxonomy_version": versions["taxonomy"],
            "cutoff_utc": cutoff,
            "window_boundaries": {name: window["bounds"] for name, window in windows.items()},
            "panel_digest": panel["panel_digest"],
            "input_digest": payload["input_digest"],
        }
    )


def _classify_observations(
    observations: dict[tuple[str, str], tuple[RadarObservationInput, UUID]],
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for (source_key, external_id), (observation, run_id) in sorted(observations.items()):
        classification = classify_role(observation.title, observation.description)
        output.append(
            {
                "source_key": source_key,
                "external_id": external_id,
                "run_id": str(run_id),
                "observed_at": _timestamp(observation.observed_at),
                "title": observation.title,
                "description": observation.description,
                "classification": _classification_payload(classification),
            }
        )
    return output


def _classification_payload(value: RoleClassification) -> dict[str, object]:
    return {
        "classifier_version": value.classifier_version,
        "taxonomy_version": value.taxonomy_version,
        "role_family": value.role_family.value if value.role_family is not None else None,
        "status": value.status.value,
        "scope": value.scope.value,
        "seniority": value.seniority.value if value.seniority is not None else None,
        "managerial_function": value.managerial_function,
        "context": [item.value for item in value.context],
        "evidence": [_evidence_payload(item) for item in value.evidence],
    }


def _evidence_payload(value: ClassificationEvidence) -> dict[str, str]:
    return {
        "axis": value.axis.value,
        "source": value.source.value,
        "rule_id": value.rule_id,
        "matched_text": value.matched_text,
    }


def _window_aggregate(
    *,
    panel_sources: dict[str, RadarPanelSource],
    classifications: list[dict[str, object]],
    matched_count: int,
    expected_count: int,
    board_coverage: dict[str, dict[str, int | float]],
    selected_count: int,
    window_slots: tuple[_SelectedSlot, ...],
) -> dict[str, object]:
    evidence_available = matched_count > 0
    in_scope: list[dict[str, object]] = []
    out_of_scope = 0
    unclassified = 0
    role_counts: dict[str, list[dict[str, object]]] = {family.id.value: [] for family in FAMILIES}
    for item in classifications:
        classification = item["classification"]
        assert isinstance(classification, dict)
        scope = classification["scope"]
        family = classification["role_family"]
        if scope == "out_of_scope":
            out_of_scope += 1
            continue
        in_scope.append(item)
        if family is None:
            unclassified += 1
        else:
            role_counts[str(family)].append(item)

    denominator = len(in_scope) if evidence_available else None
    unclassified_count = unclassified if evidence_available else None
    out_of_scope_count = out_of_scope if evidence_available else None
    family_results: dict[str, dict[str, object]] = {}
    for family in FAMILIES:
        family_id = family.id.value
        records = role_counts[family_id]
        company_counts: dict[str, int] = defaultdict(int)
        for item in records:
            company_counts[panel_sources[str(item["source_key"])].company_key] += 1
        role_count = len(records) if evidence_available else None
        max_company_count = max(company_counts.values(), default=0)
        concentration = (
            (_ratio(max_company_count, len(records)) if records else None)
            if evidence_available
            else None
        )
        family_results[family_id] = {
            "count": role_count,
            "share": _ratio(len(records), denominator)
            if evidence_available and denominator not in {None, 0}
            else None,
            "company_count": len(company_counts) if evidence_available else None,
            "companies": dict(sorted(company_counts.items())) if evidence_available else None,
            "largest_company_count": max_company_count if evidence_available else None,
            "largest_company_share": concentration,
        }

    unclassified_share = (
        _ratio(unclassified, denominator)
        if evidence_available and denominator not in {None, 0}
        else None
    )
    source_slot_coverage = {
        source_key: coverage["coverage"] for source_key, coverage in board_coverage.items()
    }
    return {
        "bounds": {
            "starts_at": _timestamp(window_slots[0].starts_at),
            "ends_at": _timestamp(window_slots[-1].ends_at),
            "boundary": "[starts_at, ends_at)",
        },
        "expected_slots": [_slot_payload(slot) for slot in window_slots],
        "selected_slot_count": selected_count,
        "matched_slot_count": matched_count,
        "expected_slot_count": expected_count,
        "coverage": {
            "matched_pairs": matched_count,
            "expected_pairs": expected_count,
            "overall": _ratio(matched_count, expected_count),
            "per_board": source_slot_coverage,
        },
        "denominator": denominator,
        "role_counts": family_results,
        "unclassified_count": unclassified_count,
        "unclassified_share": unclassified_share,
        "out_of_scope_count": out_of_scope_count,
        "classifications": classifications if evidence_available else [],
    }


def _run_gates(
    *,
    versions: dict[str, str],
    overall_coverage: float,
    board_coverage: dict[str, dict[str, int | float]],
    matched_count: int,
    expected_count: int,
    windows: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    gates: list[dict[str, object]] = [
        {"code": "panel_match", "passed": True},
        {
            "code": "classifier_version_mismatch",
            "passed": versions["classifier"] == CLASSIFIER_VERSION
            and versions["taxonomy"] == ROLE_TAXONOMY.version,
        },
    ]
    selected_total = 0
    for window in windows.values():
        selected_slots = window["selected_slot_count"]
        assert isinstance(selected_slots, int)
        selected_total += selected_slots
    gates.append(
        {
            "code": "missing_history",
            "passed": matched_count > 0,
            "selected_slots": selected_total,
            "matched_pairs": matched_count,
        }
    )
    gates.append(
        {
            "code": "coverage_below_threshold",
            "passed": overall_coverage >= OVERALL_COVERAGE_THRESHOLD,
            "matched_pairs": matched_count,
            "expected_pairs": expected_count,
            "coverage": overall_coverage,
            "minimum": OVERALL_COVERAGE_THRESHOLD,
        }
    )
    for source_key, coverage in sorted(board_coverage.items()):
        value = float(coverage["coverage"])
        gates.append(
            {
                "code": "coverage_below_threshold",
                "scope": "board",
                "source_key": source_key,
                "passed": value >= BOARD_COVERAGE_THRESHOLD,
                "coverage": value,
                "minimum": BOARD_COVERAGE_THRESHOLD,
            }
        )
    for window_name, window in windows.items():
        denominator = window["denominator"]
        unclassified = window["unclassified_count"]
        matched_slots = window["matched_slot_count"]
        assert isinstance(matched_slots, int)
        has_evidence = matched_slots > 0
        rate = window["unclassified_share"]
        rate_is_below_threshold = isinstance(rate, (int, float)) and rate <= (
            UNCLASSIFIED_THRESHOLD
        )
        gates.append(
            {
                "code": "unclassified_above_threshold",
                "window": window_name,
                "passed": bool(
                    has_evidence and denominator not in {None, 0} and rate_is_below_threshold
                ),
                "unclassified_count": unclassified,
                "denominator": denominator,
                "share": rate,
                "maximum": UNCLASSIFIED_THRESHOLD,
            }
        )
        if denominator == 0:
            gates.append(
                {
                    "code": "zero_denominator",
                    "window": window_name,
                    "passed": False,
                    "denominator": 0,
                }
            )
    return gates


def _role_comparisons(
    windows: dict[str, dict[str, object]], run_quality_state: str
) -> dict[str, dict[str, object]]:
    results: dict[str, dict[str, object]] = {}
    for family in FAMILIES:
        family_id = family.id.value
        window_values: dict[str, dict[str, object]] = {}
        gates: list[dict[str, object]] = []
        for window_name in ("previous", "current"):
            counts = windows[window_name]["role_counts"]
            assert isinstance(counts, dict)
            value = counts[family_id]
            assert isinstance(value, dict)
            count = value["count"]
            companies = value["company_count"]
            concentration = value["largest_company_share"]
            sample_passed = count is not None and int(count) >= ROLE_POSTING_MINIMUM
            company_passed = companies is not None and int(companies) >= ROLE_COMPANY_MINIMUM
            concentration_passed = concentration is not None and float(concentration) <= (
                ROLE_COMPANY_CONCENTRATION_MAXIMUM
            )
            window_values[window_name] = value
            gates.extend(
                (
                    {
                        "code": "sample_below_threshold",
                        "window": window_name,
                        "passed": sample_passed,
                        "postings": count,
                        "minimum": ROLE_POSTING_MINIMUM,
                    },
                    {
                        "code": "company_count_below_threshold",
                        "window": window_name,
                        "passed": company_passed,
                        "companies": companies,
                        "minimum": ROLE_COMPANY_MINIMUM,
                    },
                    {
                        "code": "company_concentration_above_threshold",
                        "window": window_name,
                        "passed": concentration_passed,
                        "largest_company_share": concentration,
                        "maximum": ROLE_COMPANY_CONCENTRATION_MAXIMUM,
                    },
                )
            )
        quality_passed = run_quality_state == "provisional" and all(
            bool(gate["passed"]) for gate in gates
        )
        previous_share = window_values["previous"]["share"]
        current_share = window_values["current"]["share"]
        delta: float | None = None
        if (
            quality_passed
            and isinstance(previous_share, (int, float))
            and isinstance(current_share, (int, float))
        ):
            delta = round((float(current_share) - float(previous_share)) * 100, 6)
        results[family_id] = {
            "previous": window_values["previous"],
            "current": window_values["current"],
            "delta_pp": delta,
            "role_quality_state": "provisional" if quality_passed else "insufficient",
            "gates": gates,
        }
    return results


def _slot_payload(slot: _SelectedSlot) -> dict[str, object]:
    return {
        "source_key": slot.source.source_key,
        "company_key": slot.source.company_key,
        "source_kind": slot.source.source_kind,
        "ordinal": slot.ordinal,
        "starts_at": _timestamp(slot.starts_at),
        "ends_at": _timestamp(slot.ends_at),
        "run_id": str(slot.scan.run_id) if slot.scan is not None else None,
        "finished_at": _timestamp(slot.scan.finished_at) if slot.scan is not None else None,
        "missing_reason": None if slot.scan is not None else "no_successful_full_scan",
    }


def _scan_payload(scan: RadarScanInput) -> dict[str, object]:
    return {
        "run_id": str(scan.run_id),
        "source_key": scan.source_key,
        "source_kind": scan.source_kind,
        "finished_at": _timestamp(scan.finished_at),
        "observations": [
            {
                "external_id": observation.external_id,
                "observed_at": _timestamp(observation.observed_at),
                "title": observation.title,
                "description": observation.description,
            }
            for observation in sorted(scan.observations, key=lambda item: item.external_id)
        ],
    }


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 9)


def _timestamp(value: datetime) -> str:
    return normalize_cutoff(value).isoformat().replace("+00:00", "Z")
