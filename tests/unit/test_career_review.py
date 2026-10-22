"""Evidence anchoring, stale-input protection and bounded report assembly."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from career_os.application.career_review import ReviewPlan, build_career_review, load_review_plan
from career_os.cli import app
from career_os.config.career_profiles import CareerProfile, load_career_profile
from career_os.config.skills import load_skill_taxonomy
from career_os.config.sources import ConfigurationError
from career_os.config.vacancy_evidence import VacancySnapshot, fingerprint, load_vacancy_snapshot
from career_os.reporting.career_review import render_career_review

PROFILE = Path("config/career-profile.example.yaml")
VACANCY = Path("tests/fixtures/career/vacancy.yaml")
PLAN = Path("tests/fixtures/career/review-plan.yaml")


def inputs():
    return (
        load_career_profile(
            PROFILE, taxonomy=load_skill_taxonomy("config/taxonomy/skills.v1.yaml")
        ),
        load_vacancy_snapshot(VACANCY),
        load_review_plan(PLAN),
    )


def test_synthetic_review_preserves_compound_requirements_and_uncertainties():
    profile, vacancy, plan = inputs()
    review = build_career_review(profile, vacancy, plan)
    rows = {row.evidence_id: row for row in review.rows}
    assert len(rows) == 18
    assert sum(row.section == "required" for row in review.rows) == 6
    assert sum(row.section == "preferred" for row in review.rows) == 4
    assert rows["cpp"].section == "preferred"
    assert rows["cpp"].relation == "not_established"
    assert rows["backend_years"].relation == "partial"
    assert rows["large_efforts"].relation == "partial"
    assert any(e.evidence.status == "unknown" for e in rows["backend_years"].profile_evidence)
    assert rows["mentorship"].profile_evidence[0].evidence.status == "document_unreviewed"
    assert review.constraint_status == "unresolved"
    assert {c.dimension for c in plan.constraints if c.status == "unknown"} == {
        "employment",
        "base_pay",
    }
    assert [r.evidence_id for r in review.rows] == [e.id for e in vacancy.evidence]
    assert render_career_review(review) == render_career_review(review)


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda v: v["evidence"][0].update(quote="Invented requirement"), "source span"),
        (lambda v: v["evidence"][-1].update(section="required"), "section"),
        (lambda v: v["evidence"].pop(), "every source bullet"),
        (lambda v: v["evidence"].append(deepcopy(v["evidence"][0])), "duplicate"),
        (lambda v: v.update(source_sha256="0" * 64), "digest"),
        (lambda v: v["evidence"][0].update(start=-1), "greater than or equal"),
        (lambda v: v.update(annotation_method="automatic"), "manual"),
    ],
)
def test_vacancy_rejects_changed_or_cherry_picked_evidence(mutation, message):
    data = yaml.safe_load(VACANCY.read_text())["vacancy"]
    mutation(data)
    with pytest.raises(ValidationError, match=message):
        VacancySnapshot.model_validate(data)


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda p: p.update(profile_sha256="0" * 64), "inputs changed"),
        (lambda p: p.update(vacancy_sha256="0" * 64), "inputs changed"),
        (lambda p: p.update(reviewed_on="2026-09-01"), "precedes"),
        (lambda p: p["assessments"].pop(), "every vacancy"),
        (lambda p: p["assessments"][0].update(evidence_id="not_real"), "every vacancy"),
        (lambda p: p["assessments"][0]["profile_refs"][0].update(id="missing"), "Unknown profile"),
        (lambda p: p["assessments"][0].update(profile_refs=[]), "asserted"),
        (
            lambda p: p["assessments"][0].update(
                profile_refs=[{"collection": "claims", "id": "direction"}]
            ),
            "asserted",
        ),
        (
            lambda p: p["assessments"][0].update(
                profile_refs=[{"collection": "claims", "id": "production"}]
            ),
            "asserted",
        ),
        (lambda p: p["assessments"][3].update(relation="relevant"), "Unknowns/interpretations"),
        (lambda p: p["constraints"][0].update(source_excerpt="not present"), "excerpt"),
        (lambda p: p["constraints"][0].update(source_excerpt=""), "explicit vacancy"),
        (lambda p: p.update(priority_evidence_ids=["cpp"]), "unresolved required"),
        (lambda p: p.update(priority_evidence_ids=["missing"]), "unresolved required"),
    ],
)
def test_builder_rejects_stale_or_unsupported_mapping(mutation, message):
    profile, vacancy, plan = inputs()
    data = plan.model_dump(mode="json")
    mutation(data)
    with pytest.raises(ConfigurationError, match=message):
        build_career_review(profile, vacancy, ReviewPlan.model_validate(data))


def test_changing_profile_requires_mapping_review_even_when_ids_survive():
    profile, vacancy, plan = inputs()
    data = profile.model_dump(mode="json")
    data["skills"][0]["level"] = "learning"
    changed = CareerProfile.model_validate(data)
    with pytest.raises(ConfigurationError, match="inputs changed"):
        build_career_review(changed, vacancy, plan)


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda p: p["constraints"].pop(), "five constraint"),
        (lambda p: p["constraints"].append(deepcopy(p["constraints"][0])), "five constraint"),
        (lambda p: p["assessments"].append(deepcopy(p["assessments"][0])), "duplicate assessment"),
        (
            lambda p: p["assessments"][0]["profile_refs"].append(
                deepcopy(p["assessments"][0]["profile_refs"][0])
            ),
            "duplicate profile",
        ),
        (lambda p: p.update(priority_evidence_ids=["large_efforts"] * 2), "duplicate priority"),
        (lambda p: p.update(fit_score=90), "Extra inputs"),
    ],
)
def test_plan_rejects_ambiguous_or_incomplete_inputs(mutation, message):
    data = yaml.safe_load(PLAN.read_text())["review"]
    mutation(data)
    with pytest.raises(ValidationError, match=message):
        ReviewPlan.model_validate(data)


def test_conflict_dominates_unknown_and_no_evidence_does_not_get_positive_opening():
    profile, vacancy, plan = inputs()
    data = plan.model_dump(mode="json")
    data["constraints"][0]["status"] = "conflict"
    for assessment in data["assessments"]:
        assessment.update(relation="not_established", profile_refs=[])
    review = build_career_review(profile, vacancy, ReviewPlan.model_validate(data))
    assert review.constraint_status == "conflict"
    rendered = render_career_review(review)
    assert "Релевантные свидетельства пока не установлены" in rendered
    assert "есть несовместимые условия" in rendered
    assert "Есть пересечения" not in rendered


def test_snapshot_text_reproduces_retained_public_response():
    import html
    import json

    from career_os.normalization.text import html_to_text

    _, vacancy, _ = inputs()
    raw = json.loads(Path(vacancy.retained_reference).read_text())
    text = f"{raw['title']}\n{raw['location']['name']}\n\n" + html_to_text(
        html.unescape(raw["content"])
    )
    assert raw["id"] == 7902617
    assert vacancy.source_text == text
    assert vacancy.source_sha256 == sha256(text.encode()).hexdigest()


@pytest.mark.parametrize(
    "loader, document",
    [
        (load_vacancy_snapshot, "vacancy: {}\nvacancy: {}"),
        (load_vacancy_snapshot, "vacancy: {}\nextra: 1"),
        (load_review_plan, "review: {}\nextra: 1"),
        (load_review_plan, "review: []"),
    ],
)
def test_loaders_fail_closed(tmp_path, loader, document):
    path = tmp_path / "bad.yaml"
    path.write_text(document)
    with pytest.raises(ConfigurationError):
        loader(path)


def test_cli_markdown_json_parity_and_invalid_input_preserves_output(tmp_path):
    import json

    runner = CliRunner()
    args = [
        "career",
        "review",
        "--profile",
        str(PROFILE),
        "--vacancy",
        str(VACANCY),
        "--review-plan",
        str(PLAN),
    ]
    md = tmp_path / "review.md"
    result = runner.invoke(app, [*args, "--output", str(md)])
    assert result.exit_code == 0, result.output
    js = tmp_path / "review.json"
    result = runner.invoke(app, [*args, "--output", str(js), "--format", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(js.read_text())
    profile, vacancy, plan = inputs()
    expected = build_career_review(profile, vacancy, plan)
    assert data == expected.model_dump(mode="json")
    assert md.read_text() == render_career_review(expected)
    before = md.read_bytes()
    result = runner.invoke(app, [*args, "--output", str(md), "--format", "invalid"])
    assert result.exit_code == 2
    assert md.read_bytes() == before
    result = runner.invoke(app, [*args, "--output", str(PROFILE)])
    assert result.exit_code == 2
    assert "overwrite" in result.output


def test_published_schemas_track_runtime():
    import json

    for model, filename in [
        (VacancySnapshot, "h4-vacancy-evidence.schema.json"),
        (ReviewPlan, "h5-review-plan.schema.json"),
    ]:
        assert json.loads((Path("docs") / filename).read_text()) == model.model_json_schema()


def test_fingerprint_survives_yaml_formatting():
    profile, vacancy, plan = inputs()
    assert fingerprint(profile) == plan.profile_sha256
    assert fingerprint(VacancySnapshot.model_validate_json(vacancy.model_dump_json())) == (
        plan.vacancy_sha256
    )
