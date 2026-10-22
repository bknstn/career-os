"""Compensation supplemental evidence is append-only and never mutates H3/H4 inputs."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from career_os.application.compensation import build_compensation_comparison
from career_os.cli import app
from career_os.config.career_profiles import load_career_profile
from career_os.config.compensation_supplements import (
    CompensationLedger,
    CompensationRecord,
    append_compensation_record,
    load_compensation_ledger,
)
from career_os.config.skills import load_skill_taxonomy
from career_os.config.sources import ConfigurationError
from career_os.reporting.compensation import render_compensation_comparison

PROFILE = Path("config/career-profile.example.yaml")
LEDGER = Path("config/compensation-supplement.example.yaml")
TAXONOMY = Path("config/taxonomy/skills.v1.yaml")


def profile():
    return load_career_profile(PROFILE, taxonomy=load_skill_taxonomy(TAXONOMY))


def ledger() -> CompensationLedger:
    return load_compensation_ledger(LEDGER)


def correction(*, subject_id: str = "sample_opportunity") -> CompensationRecord:
    return CompensationRecord.model_validate(
        {
            "schema_version": "compensation-record.v1",
            "source": {
                "id": "user_salary_correction_2026_09_21",
                "kind": "user_statement",
                "reference": "Synthetic unit-test user statement",
                "locator": "Test-only correction provenance",
                "recorded_on": "2026-09-21",
            },
            "observation": {
                "id": "sample_recruiter_range_2026_09_21",
                "subject": {
                    "kind": "opportunity",
                    "id": subject_id,
                    "label": "Renamed sample opportunity",
                    "reference": "https://example.invalid/sample",
                },
                "amount": {
                    "minimum": 252000,
                    "maximum": 252000,
                    "currency": "PLN",
                    "period": "year",
                    "basis": "gross",
                    "component": "base",
                    "employment_model": "UoP",
                },
                "disclosure_kind": "recruiter_range",
                "source_id": "user_salary_correction_2026_09_21",
                "disclosed_on": "2026-09-21",
                "supersedes": "sample_recruiter_range_2026_09_20",
                "note": "Synthetic test correction; not an offer.",
            },
        }
    )


def test_synthetic_report_compares_only_confirmed_matching_base_salary() -> None:
    comparison = build_compensation_comparison(
        ledger(),
        profile(),
        current_role_id="sample_current",
        opportunity_id="sample_opportunity",
    )

    assert comparison.comparable is True
    assert comparison.current_role_normalized is not None
    assert comparison.opportunity_normalized is not None
    assert comparison.current_role_normalized.minimum_annual == Decimal("120000")
    assert comparison.opportunity_normalized.minimum_monthly == Decimal("15000")
    assert comparison.opportunity_normalized.maximum_monthly == Decimal("20000")
    assert comparison.monthly_delta_minimum == Decimal("5000")
    assert comparison.monthly_delta_maximum == Decimal("10000")
    assert comparison.monthly_change_percent_minimum == Decimal("50")
    assert comparison.floor.status == "meets"
    assert comparison.opportunity.disclosure_kind == "recruiter_range"
    assert comparison.sources[0].kind == "user_statement"
    rendered = render_compensation_comparison(comparison)
    assert "+50.000%–+100.000%" in rendered
    assert "диапазон рекрутера, не оффер" in rendered
    assert "private correspondence is not retained" in rendered


def test_correction_keeps_history_and_uses_explicit_same_subject_supersession() -> None:
    updated = append_compensation_record(ledger(), correction())
    comparison = build_compensation_comparison(
        updated,
        profile(),
        current_role_id="sample_current",
        opportunity_id="sample_opportunity",
    )

    assert len(updated.observations) == 3
    assert comparison.opportunity.id == "sample_recruiter_range_2026_09_21"
    assert comparison.opportunity.subject.label == "Renamed sample opportunity"
    assert comparison.opportunity_normalized is not None
    assert comparison.opportunity_normalized.minimum_monthly == Decimal("21000")
    assert "`sample_recruiter_range_2026_09_20` | заменена" in render_compensation_comparison(
        comparison
    )


@pytest.mark.parametrize(
    "mutator, message",
    [
        (
            lambda data: data["observations"].append(
                {
                    **deepcopy(data["observations"][1]),
                    "id": "competing_sample",
                    "supersedes": None,
                }
            ),
            "competing active.*explicit supersedes",
        ),
        (
            lambda data: data["observations"].append(
                {
                    **deepcopy(data["observations"][1]),
                    "id": "wrong_subject_correction",
                    "subject": {
                        "kind": "opportunity",
                        "id": "other_opportunity",
                        "label": "Other",
                    },
                    "supersedes": "sample_recruiter_range_2026_09_20",
                }
            ),
            "same subject",
        ),
        (
            lambda data: data["observations"].append(
                {
                    **deepcopy(data["observations"][1]),
                    "id": "missing_predecessor",
                    "supersedes": "no_such_observation",
                }
            ),
            "unknown superseded",
        ),
    ],
)
def test_ledger_rejects_unreconciled_or_unrelated_replacements(mutator, message: str) -> None:
    data = ledger().model_dump(mode="json")
    mutator(data)
    with pytest.raises(ValidationError, match=message):
        CompensationLedger.model_validate(data)


def test_unknown_current_salary_does_not_hide_independent_opportunity_floor_result() -> None:
    data = ledger().model_dump(mode="json")
    amount = data["observations"][0]["amount"]
    amount.update(
        minimum=None,
        maximum=None,
        currency=None,
        period="unknown",
        basis="unknown",
        component="unknown",
        employment_model="unknown",
    )
    data["observations"][0]["disclosure_kind"] = "unknown"

    comparison = build_compensation_comparison(
        CompensationLedger.model_validate(data),
        profile(),
        current_role_id="sample_current",
        opportunity_id="sample_opportunity",
    )

    assert comparison.comparable is False
    assert comparison.monthly_delta_minimum is None
    assert comparison.floor.status == "meets"
    assert "Минимум возможности проверяется независимо" in render_compensation_comparison(
        comparison
    )


def test_floor_crossing_range_is_partial_and_contract_mismatch_is_not_comparable() -> None:
    crossing = ledger().model_dump(mode="json")
    crossing["observations"][1]["amount"].update(minimum="120000", maximum="180000")
    partial = build_compensation_comparison(
        CompensationLedger.model_validate(crossing),
        profile(),
        current_role_id="sample_current",
        opportunity_id="sample_opportunity",
    )
    assert partial.floor.status == "partial"

    mismatch = ledger().model_dump(mode="json")
    mismatch["observations"][1]["amount"]["employment_model"] = "B2B"
    blocked = build_compensation_comparison(
        CompensationLedger.model_validate(mismatch),
        profile(),
        current_role_id="sample_current",
        opportunity_id="sample_opportunity",
    )
    assert blocked.comparable is False
    assert blocked.floor.status == "unknown"
    assert any("договор B2B" in reason for reason in blocked.non_comparable_reasons)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("period", "unknown", "период оплаты не раскрыт"),
        ("basis", "unknown", "gross/net не раскрыт"),
        ("basis", "net", "basis net не совпадает с gross"),
        ("component", "unknown", "base/total не раскрыт"),
        ("component", "total", "указано total, а не base"),
        ("currency", "USD", "валюта USD не совпадает с PLN"),
        ("employment_model", "unknown", "тип договора не раскрыт"),
    ],
)
def test_unknown_or_mismatching_opportunity_metadata_is_visible_and_not_eligible(
    field: str, value: str, reason: str
) -> None:
    data = ledger().model_dump(mode="json")
    data["observations"][1]["amount"][field] = value
    blocked = build_compensation_comparison(
        CompensationLedger.model_validate(data),
        profile(),
        current_role_id="sample_current",
        opportunity_id="sample_opportunity",
    )
    assert blocked.comparable is False
    assert blocked.floor.status == "unknown"
    assert any(reason in item for item in blocked.non_comparable_reasons)


@pytest.mark.parametrize(
    "mutator, message",
    [
        (
            lambda data: data["observations"][0]["amount"].update(minimum=30000, maximum=20000),
            "minimum exceeds maximum",
        ),
        (
            lambda data: data["observations"][0].update(disclosed_on="2026-09-21"),
            "disclosure date exceeds source recording",
        ),
    ],
)
def test_invalid_amount_and_disclosure_date_fail_closed(mutator, message: str) -> None:
    data = ledger().model_dump(mode="json")
    mutator(data)
    with pytest.raises(ValidationError, match=message):
        CompensationLedger.model_validate(data)


def test_negative_fractional_delta_keeps_its_sign_in_markdown() -> None:
    data = ledger().model_dump(mode="json")
    data["observations"][1]["amount"].update(minimum=100000, maximum=100000)
    comparison = build_compensation_comparison(
        CompensationLedger.model_validate(data),
        profile(),
        current_role_id="sample_current",
        opportunity_id="sample_opportunity",
    )
    assert "| Разница | -1 666.67 PLN/месяц;" in render_compensation_comparison(comparison)


def test_historical_profile_employment_cannot_be_selected_as_current_role() -> None:
    data = ledger().model_dump(mode="json")
    data["observations"][0]["subject"]["id"] = "sample_previous"
    with pytest.raises(ConfigurationError, match="ongoing H3 employment"):
        build_compensation_comparison(
            CompensationLedger.model_validate(data),
            profile(),
            current_role_id="sample_previous",
            opportunity_id="sample_opportunity",
        )


def test_cli_record_is_atomic_for_initial_and_invalid_append(tmp_path: Path) -> None:
    new_ledger = tmp_path / "ledger.yaml"
    record_path = tmp_path / "record.yaml"
    initial_data = correction().model_dump(mode="json")
    initial_data["observation"]["id"] = "initial_sample_range"
    initial_data["observation"]["supersedes"] = None
    record_path.write_text(yaml.safe_dump({"compensation_record": initial_data}), encoding="utf-8")
    runner = CliRunner()

    first = runner.invoke(
        app,
        [
            "career",
            "compensation",
            "record",
            "--ledger",
            str(new_ledger),
            "--record",
            str(record_path),
        ],
    )
    assert first.exit_code == 0, first.output
    before = new_ledger.read_bytes()
    second = runner.invoke(
        app,
        [
            "career",
            "compensation",
            "record",
            "--ledger",
            str(new_ledger),
            "--record",
            str(record_path),
        ],
    )
    assert second.exit_code == 2
    assert new_ledger.read_bytes() == before


def test_cli_comparison_json_and_markdown_share_canonical_result_and_preserve_outputs(
    tmp_path: Path,
) -> None:
    markdown = tmp_path / "comparison.md"
    result = CliRunner().invoke(
        app,
        [
            "career",
            "compensation",
            "compare",
            "--ledger",
            str(LEDGER),
            "--profile",
            str(PROFILE),
            "--current-role",
            "sample_current",
            "--opportunity",
            "sample_opportunity",
            "--output",
            str(markdown),
        ],
    )
    assert result.exit_code == 0, result.output
    expected = build_compensation_comparison(
        ledger(),
        profile(),
        current_role_id="sample_current",
        opportunity_id="sample_opportunity",
    )
    assert markdown.read_text(encoding="utf-8") == render_compensation_comparison(expected)

    serialized = tmp_path / "comparison.json"
    result = CliRunner().invoke(
        app,
        [
            "career",
            "compensation",
            "compare",
            "--ledger",
            str(LEDGER),
            "--profile",
            str(PROFILE),
            "--current-role",
            "sample_current",
            "--opportunity",
            "sample_opportunity",
            "--output",
            str(serialized),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(serialized.read_text()) == expected.model_dump(mode="json")

    before = markdown.read_bytes()
    result = CliRunner().invoke(
        app,
        [
            "career",
            "compensation",
            "compare",
            "--ledger",
            str(LEDGER),
            "--profile",
            str(PROFILE),
            "--current-role",
            "sample_current",
            "--opportunity",
            "sample_opportunity",
            "--output",
            str(markdown),
            "--format",
            "invalid",
        ],
    )
    assert result.exit_code == 2
    assert markdown.read_bytes() == before
