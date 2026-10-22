"""Offline behavior and immutable-input checks for the P4 B prototype."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
import yaml

from career_os.config.radar_panel import RadarPanelSource
from career_os.domain.canonical import content_hash
from career_os.radar.aggregate import (
    RadarObservationInput,
    RadarPanelInput,
    RadarScanInput,
)
from career_os.radar.role_contract import (
    OCCUPATIONS,
    SPECIALTIES,
    ContractInputError,
    RoleAssignment,
    RoleContractBuild,
    _sample_pass,
    adapt_classification,
    adapt_legacy_output,
    aggregate_role_contract_windows,
    build_purposive_snapshot,
    compare_build_versions,
    write_immutable_json,
)
from career_os.radar.roles import (
    RoleFamily,
    classify_role,
)

ROOT = Path(__file__).resolve().parents[2]
FROZEN_PATH = ROOT / "tests/fixtures/radar/role-residual-cause.frozen.json"
PREFLIGHT_PATH = ROOT / "tests/fixtures/radar/role-scope-preflight.corpus.json"
VERIFICATION_PATH = ROOT / "tests/fixtures/radar/role-calibration.verification-corpus.json"
GOLDEN_PATH = ROOT / "tests/fixtures/radar/golden_role_classification.v1.yaml"
FROZEN_SHA256 = "f9790e68d37ba0004506b3204935ddf99f42570523c8c5d0cf0b2f54b577e20b"
LEGACY_ROLES_SHA256 = "3f1ddb0e76dcb243165790003fa0630fb5b737f14d29bb54823fc4bc5874db74"


def _assignment(title: str, description: str | None = None) -> RoleAssignment:
    return adapt_classification(title, classify_role(title, description))


def _panel(count: int, company_keys: tuple[str, ...] | None = None) -> RadarPanelInput:
    companies = company_keys or tuple(f"company-{index}" for index in range(count))
    assert len(companies) == count
    sources = tuple(
        RadarPanelSource(
            source_key=f"source-{index}",
            company_key=companies[index],
            source_kind="greenhouse",
        )
        for index in range(count)
    )
    manifest = {
        "panel_version": "offline-test-v1",
        "sources": [
            {
                "source_key": item.source_key,
                "company_key": item.company_key,
                "source_kind": item.source_kind,
            }
            for item in sources
        ],
    }
    return RadarPanelInput(
        panel_version="offline-test-v1",
        manifest_digest=content_hash(manifest),
        panel_digest=content_hash({"manifest": manifest, "source_configs": "frozen-test"}),
        source_config_digests={item.source_key: "a" * 64 for item in sources},
        canonical_manifest=manifest,
        sources=sources,
    )


def _scan(
    source: RadarPanelSource,
    finished_at: datetime,
    label: str,
    postings: tuple[tuple[str, str], ...],
) -> RadarScanInput:
    return RadarScanInput(
        run_id=uuid5(NAMESPACE_URL, f"https://role-contract.test/{label}"),
        source_key=source.source_key,
        source_kind=source.source_kind,
        finished_at=finished_at,
        observations=tuple(
            RadarObservationInput(
                external_id=external_id,
                observed_at=finished_at,
                title=title,
                description="",
            )
            for external_id, title in postings
        ),
    )


def _two_window_scans(
    panel: RadarPanelInput,
    cutoff: datetime,
    postings_by_source: tuple[tuple[tuple[str, str], ...], ...],
    *,
    changed_title: tuple[int, int, str] | None = None,
) -> tuple[RadarScanInput, ...]:
    scans: list[RadarScanInput] = []
    for source_index, source in enumerate(panel.sources):
        for window, start in (
            ("previous", cutoff - timedelta(days=180)),
            ("current", cutoff - timedelta(days=90)),
        ):
            for ordinal in range(13):
                observed = postings_by_source[source_index]
                if changed_title is not None:
                    changed_source, changed_ordinal, title = changed_title
                    if source_index == changed_source and ordinal == changed_ordinal:
                        changed_id = observed[0][0]
                        observed = tuple(
                            (key, title if key == changed_id else value) for key, value in observed
                        )
                scans.append(
                    _scan(
                        source,
                        start + timedelta(days=ordinal * 7 + 1),
                        f"{window}-{source_index}-{ordinal}",
                        observed,
                    )
                )
    return tuple(scans)


def _replace_scan_title(
    scans: tuple[RadarScanInput, ...], *, label: str, external_id: str, title: str
) -> tuple[RadarScanInput, ...]:
    run_id = uuid5(NAMESPACE_URL, f"https://role-contract.test/{label}")
    found = False
    updated: list[RadarScanInput] = []
    for scan in scans:
        if scan.run_id != run_id:
            updated.append(scan)
            continue
        observations = tuple(
            replace(observation, title=title)
            if observation.external_id == external_id
            else observation
            for observation in scan.observations
        )
        found = len(observations) != len(scan.observations) or any(
            item.external_id == external_id for item in scan.observations
        )
        updated.append(replace(scan, observations=observations))
    assert found
    return tuple(updated)


def _threshold_postings() -> tuple[tuple[tuple[str, str], ...], ...]:
    by_source = []
    for _ in range(5):
        titles = (
            ("Sales Engineer",) * 4
            + ("Backend Engineer",) * 8
            + ("Software Engineer",) * 2
            + ("Product Manager",) * 6
        )
        by_source.append(tuple((f"posting-{index}", title) for index, title in enumerate(titles)))
    return tuple(by_source)


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def test_all_eleven_legacy_family_ids_map_to_the_two_b_axes() -> None:
    expected = {
        "Backend Engineer": ("technical_engineering", "backend_engineering"),
        "Frontend Engineer": ("technical_engineering", "frontend_engineering"),
        "Full Stack Engineer": ("technical_engineering", "fullstack_engineering"),
        "Data Engineer": ("technical_engineering", "data_engineering"),
        "Product Engineer": ("technical_engineering", "product_engineering"),
        "Platform Engineer": ("technical_engineering", "platform_infrastructure_engineering"),
        "Machine Learning Engineer": ("technical_engineering", "machine_learning_engineering"),
        "AI Engineer": ("technical_engineering", "ai_engineering"),
        "AI Product Engineer": ("technical_engineering", "ai_product_engineering"),
        "Solutions Architect": ("solutions_architecture", None),
        "Engineering Manager": ("engineering_management", None),
    }
    seen: set[str] = set()
    for title, (occupation, specialty) in expected.items():
        legacy = classify_role(title)
        assert legacy.role_family is not None, title
        seen.add(legacy.role_family.value)
        result = adapt_classification(title, legacy)
        assert (result.occupation, result.specialty) == (occupation, specialty), title
        assert result.specialty_state == ("resolved" if specialty else "not_applicable"), title
    assert seen == {family.value for family in RoleFamily}
    assert len(OCCUPATIONS) == 5
    assert set(SPECIALTIES) == {
        family.value
        for family in RoleFamily
        if family.value not in {"engineering_management", "solutions_architecture"}
    }


def test_closed_title_rules_preserve_unknowns_and_do_not_promote_context() -> None:
    cases = (
        ("Senior Systems Engineer, Cloudflare Tunnel", "technical_engineering", "missing"),
        ("Software Engineer II, Backend", "technical_engineering", "missing"),
        ("Android Mobile Engineer", "technical_engineering", "unsupported"),
        ("Hardware Engineer", "technical_engineering", "unsupported"),
        ("Principal Engineer, Hardware", "technical_engineering", "missing"),
        ("Technical Program Manager, Bridge", "unknown", "not_assessed"),
        ("Developer Relations Engineer", "unknown", "not_assessed"),
        ("AI Technical Support Specialist | Based in Lithuania", "unknown", "not_assessed"),
        ("Head of Card Product", "unknown", "not_assessed"),
        ("Head of Website Growth", "unknown", "not_assessed"),
        ("Product Manager", "nontechnical", "not_applicable"),
    )
    for title, occupation, specialty_state in cases:
        result = _assignment(title)
        assert result.occupation == occupation, title
        assert result.specialty_state == specialty_state, title
    software = _assignment("Software Engineer, ML Platform")
    assert software.specialty_state == "resolved"
    assert software.specialty == "platform_infrastructure_engineering"
    assert _assignment("Software Engineer at Example").occupation == "unknown"
    assert _assignment("Developer Relations Engineer").occupation == "unknown"
    generic_evidence = _assignment("Software Engineer II").evidence
    assert any(item.rule_id == "b_generic_engineering_head" for item in generic_evidence)
    assert _assignment("Software Engineer I").specialty_state == "missing"
    assert _assignment("Software Engineer IV").specialty_state == "missing"
    for title in (
        "Software Engineer V",
        "Senior Staff Software Engineer",
        "Software Engineer: Platform",
        "Software Engineer II for Platform",
    ):
        assert _assignment(title).occupation == "unknown", title


def test_exact_cited_preflight_and_verification_titles_are_rerun_title_only() -> None:
    preflight = json.loads(PREFLIGHT_PATH.read_text(encoding="utf-8"))
    preflight_row = next(
        item
        for item in preflight["rows"]
        if item["provenance"]["job_id"] == "82f93793-da7d-432d-83c5-17de10cbb159"
    )
    verification = json.loads(VERIFICATION_PATH.read_text(encoding="utf-8"))
    verification_row = next(
        item
        for item in verification["rows"]
        if item["job_id"] == "081c3bd9-1318-474d-96ef-999d5fa937d1"
    )
    assert preflight_row["title"] == "Software Engineer, Product Security Data Platforms"
    assert verification_row["title"] == "Software Engineer II, Backend (Test Infra)"
    for title in (preflight_row["title"], verification_row["title"]):
        legacy = classify_role(title)
        assert legacy.classifier_version == "1.1.0"
        result = adapt_classification(title, legacy)
        assert result.occupation == "technical_engineering"
        assert result.specialty_state == "missing"
        assert any(item.rule_id == "b_generic_engineering_head" for item in result.evidence)


def test_golden_competing_duties_and_title_duty_conflict_keep_all_candidates() -> None:
    fixture = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))
    wanted = {
        "synthetic_two_direct_duties_are_ambiguous",
        "synthetic_title_and_duty_conflict",
        "synthetic_backend_frontend_disjunction",
    }
    cases = {item["id"]: item for item in fixture["cases"] if item["id"] in wanted}
    assert set(cases) == wanted
    ambiguous = cases["synthetic_two_direct_duties_are_ambiguous"]
    result = _assignment(ambiguous["title"], ambiguous["description"])
    assert result.occupation == "technical_engineering"
    assert result.specialty_state == "ambiguous"
    assert set(result.candidate_families) == {"backend_engineering", "frontend_engineering"}
    conflict = cases["synthetic_title_and_duty_conflict"]
    result = _assignment(conflict["title"], conflict["description"])
    assert result.reason == "title_duty_conflict"
    assert set(result.candidate_families) == {"backend_engineering", "machine_learning_engineering"}
    disjunction = cases["synthetic_backend_frontend_disjunction"]
    result = _assignment(disjunction["title"])
    assert set(result.candidate_families) == {"backend_engineering", "frontend_engineering"}


def test_scope_conflict_and_mixed_broad_candidates_remain_unknown() -> None:
    conflict = _assignment("Customer Success Architect")
    assert conflict.occupation == "unknown"
    assert conflict.reason == "scope_conflict"
    assert conflict.specialty_state == "not_assessed"
    mixed = classify_role("Backend Engineer", "Responsibilities:\n- Deliver customer solutions.")
    assignment = adapt_classification("Backend Engineer", mixed)
    assert assignment.occupation == "unknown"
    assert assignment.reason == "unresolved_competing_evidence"
    assert set(assignment.candidate_families) == {"backend_engineering", "solutions_architecture"}


def test_legacy_versions_are_pinned_and_assignment_inputs_are_frozen() -> None:
    legacy = classify_role("Software Engineer")
    payload = {
        "classifier_version": legacy.classifier_version,
        "taxonomy_version": legacy.taxonomy_version,
        "role_family": None,
        "status": legacy.status.value,
        "scope": legacy.scope.value,
        "evidence": [
            {
                "axis": item.axis.value,
                "source": item.source.value,
                "rule_id": item.rule_id,
                "matched_text": item.matched_text,
            }
            for item in legacy.evidence
        ],
    }
    assignment = adapt_legacy_output("Software Engineer", payload)
    payload["evidence"].clear()
    assert assignment.evidence
    payload["classifier_version"] = "1.2.0"
    with pytest.raises(ContractInputError, match=r"1\.1\.0"):
        adapt_legacy_output("Software Engineer", payload)

    mutable_families = ["backend_engineering"]
    mutable_evidence = [assignment.evidence[0]]
    frozen = RoleAssignment(
        "technical_engineering",
        None,
        "ambiguous",
        "competing",
        mutable_families,
        mutable_evidence,
    )
    mutable_families.clear()
    mutable_evidence.clear()
    assert frozen.candidate_families == ("backend_engineering",)
    assert len(frozen.evidence) == 1


def test_frozen127_replay_is_deterministic_diagnostic_and_never_history() -> None:
    frozen_bytes = FROZEN_PATH.read_bytes()
    assert hashlib.sha256(frozen_bytes).hexdigest() == FROZEN_SHA256
    assert hashlib.sha256((ROOT / "src/career_os/radar/roles.py").read_bytes()).hexdigest() == (
        LEGACY_ROLES_SHA256
    )
    frozen = json.loads(frozen_bytes)
    first = build_purposive_snapshot(frozen, source_sha256=FROZEN_SHA256)
    second = build_purposive_snapshot(frozen, source_sha256=FROZEN_SHA256)
    assert first.digest == second.digest
    payload = first.to_json()
    assert payload["purpose"] == "purposive_diagnostic_snapshot"
    assert payload["history_windows"]["reason"] == "missing_history"
    assert payload["history_windows"]["previous"]["counts"] is None
    assert payload["history_windows"]["delta_pp"] is None
    assert payload["diagnostic_counts_only"]["corpus_rows"] == 127
    assert len(payload["rows"]) == 127
    assert all("description" not in row for row in payload["rows"])
    with pytest.raises(TypeError):
        first.payload["purpose"] = "changed"  # type: ignore[index]
    payload["rows"].clear()
    assert len(first.to_json()["rows"]) == 127


def test_purposive_and_versioned_builds_are_deep_immutable_and_no_overwrite(
    tmp_path: Path,
) -> None:
    payload: dict[str, object] = {"versions": {"metric": "2.0.0"}, "rows": [{"id": "x"}]}
    digest = content_hash(payload)
    payload["build_digest"] = digest
    build = RoleContractBuild(payload, digest)
    payload["rows"].clear()
    assert build.to_json()["rows"] == [{"id": "x"}]
    path = tmp_path / "immutable.json"
    assert write_immutable_json(path, build) is True
    assert write_immutable_json(path, build) is False
    altered = {"versions": {"metric": "3.0.0"}, "rows": [{"id": "x"}]}
    altered_digest = content_hash(altered)
    altered["build_digest"] = altered_digest
    altered_build = RoleContractBuild(altered, altered_digest)
    with pytest.raises(ContractInputError, match="refusing to overwrite"):
        write_immutable_json(path, altered_build)
    with pytest.raises(ContractInputError, match="cross-version"):
        compare_build_versions(build, altered_build)


def test_aggregator_parent_oracle_keeps_denominators_and_independent_gates() -> None:
    panel = _panel(5)
    cutoff = datetime(2026, 10, 17, tzinfo=UTC)
    postings = tuple(
        tuple(
            (
                f"posting-{index}",
                "Backend Engineer"
                if index < 4
                else "Software Engineer"
                if index < 7
                else "Engineering Manager"
                if index == 7
                else "Product Manager",
            )
            for index in range(10)
        )
        for _ in panel.sources
    )
    result = aggregate_role_contract_windows(
        panel,
        cutoff,
        _two_window_scans(panel, cutoff, postings),
    ).to_json()
    previous = result["windows"]["previous"]
    current = result["windows"]["current"]
    assert result["matched_pair_count"] == 65
    assert previous["N_panel"] == current["N_panel"] == 50
    assert previous["N_specialty"] == current["N_specialty"] == 35
    assert previous["occupation_counts"]["technical_engineering"] == 35
    assert previous["occupation_shares"]["technical_engineering"] == 0.7
    assert previous["specialty_counts"]["backend_engineering"] == 20
    assert previous["specialty_shares"]["backend_engineering"] == pytest.approx(20 / 35)
    assert previous["specialty_state_counts"]["missing"] == 15
    assert previous["technical_occupation_count"] == 40
    assert previous["technical_occupation_share"] == 0.8
    assert result["comparisons"]["broad_run_status"] == "provisional"
    assert result["comparisons"]["specialty_run_status"] == "insufficient"
    assert result["comparisons"]["broad_occupation"]["technical_engineering"]["delta_pp"] == 0
    assert result["comparisons"]["broad_occupation"]["engineering_management"]["status"] == (
        "insufficient"
    )
    assert (
        result["comparisons"]["specialty_within_technical_engineering"]["backend_engineering"][
            "delta_pp"
        ]
        is None
    )
    assert result["gates"]["specialty_run"][0]["unresolved_share"] == pytest.approx(15 / 35)
    assert len(result["matched_slot_mapping"]) == 65
    assert not _contains_key(result["windows"], "description")


def test_latest_repeat_collapse_keeps_cross_board_postings_distinct() -> None:
    panel = _panel(5)
    cutoff = datetime(2026, 10, 17, tzinfo=UTC)
    postings = tuple((("same-id", "Backend Engineer"),) for _ in panel.sources)
    scans = _two_window_scans(panel, cutoff, postings, changed_title=(0, 12, "Data Engineer"))
    result = aggregate_role_contract_windows(panel, cutoff, scans).to_json()
    assert result["windows"]["previous"]["N_panel"] == 5
    assert result["windows"]["current"]["N_panel"] == 5
    assert result["windows"]["previous"]["occupation_counts"]["technical_engineering"] == 5
    assert result["windows"]["previous"]["specialty_counts"]["data_engineering"] == 1
    assert result["windows"]["current"]["specialty_counts"]["data_engineering"] == 1


def test_unmatched_slot_observation_cannot_change_latest_selected_posting() -> None:
    panel = _panel(5)
    cutoff = datetime(2026, 10, 17, tzinfo=UTC)
    postings = tuple((("same-id", "Backend Engineer"),) for _ in panel.sources)
    scans = _two_window_scans(panel, cutoff, postings)
    unmatched_previous_run = uuid5(NAMESPACE_URL, "https://role-contract.test/previous-0-12")
    scans = tuple(scan for scan in scans if scan.run_id != unmatched_previous_run)
    scans = _replace_scan_title(
        scans,
        label="current-0-12",
        external_id="same-id",
        title="Data Engineer",
    )

    result = aggregate_role_contract_windows(panel, cutoff, scans).to_json()
    assert result["matched_pair_count"] == 64
    assert result["windows"]["current"]["N_panel"] == 5
    assert result["windows"]["current"]["specialty_counts"]["backend_engineering"] == 5
    assert result["windows"]["current"]["specialty_counts"]["data_engineering"] == 0


def test_occupation_and_specialty_gates_have_inclusive_twenty_percent_boundary() -> None:
    panel = _panel(5)
    cutoff = datetime(2026, 10, 17, tzinfo=UTC)
    baseline = _two_window_scans(panel, cutoff, _threshold_postings())
    exact = aggregate_role_contract_windows(panel, cutoff, baseline).to_json()
    assert exact["windows"]["previous"]["N_panel"] == 100
    assert exact["windows"]["previous"]["unknown_occupation_share"] == 0.2
    assert exact["windows"]["previous"]["N_specialty"] == 50
    assert exact["windows"]["previous"]["unresolved_specialty_share"] == 0.2
    assert exact["comparisons"]["broad_run_status"] == "provisional"
    assert exact["comparisons"]["specialty_run_status"] == "provisional"

    unknown_above = _replace_scan_title(
        baseline,
        label="current-0-12",
        external_id="posting-19",
        title="Sales Engineer",
    )
    broad_failed = aggregate_role_contract_windows(panel, cutoff, unknown_above).to_json()
    assert broad_failed["windows"]["current"]["unknown_occupation_share"] == 0.21
    assert broad_failed["comparisons"]["broad_run_status"] == "insufficient"
    assert broad_failed["comparisons"]["specialty_run_status"] == "insufficient"

    specialty_above = _replace_scan_title(
        baseline,
        label="current-0-12",
        external_id="posting-4",
        title="Software Engineer",
    )
    specialty_failed = aggregate_role_contract_windows(panel, cutoff, specialty_above).to_json()
    assert specialty_failed["windows"]["current"]["unknown_occupation_share"] == 0.2
    assert specialty_failed["windows"]["current"]["unresolved_specialty_share"] == 0.22
    assert specialty_failed["comparisons"]["broad_run_status"] == "provisional"
    assert specialty_failed["comparisons"]["specialty_run_status"] == "insufficient"


def test_shared_company_keys_and_category_gates_are_aggregated_independently() -> None:
    company_keys = ("company-a", "company-a", "company-b", "company-c", "company-d", "company-e")
    panel = _panel(6, company_keys)
    cutoff = datetime(2026, 10, 17, tzinfo=UTC)
    specs = (
        (5, 5),
        (5, 5),
        (4, 3),
        (3, 3),
        (3, 2),
        (0, 2),
    )
    postings_by_source = []
    for source_index, (backend_count, manager_count) in enumerate(specs):
        titles = (
            ("Backend Engineer",) * backend_count
            + ("Engineering Manager",) * manager_count
            + ("Product Manager",) * (20 - backend_count - manager_count)
        )
        postings_by_source.append(
            tuple((f"posting-{source_index}-{index}", title) for index, title in enumerate(titles))
        )
    result = aggregate_role_contract_windows(
        panel,
        cutoff,
        _two_window_scans(panel, cutoff, tuple(postings_by_source)),
    ).to_json()
    previous = result["windows"]["previous"]
    comparisons = result["comparisons"]
    assert previous["N_panel"] == 120
    assert previous["N_specialty"] == 20
    assert previous["occupation_groups"]["technical_engineering"]["count"] == 20
    assert previous["specialty_groups"]["backend_engineering"]["company_count"] == 4
    assert previous["specialty_groups"]["backend_engineering"]["largest_company_share"] == 0.5
    assert previous["occupation_groups"]["engineering_management"]["company_count"] == 5
    assert previous["occupation_groups"]["engineering_management"]["largest_company_share"] == 0.5
    assert previous["technical_union_sample"]["company_count"] == 5
    assert previous["technical_union_sample"]["largest_company_share"] == 0.5
    assert comparisons["broad_run_status"] == "provisional"
    assert comparisons["specialty_run_status"] == "provisional"
    assert (
        comparisons["specialty_within_technical_engineering"]["backend_engineering"][
            "sample_gate_passed"
        ]
        is False
    )
    assert comparisons["broad_occupation"]["engineering_management"]["sample_gate_passed"] is False
    assert (
        comparisons["broad_occupation"]["technical_occupation_share"]["sample_gate_passed"] is False
    )


def test_missing_history_zero_denominator_and_gate_threshold_edges() -> None:
    panel = _panel(1)
    cutoff = datetime(2026, 10, 17, tzinfo=UTC)
    missing = aggregate_role_contract_windows(panel, cutoff, ()).to_json()
    assert missing["windows"]["previous"]["N_panel"] is None
    assert missing["windows"]["previous"]["occupation_counts"] is None
    assert missing["windows"]["previous"]["occupation_groups"]["unknown"]["count"] is None
    assert missing["comparisons"]["broad_run_status"] == "insufficient"
    empty_scans = (
        _scan(panel.sources[0], cutoff - timedelta(days=179), "prev-empty", ()),
        _scan(panel.sources[0], cutoff - timedelta(days=89), "current-empty", ()),
    )
    zero = aggregate_role_contract_windows(panel, cutoff, empty_scans).to_json()
    assert zero["windows"]["previous"]["N_panel"] == 0
    assert zero["windows"]["previous"]["occupation_counts"]["unknown"] == 0
    assert zero["windows"]["previous"]["occupation_shares"] is not None
    assert zero["windows"]["previous"]["occupation_shares"]["unknown"] is None
    assert zero["windows"]["previous"]["N_specialty"] == 0
    assert zero["windows"]["previous"]["specialty_shares"]["backend_engineering"] is None
    assert zero["gates"]["specialty_run"][0]["passed"] is False
    assert _sample_pass({"count": 20, "company_count": 5, "largest_company_share": 0.4})
    assert not _sample_pass({"count": 19, "company_count": 5, "largest_company_share": 0.4})
    assert not _sample_pass({"count": 20, "company_count": 4, "largest_company_share": 0.4})
    assert not _sample_pass({"count": 20, "company_count": 5, "largest_company_share": 0.400000001})


def test_eligible_specialty_delta_is_reported_in_percentage_points() -> None:
    panel = _panel(5)
    cutoff = datetime(2026, 10, 17, tzinfo=UTC)
    baseline = _two_window_scans(panel, cutoff, _threshold_postings())
    changed = _replace_scan_title(
        baseline,
        label="current-0-12",
        external_id="posting-4",
        title="Data Engineer",
    )
    result = aggregate_role_contract_windows(panel, cutoff, changed).to_json()
    assert result["comparisons"]["specialty_run_status"] == "provisional"
    backend = result["comparisons"]["specialty_within_technical_engineering"]["backend_engineering"]
    assert backend["sample_gate_passed"] is True
    assert backend["delta_pp"] == -2.0
