"""H3 provenance boundaries and the synthetic example profile."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from career_os.cli import app
from career_os.config.career_profiles import CareerProfile, load_career_profile
from career_os.config.skills import load_skill_taxonomy
from career_os.config.sources import ConfigurationError

PROFILE = Path("config/career-profile.example.yaml")
TAXONOMY = Path("config/taxonomy/skills.v1.yaml")


def payload():
    return yaml.safe_load(PROFILE.read_text())["career_profile"]


def test_synthetic_profile_preserves_evidence_and_current_intent():
    profile = load_career_profile(PROFILE, taxonomy=load_skill_taxonomy(TAXONOMY))
    claims = {claim.id: claim for claim in profile.claims}
    assert profile.review_status == "draft"
    assert profile.target.role_families == ("backend_engineering",)
    assert profile.target.minimum_base_pay == 14000
    assert profile.target.pay_basis == "gross"
    assert profile.target.employment_model == "UoP"
    assert profile.target.onsite_or_hybrid_cities == ("Warsaw",)
    assert not profile.target.relocation
    assert len(profile.employment) == 2
    assert profile.employment[0].title == "Software Engineer"
    assert profile.employment[0].start == "2025-01"
    assert profile.employment[0].evidence.status == "document_unreviewed"
    assert claims["feature_lead"].evidence.status == "user_confirmed"
    assert claims["no_coordination"].context == "Synthetic example initiative"
    assert claims["monetization"].category == "business_goal"
    assert claims["initiative_outcome"].evidence.status == "unknown"
    assert claims["direction"].evidence.status == "interpretation"
    assert {skill.id for skill in profile.skills} == {
        "python",
        "go",
        "postgresql",
        "docker",
    }
    assert all(skill.years is None for skill in profile.skills)
    assert CareerProfile.model_validate_json(profile.model_dump_json()) == profile


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda p: p["claims"][0]["evidence"].update(source_ids=["missing"]), "unknown source"),
        (lambda p: p["claims"][0]["evidence"].update(source_ids=["cv"]), "user_statement"),
        (
            lambda p: p["employment"][0]["evidence"].update(source_ids=["analysis"]),
            "document source",
        ),
        (lambda p: p["employment"][0].update(end="2024-01"), "precedes"),
        (lambda p: p["employment"][0].update(start="2025-13"), "pattern"),
        (lambda p: p["claims"].append(deepcopy(p["claims"][0])), "duplicate claims"),
        (lambda p: p["sources"].append(deepcopy(p["sources"][0])), "duplicate sources"),
        (lambda p: p["skills"].append(deepcopy(p["skills"][0])), "duplicate skills"),
        (lambda p: p["claims"][0]["evidence"].update(source_ids=["cv", "cv"]), "duplicate source"),
        (lambda p: p.update(review_status="user_reviewed"), "explicit user review"),
        (lambda p: p.update(review_source_id="target_review"), "draft"),
        (lambda p: p["sources"][0].update(recorded_on="2026-09-09"), "exceeds"),
        (lambda p: p["skills"][0]["evidence"].update(status="interpretation"), "asserted evidence"),
        (lambda p: p["target"]["evidence"].update(status="unknown"), "user-confirmed"),
        (lambda p: p["target"].update(minimum_base_pay="NaN"), "finite"),
        (lambda p: p["target"].update(role_families=["backend_engineering"] * 2), "duplicate"),
        (lambda p: p["claims"][0].update(statement="  "), "at least 1"),
        (lambda p: p.update(fit_probability=0.9), "Extra inputs"),
    ],
)
def test_rejects_invalid_or_misrepresented_evidence(change, message):
    data = payload()
    change(data)
    with pytest.raises(ValidationError, match=message):
        CareerProfile.model_validate(data)


@pytest.mark.parametrize(
    "document, message",
    [
        ("career_profile: {}\ncareer_profile: {}", "Malformed YAML"),
        ("career_profile: {}\nextra: true", "root"),
        ("career_profile: []", "dictionary"),
        ("[one, two]", "mapping"),
    ],
)
def test_loader_fails_closed(tmp_path, document, message):
    path = tmp_path / "bad.yaml"
    path.write_text(document)
    with pytest.raises(ConfigurationError, match=message):
        load_career_profile(path, taxonomy=load_skill_taxonomy(TAXONOMY))


def test_unknown_skill_and_legacy_profile_are_not_silently_accepted(tmp_path):
    data = payload()
    data["skills"][0]["id"] = "imaginary_skill"
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"career_profile": data}))
    with pytest.raises(ConfigurationError, match="Unknown taxonomy"):
        load_career_profile(path, taxonomy=load_skill_taxonomy(TAXONOMY))
    with pytest.raises(ConfigurationError, match="root"):
        load_career_profile("config/profile.example.yaml", taxonomy=load_skill_taxonomy(TAXONOMY))


def test_cli_emits_roundtrippable_profile_and_useful_failure(tmp_path):
    runner = CliRunner()
    result = runner.invoke(app, ["profile", "validate", "--profile", str(PROFILE)])
    assert result.exit_code == 0, result.output
    assert CareerProfile.model_validate_json(result.stdout).review_status == "draft"
    result = runner.invoke(app, ["profile", "validate", "--profile", str(tmp_path / "missing")])
    assert result.exit_code == 2
    assert "does not exist" in result.output


def test_published_schema_tracks_runtime_contract():
    import json

    schema = json.loads(Path("docs/h3-career-profile.schema.json").read_text())
    assert schema == CareerProfile.model_json_schema()
