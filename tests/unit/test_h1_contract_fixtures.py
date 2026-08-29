"""Executable acceptance checks for the transport-neutral H1 result contract."""

from __future__ import annotations

import json
from copy import deepcopy
from math import isclose
from pathlib import Path
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, FormatChecker

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = PROJECT_ROOT / "tests/fixtures/contracts"
CONTRACT_VERSION = "h1.hybrid-result.v1"
D9_GATE_IDS = (
    "reviewed_eligible_jobs",
    "represented_companies",
    "max_company_concentration",
    "demonstrated_eligibility_false_positives",
    "skill_semantics",
    "compensation_reconciled",
)


@pytest.mark.parametrize(
    ("fixture_name", "state"),
    (
        ("h1_accepted_result.json", "accepted"),
        ("h1_evidence_limited_result.json", "evidence_limited"),
    ),
)
def test_result_fixture_reconciles_opportunities_observations_and_quality(
    fixture_name: str, state: str
) -> None:
    result = _load_json(fixture_name)
    opportunities = result["opportunities"]
    sample = result["observations"]["sample"]
    quality = result["quality"]
    job_ids = tuple(opportunity["job_id"] for opportunity in opportunities["items"])

    assert result["contract_version"] == CONTRACT_VERSION
    assert result["scope"] == {
        "kind": "configured_source_panel",
        "name": result["filters"]["name"],
        "target_universe": None,
    }
    assert quality["state"] == state
    assert quality["claim_policy"]["opportunities"] == "available"
    assert quality["claim_policy"]["broad_market_claims"] == "disabled"
    assert quality["claim_policy"]["skill_semantics"] == "mentions_only"
    assert quality["claim_policy"]["fit_probability"] == "not_provided"
    assert tuple(gate["gate_id"] for gate in quality["gates"]) == D9_GATE_IDS
    assert tuple(quality["evaluation_provenance"]) == D9_GATE_IDS

    assert job_ids == tuple(sorted(job_ids))
    assert len(job_ids) == len(set(job_ids))
    assert all(_is_uuid(job_id) for job_id in job_ids)
    assert opportunities["job_ids"] == list(job_ids)
    assert opportunities["count"] == len(job_ids)
    assert opportunities["company_count"] == len(
        {item["company_name"] for item in opportunities["items"]}
    )
    assert sample["job_ids"] == list(job_ids)
    assert sample["job_count"] == len(job_ids)
    assert sample["company_names"] == sorted(
        {item["company_name"] for item in opportunities["items"]}
    )
    assert sample["company_count"] == len(sample["company_names"])
    assert quality["observed_values"]["matching_job_count"] == sample["job_count"]
    assert quality["observed_values"]["company_count"] == sample["company_count"]
    reviewed_gate = next(
        gate for gate in quality["gates"] if gate["gate_id"] == "reviewed_eligible_jobs"
    )
    assert quality["observed_values"]["reviewed_eligible_job_count"] == reviewed_gate["observed"]
    assert quality["observed_values"]["represented_company_count"] == sample["company_count"]
    assert quality["observed_values"]["demonstrated_eligibility_false_positive_count"] == 0
    assert quality["observed_values"]["skill_semantics"] == "mentions_only"
    assert quality["observed_values"]["compensation_reconciled"] is True
    assert quality["observed_values"]["collection_cutoff"] == sample["collection_cutoff"]

    _assert_freshness_and_sources(opportunities["items"])
    _assert_source_company_identity(opportunities["items"])
    expected_cutoff = max(item["freshness"]["last_seen_at"] for item in opportunities["items"])
    assert sample["collection_cutoff"] == expected_cutoff
    assert quality["observed_values"]["collection_cutoff"] == expected_cutoff
    _assert_per_job_evidence_reconciliation(opportunities["items"], result["observations"])
    _assert_source_coverage(sample["source_coverage"], set(job_ids))
    _assert_buckets(result["observations"], set(job_ids))
    _assert_skills(result["observations"]["skills"], set(job_ids), sample)
    _assert_compensation(
        result["observations"]["compensation"],
        set(job_ids),
        set(sample["compensation_job_ids"]),
    )
    _assert_quality_gates(quality, sample, set(job_ids))
    _assert_manual_review_provenance(fixture_name, quality)


def test_evidence_limited_fixture_keeps_all_opportunities_and_qualifies_observations() -> None:
    result = _load_json("h1_evidence_limited_result.json")

    assert result["quality"]["state"] == "evidence_limited"
    assert result["quality"]["failed_gates"] == [
        "reviewed_eligible_jobs",
        "represented_companies",
        "max_company_concentration",
    ]
    assert result["quality"]["claim_policy"]["panel_observations"] == "qualified"
    assert result["quality"]["claim_policy"]["broad_market_claims"] == "disabled"
    assert result["opportunities"]["count"] == result["observations"]["sample"]["job_count"] == 3
    assert result["observations"]["sample"]["max_company_concentration"]["job_count"] == 2
    assert (
        result["observations"]["skills"][0]["job_share"]
        > result["observations"]["skills"][0]["company_share"]
    )


def test_accepted_fixture_has_no_failed_quality_gates_without_unlocking_broad_claims() -> None:
    result = _load_json("h1_accepted_result.json")

    assert result["quality"]["state"] == "accepted"
    assert result["quality"]["failed_gates"] == []
    assert result["quality"]["claim_policy"]["panel_observations"] == "panel_bounded"
    assert result["quality"]["claim_policy"]["broad_market_claims"] == "disabled"


def test_zero_sample_is_evidence_limited_and_marks_gates_not_evaluable() -> None:
    result = deepcopy(_load_json("h1_evidence_limited_result.json"))
    opportunities = result["opportunities"]
    sample = result["observations"]["sample"]
    quality = result["quality"]
    opportunities.update({"job_ids": [], "count": 0, "company_count": 0, "items": []})
    sample.update(
        {
            "job_ids": [],
            "job_count": 0,
            "company_names": [],
            "company_count": 0,
            "max_company_concentration": None,
            "collection_cutoff": None,
            "skill_denominator_job_ids": [],
            "compensation_job_ids": [],
        }
    )
    sample["source_coverage"].update(
        {"matching_source_count": 0, "matching_source_keys": [], "source_share": 0.0}
    )
    sample["source_coverage"]["job_ids_by_source"] = [
        {"source_key": source_key, "job_ids": []}
        for source_key in sample["source_coverage"]["configured_source_keys"]
    ]
    result["observations"].update(
        {
            "companies": [],
            "skills": [],
            "seniorities": [],
            "workplace_types": [],
            "countries": [],
            "compensation": [],
        }
    )
    quality["claim_policy"]["panel_observations"] = "qualified"
    quality["failed_gates"] = [
        "reviewed_eligible_jobs",
        "represented_companies",
        "max_company_concentration",
        "demonstrated_eligibility_false_positives",
    ]
    for gate in quality["gates"]:
        gate["evidence_job_ids"] = []
        gate["evidence_company_names"] = []
        if gate["gate_id"] in quality["failed_gates"]:
            gate["status"] = "not_evaluable"
            gate["observed"] = None if gate["gate_id"] == "max_company_concentration" else 0
    quality["observed_values"].update(
        {
            "matching_job_count": 0,
            "company_count": 0,
            "reviewed_eligible_job_count": 0,
            "represented_company_count": 0,
            "demonstrated_eligibility_false_positive_count": 0,
            "max_company_concentration": None,
            "source_coverage": 0.0,
            "compensation_coverage": None,
            "collection_cutoff": None,
        }
    )
    for evaluation in quality["evaluation_provenance"].values():
        evaluation["provenance"]["reviewed_job_ids"] = []

    schema = json.loads(
        (PROJECT_ROOT / "docs/h1-hybrid-result-contract.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = list(validator.iter_errors(result))
    assert errors == [], [(list(error.path), error.message) for error in errors]
    assert quality["state"] == "evidence_limited"
    assert sample["max_company_concentration"] is None
    assert all(gate["status"] == "not_evaluable" for gate in quality["gates"][:4])
    assert opportunities["items"] == []
    assert quality["evaluation_provenance"]["reviewed_eligible_jobs"]["provenance"]["ref"]
    assert (
        quality["evaluation_provenance"]["reviewed_eligible_jobs"]["provenance"]["reviewed_job_ids"]
        == []
    )


def test_zero_configured_source_denominator_is_null_not_zero() -> None:
    coverage = {
        "configured_source_count": 0,
        "configured_source_keys": [],
        "matching_source_count": 0,
        "matching_source_keys": [],
        "source_share": None,
        "job_ids_by_source": [],
    }
    _assert_source_coverage(coverage, set())

    schema = json.loads(
        (PROJECT_ROOT / "docs/h1-hybrid-result-contract.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(
        {
            "$schema": schema["$schema"],
            "$defs": schema["$defs"],
            **schema["$defs"]["source_coverage"],
        },
        format_checker=FormatChecker(),
    )
    errors = list(validator.iter_errors(coverage))
    assert errors == []


def test_markdown_fixture_places_opportunities_before_panel_observations() -> None:
    rendered = (FIXTURE_DIR / "h1_evidence_limited_report.md").read_text(encoding="utf-8")
    opportunity_heading = rendered.index("## Concrete opportunities")
    observation_heading = rendered.index("## Panel-bounded observations (configured source panel)")

    assert opportunity_heading < observation_heading
    assert "Result state: `evidence_limited`" in rendered
    assert "Opportunities: **available**" in rendered
    assert "broad-market claims: **disabled**" in rendered
    assert "max_company_concentration" in rendered
    assert "Source coverage" in rendered
    assert "First seen" in rendered and "Last seen" in rendered
    assert "Locations / remote scope" in rendered
    assert "Mention evidence" in rendered
    assert "Compensation evidence" in rendered
    assert "Company share" in rendered
    assert "Amounts remain in original currency and interval" in rendered
    assert "unknown` interval" in rendered
    assert "## Demand" not in rendered
    assert "## Ranked gaps" not in rendered
    assert "`sample_available`" not in rendered

    limited = _load_json("h1_evidence_limited_result.json")
    for gate in limited["quality"]["gates"]:
        assert f"| `{gate['gate_id']}` | {gate['status']} |" in rendered
        evaluation = limited["quality"]["evaluation_provenance"][gate["gate_id"]]
        assert evaluation["basis"].replace("_", " ") in rendered
    for gate_id in limited["quality"]["failed_gates"]:
        assert f"`{gate_id}`" in rendered


def test_schema_declares_h1_envelope_and_explicit_h2_boundary() -> None:
    schema = json.loads(
        (PROJECT_ROOT / "docs/h1-hybrid-result-contract.schema.json").read_text(encoding="utf-8")
    )
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["contract_version"] == {"const": CONTRACT_VERSION}
    assert schema["required"] == [
        "contract_version",
        "scope",
        "filters",
        "profile",
        "quality",
        "opportunities",
        "observations",
    ]
    assert schema["$defs"]["scope"]["properties"]["kind"] == {"const": "configured_source_panel"}
    assert schema["$defs"]["scope"]["properties"]["target_universe"] == {"type": "null"}
    assert schema["$defs"]["quality"]["properties"]["evaluation_provenance"]["required"] == list(
        D9_GATE_IDS
    )
    assert [gate_id for gate_id in D9_GATE_IDS] == list(D9_GATE_IDS)
    assert schema["$defs"]["observed_values"]["properties"]["collection_cutoff"] == {
        "anyOf": [{"$ref": "#/$defs/timestamp"}, {"type": "null"}]
    }
    assert schema["$defs"]["sample"]["properties"]["collection_cutoff"] == {
        "anyOf": [{"$ref": "#/$defs/timestamp"}, {"type": "null"}]
    }
    assert schema["$defs"]["sample"]["properties"]["max_company_concentration"]["anyOf"] == [
        {"$ref": "#/$defs/concentration"},
        {"type": "null"},
    ]


@pytest.mark.parametrize(
    "fixture_name",
    ("h1_accepted_result.json", "h1_evidence_limited_result.json"),
)
def test_result_fixtures_validate_against_the_published_json_schema(fixture_name: str) -> None:
    schema = json.loads(
        (PROJECT_ROOT / "docs/h1-hybrid-result-contract.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    errors = sorted(validator.iter_errors(_load_json(fixture_name)), key=lambda error: error.path)
    assert errors == []


def test_schema_format_checker_rejects_invalid_collection_cutoff() -> None:
    schema = json.loads(
        (PROJECT_ROOT / "docs/h1-hybrid-result-contract.schema.json").read_text(encoding="utf-8")
    )
    result = deepcopy(_load_json("h1_accepted_result.json"))
    result["observations"]["sample"]["collection_cutoff"] = "not-a-date"
    result["quality"]["observed_values"]["collection_cutoff"] = "not-a-date"
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = list(validator.iter_errors(result))
    assert any(
        list(error.path) == ["observations", "sample", "collection_cutoff"] for error in errors
    )
    assert any(
        list(error.path) == ["quality", "observed_values", "collection_cutoff"] for error in errors
    )


def test_filters_and_profile_use_strict_current_validated_shapes() -> None:
    schema = json.loads(
        (PROJECT_ROOT / "docs/h1-hybrid-result-contract.schema.json").read_text(encoding="utf-8")
    )
    result = deepcopy(_load_json("h1_evidence_limited_result.json"))
    result["filters"]["unexpected"] = True
    result["profile"]["unexpected"] = True
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    paths = {tuple(error.path) for error in validator.iter_errors(result)}
    assert ("filters",) in paths
    assert ("profile",) in paths


def _load_json(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


def _assert_freshness_and_sources(items: list[dict[str, object]]) -> None:
    for item in items:
        assert item["source"]["key"]
        assert item["source"]["kind"]
        url = item["source"]["url"]
        assert url is None or url.startswith(("http://", "https://"))
        first_seen = item["freshness"]["first_seen_at"]
        last_seen = item["freshness"]["last_seen_at"]
        assert first_seen.endswith("Z") and last_seen.endswith("Z")
        assert first_seen <= last_seen


def _assert_source_company_identity(items: list[dict[str, object]]) -> None:
    source_to_company: dict[str, str] = {}
    for item in items:
        source_key = item["source"]["key"]
        company_name = item["company_name"]
        previous_company = source_to_company.setdefault(source_key, company_name)
        assert previous_company == company_name


def _assert_manual_review_provenance(fixture_name: str, quality: dict[str, object]) -> None:
    artifact_name = fixture_name.replace("_result.json", "_review_artifact.json")
    expected_ref = f"tests/fixtures/contracts/{artifact_name}"
    artifact = _load_json(Path(expected_ref).name)
    manual_evaluations = [
        evaluation
        for evaluation in quality["evaluation_provenance"].values()
        if evaluation["basis"] == "manual_review"
    ]
    assert manual_evaluations
    for evaluation in manual_evaluations:
        assert evaluation["provenance"]["ref"] == expected_ref
        assert evaluation["provenance"]["reviewed_job_ids"] == artifact["reviewed_job_ids"]
    assert artifact["result_fixture"] == fixture_name
    assert artifact["false_positive_job_ids"] == []


def _assert_per_job_evidence_reconciliation(
    items: list[dict[str, object]], observations: dict[str, object]
) -> None:
    skill_jobs: dict[str, set[str]] = {}
    compensation_components: set[str] = set()
    for item in items:
        job_id = item["job_id"]
        locations = item["locations"]
        ordinals = [location["ordinal"] for location in locations]
        assert ordinals == sorted(set(ordinals))
        for mention in item["skill_mentions"]:
            assert _is_uuid(mention["id"])
            skill_jobs.setdefault(mention["skill_id"], set()).add(job_id)
        for component in item["compensation"]:
            component_id = component["component_id"]
            assert _is_uuid(component_id)
            compensation_components.add(component_id)
    for skill in observations["skills"]:
        assert set(skill["job_ids"]) == skill_jobs.get(skill["skill_id"], set())
    observed_component_ids = {
        component_id
        for group in observations["compensation"]
        for component_id in group["component_ids"]
    }
    assert observed_component_ids == compensation_components
    observed_compensation_jobs = {
        job_id for group in observations["compensation"] for job_id in group["job_ids"]
    }
    expected_compensation_jobs = {item["job_id"] for item in items if item["compensation"]}
    assert observed_compensation_jobs == expected_compensation_jobs


def _assert_source_coverage(coverage: dict[str, object], job_ids: set[str]) -> None:
    configured = coverage["configured_source_keys"]
    matching = coverage["matching_source_keys"]
    assert configured == sorted(set(configured))
    assert matching == sorted(set(matching))
    assert set(matching) <= set(configured)
    rows = coverage["job_ids_by_source"]
    assert [row["source_key"] for row in rows] == sorted(row["source_key"] for row in rows)
    assert len({row["source_key"] for row in rows}) == len(rows)
    assert {row["source_key"] for row in rows} <= set(configured)
    mapped_ids = {job_id for row in rows for job_id in row["job_ids"]}
    assert mapped_ids == job_ids
    for row in rows:
        assert row["job_ids"] == sorted(set(row["job_ids"]))
    assert coverage["matching_source_count"] == len(set(matching))
    assert coverage["configured_source_count"] == len(set(configured))
    if configured:
        assert isclose(coverage["source_share"], len(set(matching)) / len(set(configured)))
    else:
        assert matching == []
        assert coverage["source_share"] is None


def _assert_buckets(observations: dict[str, object], job_ids: set[str]) -> None:
    for field in ("companies", "seniorities", "workplace_types", "countries"):
        for bucket in observations[field]:
            numerator = set(bucket["job_ids"])
            denominator = set(bucket["denominator_job_ids"])
            assert bucket["job_ids"] == sorted(set(bucket["job_ids"]))
            assert bucket["denominator_job_ids"] == sorted(set(bucket["denominator_job_ids"]))
            assert numerator <= denominator <= job_ids
            assert bucket["job_count"] == len(numerator)
            assert isclose(bucket["job_share"], len(numerator) / len(denominator))


def _assert_skills(
    skills: list[dict[str, object]], job_ids: set[str], sample: dict[str, object]
) -> None:
    denominator = set(sample["skill_denominator_job_ids"])
    sample_companies = set(sample["company_names"])
    for skill in skills:
        mentioned = set(skill["job_ids"])
        assert skill["semantics"] == "mention"
        assert skill["job_ids"] == sorted(set(skill["job_ids"]))
        assert skill["denominator_job_ids"] == sorted(set(skill["denominator_job_ids"]))
        assert mentioned <= denominator <= job_ids
        assert skill["job_count"] == len(mentioned)
        assert isclose(skill["job_share"], len(mentioned) / len(denominator))
        companies = set(skill["company_names"])
        denominator_companies = set(skill["denominator_company_names"])
        assert skill["company_names"] == sorted(set(skill["company_names"]))
        assert skill["denominator_company_names"] == sorted(set(skill["denominator_company_names"]))
        assert companies <= denominator_companies <= sample_companies
        assert skill["company_count"] == len(companies)
        assert isclose(skill["company_share"], len(companies) / len(denominator_companies))


def _assert_compensation(
    compensation: list[dict[str, object]],
    job_ids: set[str],
    expected_job_ids: set[str],
) -> None:
    grouped_jobs: set[str] = set()
    for group in compensation:
        group_jobs = set(group["job_ids"])
        assert group["component_ids"] == sorted(set(group["component_ids"]))
        assert group["job_ids"] == sorted(set(group["job_ids"]))
        assert group["denominator_job_ids"] == sorted(set(group["denominator_job_ids"]))
        assert group_jobs <= set(group["denominator_job_ids"]) <= job_ids
        assert group["component_count"] == len(group["component_ids"])
        assert group["job_count"] == len(group_jobs)
        assert group["min_value"] is not None or group["max_value"] is not None
        if group["interval"] == "unknown":
            assert len(group_jobs) == 1
        grouped_jobs |= group_jobs
    assert grouped_jobs == expected_job_ids


def _assert_quality_gates(
    quality: dict[str, object], sample: dict[str, object], job_ids: set[str]
) -> None:
    gates = quality["gates"]
    assert tuple(gate["gate_id"] for gate in gates) == D9_GATE_IDS
    assert tuple(quality["evaluation_provenance"]) == D9_GATE_IDS
    failed = [gate["gate_id"] for gate in gates if gate["status"] != "passed"]
    assert quality["failed_gates"] == failed
    assert quality["state"] == ("evidence_limited" if failed else "accepted")
    for gate in gates:
        assert set(gate["evidence_job_ids"]) <= job_ids
        assert gate["evidence_job_ids"] == sorted(set(gate["evidence_job_ids"]))
        assert gate["evidence_company_names"] == sorted(set(gate["evidence_company_names"]))
        evaluation = quality["evaluation_provenance"][gate["gate_id"]]
        provenance = evaluation["provenance"]
        assert evaluation["basis"] in {"computed", "contract_invariant", "manual_review"}
        assert provenance["reviewed_job_ids"] == sorted(set(provenance["reviewed_job_ids"]))
        assert set(provenance["reviewed_job_ids"]) <= job_ids
        if gate["status"] != "not_evaluable" and evaluation["basis"] == "manual_review":
            assert provenance["kind"] == "review_artifact"
            assert provenance["reviewed_job_ids"]
        if gate["gate_id"] == "demonstrated_eligibility_false_positives" and gate["observed"] == 0:
            assert evaluation["basis"] == "manual_review"
            assert provenance["kind"] == "review_artifact"
            assert provenance["reviewed_job_ids"]
    concentration = sample["max_company_concentration"]
    concentration_gate = next(
        gate for gate in gates if gate["gate_id"] == "max_company_concentration"
    )
    assert isclose(concentration_gate["observed"], concentration["job_share"])
