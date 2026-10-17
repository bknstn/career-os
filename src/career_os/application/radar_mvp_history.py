"""Versioned 30/90/180-day Career Radar MVP comparisons over immutable scans."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, TypedDict

from career_os.config.radar_panel import RadarPanelSource
from career_os.domain.canonical import content_hash
from career_os.domain.skills import SkillEvidence
from career_os.persistence.radar_mvp import RadarMvpSource
from career_os.radar.aggregate import RadarObservationInput, RadarPanelInput, RadarScanInput
from career_os.radar.role_contract import RoleAssignment

type PostingAnalysis = tuple[RoleAssignment, tuple[SkillEvidence, ...]]
type AnalyzePosting = Callable[[str, str], PostingAnalysis]

MVP_HISTORY_CONTRACT_VERSION: Final = "1.0.0"
MVP_HISTORY_ALGORITHM_VERSION: Final = "1.0.0"
SUPPORTED_PERIOD_DAYS: Final = (30, 90, 180)
SLOT_DAYS: Final = 7
OVERALL_COVERAGE_MINIMUM: Final = 0.90
BOARD_COVERAGE_MINIMUM: Final = 0.80
OCCUPATION_UNKNOWN_MAXIMUM: Final = 0.20
SPECIALTY_UNRESOLVED_MAXIMUM: Final = 0.20
DESCRIPTION_MISSING_MAXIMUM: Final = 0.20
GROUP_POSTING_MINIMUM: Final = 20
GROUP_COMPANY_MINIMUM: Final = 5
GROUP_COMPANY_CONCENTRATION_MAXIMUM: Final = 0.40
STABLE_BAND_PP: Final = 1.0


@dataclass(frozen=True, slots=True)
class _MatchedPair:
    source_key: str
    ordinal: int
    previous: RadarScanInput
    current: RadarScanInput


@dataclass(frozen=True, slots=True)
class _Posting:
    source_key: str
    external_id: str
    title: str
    description: str
    company_key: str
    company_name: str
    occupation: str
    specialty: str | None
    specialty_state: str
    skill_ids: frozenset[str]


type PostingPair = tuple[tuple[_Posting, ...], tuple[_Posting, ...]]


class _Selection(TypedDict):
    period_days: int
    slot_days: int
    slots_per_window: int
    windows: dict[str, dict[str, str]]
    slot_assignment_digest: str


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 9) if denominator else None


def _common_gates(
    *,
    matched_pairs: tuple[_MatchedPair, ...],
    expected_pair_count: int,
    selected_source_keys: tuple[str, ...],
) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
    matched_by_source = Counter(pair.source_key for pair in matched_pairs)
    overall = _ratio(len(matched_pairs), expected_pair_count)
    gates: list[dict[str, object]] = [
        {
            "id": "history_present",
            "passed": bool(matched_pairs),
            "observed": len(matched_pairs),
            "expected": expected_pair_count,
        },
        {
            "id": "overall_coverage",
            "passed": overall is not None and overall >= OVERALL_COVERAGE_MINIMUM,
            "observed": overall,
            "threshold": OVERALL_COVERAGE_MINIMUM,
        },
    ]
    by_source: dict[str, dict[str, object]] = {}
    for source_key in selected_source_keys:
        matched = matched_by_source[source_key]
        coverage = _ratio(matched, expected_pair_count // len(selected_source_keys))
        passed = coverage is not None and coverage >= BOARD_COVERAGE_MINIMUM
        by_source[source_key] = {
            "matched_slots": matched,
            "expected_slots": expected_pair_count // len(selected_source_keys),
            "coverage": coverage,
            "passed": passed,
        }
        gates.append(
            {
                "id": "board_coverage",
                "source_key": source_key,
                "passed": passed,
                "observed": coverage,
                "threshold": BOARD_COVERAGE_MINIMUM,
            }
        )
    return gates, by_source


def _select_pairs(
    *,
    panel: RadarPanelInput,
    scans: tuple[RadarScanInput, ...],
    cutoff: datetime,
    period_days: int,
    selected_source_keys: tuple[str, ...],
) -> tuple[tuple[_MatchedPair, ...], int, _Selection]:
    if period_days not in SUPPORTED_PERIOD_DAYS:
        raise ValueError("period_days must be 30, 90, or 180")
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("history cutoff must be timezone-aware")
    end = cutoff.astimezone(UTC)
    slot_count = (period_days + SLOT_DAYS - 1) // SLOT_DAYS
    source_map = {source.source_key: source for source in panel.sources}
    if any(source_key not in source_map for source_key in selected_source_keys):
        raise ValueError("selected source is outside the registered MVP panel")
    for scan in scans:
        source = source_map.get(scan.source_key)
        if source is None or source.source_kind != scan.source_kind:
            raise ValueError("history scan does not match its registered panel source")
        if scan.finished_at.tzinfo is None or scan.finished_at.utcoffset() is None:
            raise ValueError("history scan timestamp must be timezone-aware")
    ordered: dict[str, tuple[RadarScanInput, ...]] = {}
    for source_key in selected_source_keys:
        ordered[source_key] = tuple(
            sorted(
                (scan for scan in scans if scan.source_key == source_key),
                key=lambda scan: (scan.finished_at.astimezone(UTC), str(scan.run_id)),
            )
        )

    selected_windows: dict[str, dict[tuple[str, int], RadarScanInput | None]] = {
        "previous": {},
        "current": {},
    }
    boundaries: dict[str, dict[str, str]] = {}
    for window_name, window_start, window_end in (
        ("previous", end - timedelta(days=2 * period_days), end - timedelta(days=period_days)),
        ("current", end - timedelta(days=period_days), end),
    ):
        boundaries[window_name] = {
            "start_utc": window_start.isoformat(),
            "end_utc": window_end.isoformat(),
        }
        for source_key in selected_source_keys:
            source_scans = ordered[source_key]
            for ordinal in range(slot_count):
                slot_start = window_start + timedelta(days=ordinal * SLOT_DAYS)
                slot_end = min(slot_start + timedelta(days=SLOT_DAYS), window_end)
                selected = next(
                    (
                        scan
                        for scan in source_scans
                        if slot_start <= scan.finished_at.astimezone(UTC) < slot_end
                    ),
                    None,
                )
                selected_windows[window_name][(source_key, ordinal)] = selected

    matched_keys = tuple(
        sorted(
            key
            for key, previous in selected_windows["previous"].items()
            if previous is not None and selected_windows["current"][key] is not None
        )
    )
    matched: list[_MatchedPair] = []
    for source_key, ordinal in matched_keys:
        previous = selected_windows["previous"][(source_key, ordinal)]
        current = selected_windows["current"][(source_key, ordinal)]
        if previous is not None and current is not None:
            matched.append(_MatchedPair(source_key, ordinal, previous, current))
    pairs = tuple(matched)
    expected = len(selected_source_keys) * slot_count
    assignments = {
        name: [
            {
                "source_key": source_key,
                "ordinal": ordinal,
                "run_id": None if scan is None else str(scan.run_id),
            }
            for (source_key, ordinal), scan in sorted(window.items())
        ]
        for name, window in selected_windows.items()
    }
    return (
        pairs,
        expected,
        {
            "period_days": period_days,
            "slot_days": SLOT_DAYS,
            "slots_per_window": slot_count,
            "windows": boundaries,
            "slot_assignment_digest": content_hash(assignments),
        },
    )


def _postings_for_window(
    *,
    pairs: tuple[_MatchedPair, ...],
    window_name: str,
    panel_sources: dict[str, RadarPanelSource],
    sources: dict[str, RadarMvpSource],
    analyze: AnalyzePosting,
) -> tuple[_Posting, ...] | None:
    if not pairs:
        return None
    latest: dict[tuple[str, str], tuple[datetime, str, RadarObservationInput]] = {}
    for pair in pairs:
        scan = pair.previous if window_name == "previous" else pair.current
        for observation in scan.observations:
            if (
                observation.observed_at.tzinfo is None
                or observation.observed_at.utcoffset() is None
            ):
                raise ValueError("history observation timestamp must be timezone-aware")
            identity = (scan.source_key, observation.external_id)
            candidate = (observation.observed_at.astimezone(UTC), str(scan.run_id), observation)
            existing = latest.get(identity)
            if existing is None or candidate[:2] > existing[:2]:
                latest[identity] = candidate
    result: list[_Posting] = []
    for (source_key, external_id), (_observed_at, _run_id, observation) in sorted(latest.items()):
        analysis = analyze(observation.title, observation.description)
        panel_source = panel_sources[source_key]
        source = sources.get(source_key)
        result.append(
            _Posting(
                source_key=source_key,
                external_id=external_id,
                title=observation.title,
                description=observation.description,
                company_key=panel_source.company_key,
                company_name=(source.company_name if source else panel_source.company_key),
                occupation=analysis[0].occupation,
                specialty=analysis[0].specialty,
                specialty_state=analysis[0].specialty_state,
                skill_ids=frozenset(item.skill_id for item in analysis[1]),
            )
        )
    return tuple(result)


def _matches_filters(
    posting: _Posting,
    *,
    query: str | None,
    occupation: str | None,
    specialty: str | None,
    skill: str | None,
) -> bool:
    if query is not None:
        needle = query.casefold()
        if not any(needle in text.casefold() for text in (posting.title, posting.description)):
            return False
    if occupation is not None and posting.occupation != occupation:
        return False
    if specialty is not None and posting.specialty != specialty:
        return False
    return skill is None or skill in posting.skill_ids


def _sample_gate(
    postings: tuple[_Posting, ...], predicate: Callable[[_Posting], bool]
) -> dict[str, object]:
    selected = tuple(posting for posting in postings if predicate(posting))
    companies = Counter(posting.company_key for posting in selected)
    company_count = len(companies)
    largest_share = _ratio(max(companies.values(), default=0), len(selected))
    passed = (
        len(selected) >= GROUP_POSTING_MINIMUM
        and company_count >= GROUP_COMPANY_MINIMUM
        and largest_share is not None
        and largest_share <= GROUP_COMPANY_CONCENTRATION_MAXIMUM
    )
    return {
        "passed": passed,
        "posting_count": len(selected),
        "company_count": company_count,
        "largest_company_share": largest_share,
        "minimum_postings": GROUP_POSTING_MINIMUM,
        "minimum_companies": GROUP_COMPANY_MINIMUM,
        "maximum_company_share": GROUP_COMPANY_CONCENTRATION_MAXIMUM,
    }


def _window_facts(postings: tuple[_Posting, ...] | None) -> dict[str, object]:
    if postings is None:
        return {
            "N_panel": None,
            "unknown_occupation_count": None,
            "unknown_occupation_share": None,
            "N_specialty": None,
            "unresolved_specialty_count": None,
            "unresolved_specialty_share": None,
            "description_count": None,
            "missing_description_count": None,
            "missing_description_share": None,
        }
    n_panel = len(postings)
    technical = tuple(
        posting for posting in postings if posting.occupation == "technical_engineering"
    )
    unknown_count = sum(posting.occupation == "unknown" for posting in postings)
    unresolved_count = sum(posting.specialty_state != "resolved" for posting in technical)
    missing_description = sum(not posting.description.strip() for posting in postings)
    return {
        "N_panel": n_panel,
        "unknown_occupation_count": unknown_count,
        "unknown_occupation_share": _ratio(unknown_count, n_panel),
        "N_specialty": len(technical),
        "unresolved_specialty_count": unresolved_count,
        "unresolved_specialty_share": _ratio(unresolved_count, len(technical)),
        "description_count": n_panel - missing_description,
        "missing_description_count": missing_description,
        "missing_description_share": _ratio(missing_description, n_panel),
    }


def _metric(
    *,
    metric_id: str,
    label: str,
    level: str,
    kind: str,
    denominator_id: str,
    numerator: PostingPair | None,
    denominator: PostingPair | None,
    previous_facts: dict[str, object],
    current_facts: dict[str, object],
    common_gates: tuple[dict[str, object], ...],
    extra_gates: tuple[dict[str, object], ...],
    selected: bool,
) -> dict[str, object]:
    if numerator is None or denominator is None:
        previous_count = current_count = previous_denominator = current_denominator = None
        previous_share = current_share = None
    else:
        previous_count = len(numerator[0])
        current_count = len(numerator[1])
        previous_denominator = len(denominator[0])
        current_denominator = len(denominator[1])
        previous_share = _ratio(previous_count, previous_denominator)
        current_share = _ratio(current_count, current_denominator)
    zero_denominator = (
        previous_denominator == 0 or current_denominator == 0
        if previous_denominator is not None and current_denominator is not None
        else True
    )
    denominator_gate = {
        "id": "positive_denominator",
        "passed": not zero_denominator,
        "previous": previous_denominator,
        "current": current_denominator,
    }
    all_gates = (*common_gates, *extra_gates, denominator_gate)
    passed = all(bool(gate["passed"]) for gate in all_gates)
    delta = (
        round((current_share - previous_share) * 100, 9)
        if passed and previous_share is not None and current_share is not None
        else None
    )
    direction = "uncertain"
    if delta is not None:
        if delta > STABLE_BAND_PP:
            direction = "up"
        elif delta < -STABLE_BAND_PP:
            direction = "down"
        else:
            direction = "stable"
    failed = next((str(gate["id"]) for gate in all_gates if not gate["passed"]), None)
    quality_gates = [dict(gate) for gate in all_gates]
    return {
        "id": metric_id,
        "label": label,
        "level": level,
        "kind": kind,
        "denominator": denominator_id,
        "selected": selected,
        "status": "provisional" if passed else "insufficient",
        "reason": failed,
        "direction": direction,
        "previous_count": previous_count,
        "current_count": current_count,
        "previous_denominator": previous_denominator,
        "current_denominator": current_denominator,
        "previous_share": previous_share,
        "current_share": current_share,
        "delta_pp": delta,
        "quality": {"passed": passed, "gates": quality_gates},
        "window_quality": {"previous": previous_facts, "current": current_facts},
    }


def _metric_from_counts(
    *,
    metric_id: str,
    label: str,
    level: str,
    kind: str,
    denominator_id: str,
    previous_rows: tuple[_Posting, ...] | None,
    current_rows: tuple[_Posting, ...] | None,
    previous_denominator_rows: tuple[_Posting, ...] | None,
    current_denominator_rows: tuple[_Posting, ...] | None,
    predicate: Callable[[_Posting], bool],
    previous_facts: dict[str, object],
    current_facts: dict[str, object],
    occupation_facts_before: dict[str, object] | None = None,
    occupation_facts_after: dict[str, object] | None = None,
    specialty_facts_before: dict[str, object] | None = None,
    specialty_facts_after: dict[str, object] | None = None,
    common_gates: tuple[dict[str, object], ...],
    extra_gate_specs: tuple[str, ...],
    selected: bool,
) -> dict[str, object]:
    if previous_rows is None or current_rows is None:
        numerator = None
        denominator = None
    else:
        numerator = (
            tuple(posting for posting in previous_rows if predicate(posting)),
            tuple(posting for posting in current_rows if predicate(posting)),
        )
        denominator = (
            previous_denominator_rows or (),
            current_denominator_rows or (),
        )
    extra_gates: list[dict[str, object]] = []
    for gate_id in extra_gate_specs:
        if gate_id == "occupation_unknown_threshold":
            previous_value = (occupation_facts_before or previous_facts)["unknown_occupation_share"]
            current_value = (occupation_facts_after or current_facts)["unknown_occupation_share"]
            extra_gates.extend(
                [
                    {
                        "id": gate_id,
                        "window": "previous",
                        "passed": isinstance(previous_value, float)
                        and previous_value <= OCCUPATION_UNKNOWN_MAXIMUM,
                        "observed": previous_value,
                        "threshold": OCCUPATION_UNKNOWN_MAXIMUM,
                    },
                    {
                        "id": gate_id,
                        "window": "current",
                        "passed": isinstance(current_value, float)
                        and current_value <= OCCUPATION_UNKNOWN_MAXIMUM,
                        "observed": current_value,
                        "threshold": OCCUPATION_UNKNOWN_MAXIMUM,
                    },
                ]
            )
        elif gate_id == "specialty_unresolved_threshold":
            previous_value = (specialty_facts_before or previous_facts)[
                "unresolved_specialty_share"
            ]
            current_value = (specialty_facts_after or current_facts)["unresolved_specialty_share"]
            extra_gates.extend(
                [
                    {
                        "id": gate_id,
                        "window": "previous",
                        "passed": isinstance(previous_value, float)
                        and previous_value <= SPECIALTY_UNRESOLVED_MAXIMUM,
                        "observed": previous_value,
                        "threshold": SPECIALTY_UNRESOLVED_MAXIMUM,
                    },
                    {
                        "id": gate_id,
                        "window": "current",
                        "passed": isinstance(current_value, float)
                        and current_value <= SPECIALTY_UNRESOLVED_MAXIMUM,
                        "observed": current_value,
                        "threshold": SPECIALTY_UNRESOLVED_MAXIMUM,
                    },
                ]
            )
        elif gate_id == "description_coverage_threshold":
            previous_value = previous_facts["missing_description_share"]
            current_value = current_facts["missing_description_share"]
            extra_gates.extend(
                [
                    {
                        "id": gate_id,
                        "window": "previous",
                        "passed": isinstance(previous_value, float)
                        and previous_value <= DESCRIPTION_MISSING_MAXIMUM,
                        "observed": previous_value,
                        "threshold": DESCRIPTION_MISSING_MAXIMUM,
                    },
                    {
                        "id": gate_id,
                        "window": "current",
                        "passed": isinstance(current_value, float)
                        and current_value <= DESCRIPTION_MISSING_MAXIMUM,
                        "observed": current_value,
                        "threshold": DESCRIPTION_MISSING_MAXIMUM,
                    },
                ]
            )
        elif gate_id == "sample_threshold":
            previous_sample = (
                _sample_gate(previous_rows, predicate) if previous_rows is not None else None
            )
            current_sample = (
                _sample_gate(current_rows, predicate) if current_rows is not None else None
            )
            extra_gates.extend(
                [
                    {
                        "id": gate_id,
                        "window": "previous",
                        "passed": previous_sample is not None and bool(previous_sample["passed"]),
                        "observed": previous_sample,
                    },
                    {
                        "id": gate_id,
                        "window": "current",
                        "passed": current_sample is not None and bool(current_sample["passed"]),
                        "observed": current_sample,
                    },
                ]
            )
    return _metric(
        metric_id=metric_id,
        label=label,
        level=level,
        kind=kind,
        denominator_id=denominator_id,
        numerator=numerator,
        denominator=denominator,
        previous_facts=previous_facts,
        current_facts=current_facts,
        common_gates=common_gates,
        extra_gates=tuple(extra_gates),
        selected=selected,
    )


def _metric_predicate(level: str, value: str) -> Callable[[_Posting], bool]:
    def matches(posting: _Posting) -> bool:
        if level == "occupation":
            return posting.occupation == value
        if level == "specialty":
            return posting.specialty == value
        return value in posting.skill_ids

    return matches


def compare_mvp_history(
    *,
    panel: RadarPanelInput | None,
    scans: tuple[RadarScanInput, ...],
    input_reason: str | None,
    cutoff: datetime,
    period_days: int,
    selected_source_keys: tuple[str, ...],
    sources: tuple[RadarMvpSource, ...],
    q: str | None,
    selected_occupation: str | None,
    selected_specialty: str | None,
    selected_skill: str | None,
    occupations: tuple[tuple[str, str], ...],
    specialties: tuple[tuple[str, str], ...],
    skills: tuple[tuple[str, str], ...],
    analyze: AnalyzePosting,
) -> dict[str, object]:
    """Compare paired immutable windows and return per-metric gates and deltas."""
    source_map = {source.key: source for source in sources}
    if panel is None:
        common_gates: tuple[dict[str, object], ...] = (
            {"id": "history_present", "passed": False, "observed": 0, "expected": None},
        )
        selected_meta = _selected_metric_meta(
            selected_occupation,
            selected_specialty,
            selected_skill,
            occupations,
            specialties,
            skills,
        )
        return _empty_history(
            period_days=period_days,
            reason=input_reason or "missing_history",
            selected_meta=selected_meta,
            common_gates=common_gates,
            occupations=occupations,
            specialties=specialties,
            skills=skills,
        )

    pairs, expected_count, selection = _select_pairs(
        panel=panel,
        scans=scans,
        cutoff=cutoff,
        period_days=period_days,
        selected_source_keys=selected_source_keys,
    )
    common_gate_list, by_source = _common_gates(
        matched_pairs=pairs,
        expected_pair_count=expected_count,
        selected_source_keys=selected_source_keys,
    )
    common_gates = tuple(common_gate_list)
    panel_sources = {source.source_key: source for source in panel.sources}
    previous_raw = _postings_for_window(
        pairs=pairs,
        window_name="previous",
        panel_sources=panel_sources,
        sources=source_map,
        analyze=analyze,
    )
    current_raw = _postings_for_window(
        pairs=pairs,
        window_name="current",
        panel_sources=panel_sources,
        sources=source_map,
        analyze=analyze,
    )
    previous_all = _filter_rows(previous_raw, q=q)
    current_all = _filter_rows(current_raw, q=q)
    # A selected skill conditions role shares; role and specialty filters
    # condition skill shares. Each selected metric omits its own ID filter.
    previous_role = _filter_rows(previous_all, skill=selected_skill)
    current_role = _filter_rows(current_all, skill=selected_skill)
    previous_skills = _filter_rows(
        previous_all, occupation=selected_occupation, specialty=selected_specialty
    )
    current_skills = _filter_rows(
        current_all, occupation=selected_occupation, specialty=selected_specialty
    )
    previous_facts = _window_facts(previous_role)
    current_facts = _window_facts(current_role)
    previous_skill_facts = _window_facts(previous_skills)
    current_skill_facts = _window_facts(current_skills)
    previous_all_technical = (
        tuple(row for row in previous_all if row.occupation == "technical_engineering")
        if previous_all is not None
        else None
    )
    current_all_technical = (
        tuple(row for row in current_all if row.occupation == "technical_engineering")
        if current_all is not None
        else None
    )
    previous_all_facts = _window_facts(previous_all)
    current_all_facts = _window_facts(current_all)
    previous_all_specialty_facts = _window_facts(previous_all_technical)
    current_all_specialty_facts = _window_facts(current_all_technical)
    occupation_unknown_gates: tuple[str, ...] = ("occupation_unknown_threshold",)
    common_included = common_gates
    metrics: list[dict[str, object]] = []

    def make_metric(
        *,
        metric_id: str,
        label: str,
        level: str,
        denominator_id: str,
        rows_before: tuple[_Posting, ...] | None,
        rows_after: tuple[_Posting, ...] | None,
        denominator_before: tuple[_Posting, ...] | None,
        denominator_after: tuple[_Posting, ...] | None,
        predicate: Callable[[_Posting], bool],
        facts_before: dict[str, object],
        facts_after: dict[str, object],
        occupation_facts_before: dict[str, object] | None = None,
        occupation_facts_after: dict[str, object] | None = None,
        specialty_facts_before: dict[str, object] | None = None,
        specialty_facts_after: dict[str, object] | None = None,
        gate_specs: tuple[str, ...],
        selected: bool,
    ) -> dict[str, object]:
        metric = _metric_from_counts(
            metric_id=metric_id,
            label=label,
            level=level,
            kind=level,
            denominator_id=denominator_id,
            previous_rows=rows_before,
            current_rows=rows_after,
            previous_denominator_rows=denominator_before,
            current_denominator_rows=denominator_after,
            predicate=predicate,
            previous_facts=facts_before,
            current_facts=facts_after,
            occupation_facts_before=occupation_facts_before,
            occupation_facts_after=occupation_facts_after,
            specialty_facts_before=specialty_facts_before,
            specialty_facts_after=specialty_facts_after,
            common_gates=common_included,
            extra_gate_specs=gate_specs,
            selected=selected,
        )
        return {**metric, "period_days": period_days}

    for key, label in occupations:
        metrics.append(
            make_metric(
                metric_id=key,
                label=label,
                level="occupation",
                denominator_id="N_panel",
                rows_before=previous_role,
                rows_after=current_role,
                denominator_before=previous_role,
                denominator_after=current_role,
                predicate=_metric_predicate("occupation", key),
                facts_before=previous_facts,
                facts_after=current_facts,
                gate_specs=(*occupation_unknown_gates, "sample_threshold"),
                selected=selected_occupation == key,
            )
        )

    for key, label in specialties:
        previous_specialty = (
            tuple(
                row
                for row in previous_role
                if row.occupation == "technical_engineering"
                and (selected_occupation is None or row.occupation == selected_occupation)
            )
            if previous_role is not None
            else None
        )
        current_specialty = (
            tuple(
                row
                for row in current_role
                if row.occupation == "technical_engineering"
                and (selected_occupation is None or row.occupation == selected_occupation)
            )
            if current_role is not None
            else None
        )
        specialty_facts_previous = _window_facts(previous_specialty)
        specialty_facts_current = _window_facts(current_specialty)
        metrics.append(
            make_metric(
                metric_id=key,
                label=label,
                level="specialty",
                denominator_id="N_specialty",
                rows_before=previous_specialty,
                rows_after=current_specialty,
                denominator_before=previous_specialty,
                denominator_after=current_specialty,
                predicate=_metric_predicate("specialty", key),
                facts_before=specialty_facts_previous,
                facts_after=specialty_facts_current,
                occupation_facts_before=previous_facts,
                occupation_facts_after=current_facts,
                specialty_facts_before=specialty_facts_previous,
                specialty_facts_after=specialty_facts_current,
                gate_specs=(
                    "occupation_unknown_threshold",
                    "specialty_unresolved_threshold",
                    "sample_threshold",
                ),
                selected=selected_specialty == key,
            )
        )

    for key, label in skills:
        metrics.append(
            make_metric(
                metric_id=key,
                label=label,
                level="skill",
                denominator_id="N_panel",
                rows_before=previous_skills,
                rows_after=current_skills,
                denominator_before=previous_skills,
                denominator_after=current_skills,
                predicate=_metric_predicate("skill", key),
                facts_before=previous_skill_facts,
                facts_after=current_skill_facts,
                occupation_facts_before=(
                    previous_all_facts
                    if selected_occupation is not None or selected_specialty is not None
                    else None
                ),
                occupation_facts_after=(
                    current_all_facts
                    if selected_occupation is not None or selected_specialty is not None
                    else None
                ),
                specialty_facts_before=(
                    previous_all_specialty_facts if selected_specialty is not None else None
                ),
                specialty_facts_after=(
                    current_all_specialty_facts if selected_specialty is not None else None
                ),
                gate_specs=(
                    ("occupation_unknown_threshold",)
                    if selected_occupation is not None or selected_specialty is not None
                    else ()
                )
                + (("specialty_unresolved_threshold",) if selected_specialty is not None else ())
                + ("description_coverage_threshold", "sample_threshold"),
                selected=selected_skill == key,
            )
        )

    def union_predicate(posting: _Posting) -> bool:
        return posting.occupation in {
            "technical_engineering",
            "engineering_management",
            "solutions_architecture",
        }

    union = make_metric(
        metric_id="technical_occupation_share",
        label="Technical occupation share",
        level="technical_union",
        denominator_id="N_panel",
        rows_before=previous_role,
        rows_after=current_role,
        denominator_before=previous_role,
        denominator_after=current_role,
        predicate=union_predicate,
        facts_before=previous_facts,
        facts_after=current_facts,
        gate_specs=(*occupation_unknown_gates, "sample_threshold"),
        selected=not any((selected_occupation, selected_specialty, selected_skill)),
    )
    metrics.append(union)
    selected_meta = _selected_metric_meta(
        selected_occupation,
        selected_specialty,
        selected_skill,
        occupations,
        specialties,
        skills,
    )
    selected_metric = next(
        (
            metric
            for metric in metrics
            if metric["level"] == selected_meta["kind"] and metric["id"] == selected_meta["id"]
        ),
        union,
    )
    if not pairs:
        selected_metric = {**selected_metric, "reason": "missing_history"}
        metrics = [
            {**metric, "reason": "missing_history"}
            if metric["reason"] == "history_present"
            else metric
            for metric in metrics
        ]
    window_rows = {
        "previous": previous_role,
        "current": current_role,
    }
    window_summaries: dict[str, object] = {}
    for name, rows in window_rows.items():
        window_summaries[name] = {
            **selection["windows"][name],
            **_window_facts(rows),
            "selected_slots": sum(
                1 for pair in pairs if pair.previous is not None and pair.current is not None
            ),
        }
    matched_mapping = [
        {
            "source_key": pair.source_key,
            "ordinal": pair.ordinal,
            "previous_run_id": str(pair.previous.run_id),
            "current_run_id": str(pair.current.run_id),
        }
        for pair in pairs
    ]
    history_input_digest = content_hash(
        {
            "metric_contract": MVP_HISTORY_CONTRACT_VERSION,
            "algorithm": MVP_HISTORY_ALGORITHM_VERSION,
            "panel_version": panel.panel_version,
            "panel_digest": panel.panel_digest,
            "selection": selection,
            "matched_pairs": matched_mapping,
            "scans": [
                {
                    "run_id": str(scan.run_id),
                    "source_key": scan.source_key,
                    "source_kind": scan.source_kind,
                    "finished_at": scan.finished_at.astimezone(UTC).isoformat(),
                    "observations": [
                        {
                            "external_id": observation.external_id,
                            "observed_at": observation.observed_at.astimezone(UTC).isoformat(),
                            "title": observation.title,
                            "description": observation.description,
                        }
                        for observation in scan.observations
                    ],
                }
                for pair in pairs
                for scan in (pair.previous, pair.current)
            ],
        }
    )
    result = dict(selected_metric)
    reason = result["reason"]
    return {
        "period_days": period_days,
        "metric_contract_version": MVP_HISTORY_CONTRACT_VERSION,
        "algorithm_version": MVP_HISTORY_ALGORITHM_VERSION,
        "status": result["status"],
        "reason": reason,
        "direction": result["direction"],
        "previous_count": result["previous_count"],
        "current_count": result["current_count"],
        "delta_pp": result["delta_pp"],
        "selected_metric": selected_meta,
        "metrics": metrics,
        "windows": window_summaries,
        "quality": {
            "common_gates": common_gate_list,
            "board_coverage": by_source,
            "overall_coverage": _ratio(len(pairs), expected_count),
            "matched_slots": len(pairs),
            "expected_slots": expected_count,
            "matched_pair_mapping_digest": content_hash(matched_mapping),
        },
        "provenance": {
            "panel_version": panel.panel_version,
            "panel_digest": panel.panel_digest,
            "input_digest": history_input_digest,
        },
    }


def _filter_rows(
    rows: tuple[_Posting, ...] | None,
    *,
    q: str | None = None,
    occupation: str | None = None,
    specialty: str | None = None,
    skill: str | None = None,
) -> tuple[_Posting, ...] | None:
    if rows is None:
        return None
    return tuple(
        posting
        for posting in rows
        if _matches_filters(
            posting,
            query=q,
            occupation=occupation,
            specialty=specialty,
            skill=skill,
        )
    )


def _selected_metric_meta(
    occupation: str | None,
    specialty: str | None,
    skill: str | None,
    occupations: tuple[tuple[str, str], ...],
    specialties: tuple[tuple[str, str], ...],
    skills: tuple[tuple[str, str], ...],
) -> dict[str, object]:
    if skill is not None:
        label = dict(skills)[skill]
        return {
            "kind": "skill",
            "id": skill,
            "label": label,
            "denominator": "N_panel: selected postings after source and role filters",
        }
    if specialty is not None:
        label = dict(specialties)[specialty]
        return {
            "kind": "specialty",
            "id": specialty,
            "label": label,
            "denominator": "N_specialty: technical_engineering postings",
        }
    if occupation is not None:
        return {
            "kind": "occupation",
            "id": occupation,
            "label": dict(occupations)[occupation],
            "denominator": "N_panel: selected postings after source and text filters",
        }
    return {
        "kind": "technical_union",
        "id": "technical_occupation_share",
        "label": "Technical occupation share",
        "denominator": "N_panel: selected postings after source and text filters",
    }


def _empty_history(
    *,
    period_days: int,
    reason: str,
    selected_meta: dict[str, object],
    common_gates: tuple[dict[str, object], ...],
    occupations: tuple[tuple[str, str], ...],
    specialties: tuple[tuple[str, str], ...],
    skills: tuple[tuple[str, str], ...],
) -> dict[str, object]:
    metric_specs = [(key, label, "occupation", "N_panel") for key, label in occupations] + [
        (key, label, "specialty", "N_specialty") for key, label in specialties
    ]
    metric_specs += [(key, label, "skill", "N_panel") for key, label in skills]
    metric_specs.append(
        ("technical_occupation_share", "Technical occupation share", "technical_union", "N_panel")
    )
    metrics: list[dict[str, object]] = []
    for metric_id, label, level, denominator in metric_specs:
        metrics.append(
            {
                "id": metric_id,
                "label": label,
                "level": level,
                "kind": level,
                "denominator": denominator,
                "selected": (level == selected_meta["kind"] and metric_id == selected_meta["id"]),
                "period_days": period_days,
                "status": "insufficient",
                "reason": reason,
                "direction": "uncertain",
                "previous_count": None,
                "current_count": None,
                "previous_denominator": None,
                "current_denominator": None,
                "previous_share": None,
                "current_share": None,
                "delta_pp": None,
                "quality": {
                    "passed": False,
                    "gates": [*common_gates, {"id": reason, "passed": False}],
                },
                "window_quality": {"previous": None, "current": None},
            }
        )
    return {
        "period_days": period_days,
        "metric_contract_version": MVP_HISTORY_CONTRACT_VERSION,
        "algorithm_version": MVP_HISTORY_ALGORITHM_VERSION,
        "status": "insufficient",
        "reason": reason,
        "direction": "uncertain",
        "previous_count": None,
        "current_count": None,
        "delta_pp": None,
        "selected_metric": selected_meta,
        "metrics": metrics,
        "windows": {
            "previous": {"N_panel": None, "N_specialty": None},
            "current": {"N_panel": None, "N_specialty": None},
        },
        "quality": {
            "common_gates": [*common_gates, {"id": reason, "passed": False}],
            "board_coverage": {},
            "overall_coverage": None,
            "matched_slots": None,
            "expected_slots": None,
        },
        "provenance": {"panel_version": None, "panel_digest": None, "input_digest": None},
    }
