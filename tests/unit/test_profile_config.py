"""Strict profile parsing and taxonomy-reference tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from career_os.config.profiles import Profile, load_profile
from career_os.config.skills import load_skill_taxonomy
from career_os.config.sources import ConfigurationError

TAXONOMY_PATH = Path("config/taxonomy/skills.v1.yaml")
EXAMPLE_PATH = Path("config/profile.example.yaml")


def test_checked_in_profile_example_validates_against_current_taxonomy() -> None:
    profile = load_profile(EXAMPLE_PATH, taxonomy=load_skill_taxonomy(TAXONOMY_PATH))

    assert profile.name == "current-backend"
    assert profile.target_role_families == (
        "backend_engineering",
        "platform_engineering",
    )
    assert profile.skill_ids == frozenset({"python", "postgresql", "kafka", "terraform"})
    assert profile.seniority == "senior"


def test_profile_file_loads_declared_metadata_and_taxonomy_skills(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text(
        """
        profile:
          name: current
          target_role_families: [backend_engineering, platform_engineering]
          seniority: senior
          skills:
            - {id: python, level: strong, years: 7}
            - {id: postgresql, level: working}
        """,
        encoding="utf-8",
    )

    profile = load_profile(path, taxonomy=load_skill_taxonomy(TAXONOMY_PATH))

    assert profile.name == "current"
    assert profile.skills[0].years == 7
    assert profile.skill_ids == frozenset({"python", "postgresql"})


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (
            "profile: {name: p, target_role_families: [backend_engineering], "
            "seniority: senior, skills: [], x: 1}",
            "x",
        ),
        (
            "profile: {name: p, target_role_families: [backend_engineering], seniority: senior, "
            "skills: [{id: python, level: strong, x: 1}]}",
            "x",
        ),
        (
            "profile: {name: p, target_role_families: [backend_engineering], seniority: senior, "
            "skills: [{id: Python, level: strong}]}",
            "lowercase snake_case",
        ),
        (
            "profile: {name: p, target_role_families: [backend_engineering], seniority: senior, "
            "skills: [{id: python, level: legendary}]}",
            "level",
        ),
        (
            "profile: {name: p, target_role_families: [backend_engineering], seniority: senior, "
            "skills: [{id: python, level: strong, years: -1}]}",
            "years",
        ),
        (
            "profile: {name: p, target_role_families: [backend_engineering], "
            "seniority: senior, skills: []}\nextra: 1",
            "root",
        ),
    ],
)
def test_profile_rejects_unknown_keys_and_invalid_controlled_values(
    tmp_path: Path, document: str, message: str
) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_profile(path, taxonomy=load_skill_taxonomy(TAXONOMY_PATH))


def test_profile_rejects_duplicate_skills_roles_yaml_keys_and_unknown_taxonomy_ids(
    tmp_path: Path,
) -> None:
    taxonomy = load_skill_taxonomy(TAXONOMY_PATH)
    path = tmp_path / "profile.yaml"
    base = "name: p, target_role_families: [backend_engineering], seniority: senior"

    path.write_text(
        f"profile: {{{base}, skills: [{{id: python, level: strong}}, "
        "{id: python, level: working}]}",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="duplicate skill IDs"):
        load_profile(path, taxonomy=taxonomy)

    path.write_text(
        "profile: {name: p, target_role_families: [backend_engineering, backend_engineering], "
        "seniority: senior, skills: []}",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="duplicate role families"):
        load_profile(path, taxonomy=taxonomy)

    path.write_text(
        f"profile: {{{base}, skills: [{{id: imaginary_db, level: working}}]}}",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="unknown taxonomy IDs: imaginary_db"):
        load_profile(path, taxonomy=taxonomy)

    path.write_text(
        "profile:\n  name: first\n  name: second\n  target_role_families: []\n"
        "  target_role_families: [backend_engineering]\n  seniority: senior\n  skills: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="Malformed YAML"):
        load_profile(path, taxonomy=taxonomy)


def test_direct_profile_schema_rejects_non_finite_years_and_duplicate_skills() -> None:
    common: dict[str, object] = {
        "name": "p",
        "target_role_families": ["backend_engineering"],
        "seniority": "senior",
    }
    with pytest.raises(ValueError, match="finite number"):
        Profile.model_validate(
            {**common, "skills": [{"id": "python", "level": "strong", "years": "NaN"}]}
        )
    with pytest.raises(ValueError, match="duplicate skill IDs"):
        Profile.model_validate(
            {
                **common,
                "skills": [
                    {"id": "python", "level": "strong"},
                    {"id": "python", "level": "working"},
                ],
            }
        )
