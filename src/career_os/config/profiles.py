"""Strict, taxonomy-validated local profile configuration."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from career_os.config.skills import SkillTaxonomy, _load_yaml_mapping
from career_os.config.sources import ConfigurationError, _validation_message
from career_os.domain.normalization import RoleFamily, Seniority

NonBlankText = Annotated[str, Field(min_length=1)]
ProfileSkillLevel = Literal["learning", "working", "strong", "expert"]
_SKILL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class ProfileSkill(BaseModel):
    """One declared canonical skill; level and years are profile evidence only."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str
    level: ProfileSkillLevel
    years: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("id")
    @classmethod
    def stable_skill_id(cls, value: str) -> str:
        if not _SKILL_ID_PATTERN.fullmatch(value):
            raise ValueError("must be lowercase snake_case")
        return value


class Profile(BaseModel):
    """Declared profile metadata; it does not silently re-filter market jobs."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: NonBlankText
    target_role_families: tuple[RoleFamily, ...] = Field(min_length=1)
    seniority: Seniority
    skills: tuple[ProfileSkill, ...]

    @field_validator("target_role_families")
    @classmethod
    def unique_role_families(cls, values: tuple[RoleFamily, ...]) -> tuple[RoleFamily, ...]:
        if len(set(values)) != len(values):
            raise ValueError("contains duplicate role families")
        return values

    @model_validator(mode="after")
    def unique_skills(self) -> Profile:
        skill_ids = tuple(skill.id for skill in self.skills)
        if len(set(skill_ids)) != len(skill_ids):
            raise ValueError("contains duplicate skill IDs")
        return self

    @property
    def skill_ids(self) -> frozenset[str]:
        return frozenset(skill.id for skill in self.skills)


def load_profile(path: str | Path, *, taxonomy: SkillTaxonomy) -> Profile:
    """Load a documented ``profile:`` mapping and reject unknown taxonomy IDs."""
    config_path = Path(path)
    parsed = _load_yaml_mapping(config_path, subject="profile configuration")
    value: object = parsed.get("profile", parsed)
    if not isinstance(value, dict):
        raise ConfigurationError(
            f"Profile configuration must contain a profile mapping: {config_path}"
        )
    if "profile" in parsed and set(parsed) != {"profile"}:
        raise ConfigurationError(
            "Invalid profile configuration: root: extra keys are not permitted"
        )
    try:
        profile = Profile.model_validate(value)
    except ValidationError as error:
        raise ConfigurationError(_validation_message("profile configuration", error)) from error
    validate_profile_taxonomy(profile, taxonomy=taxonomy)
    return profile


def validate_profile_taxonomy(profile: Profile, *, taxonomy: SkillTaxonomy) -> None:
    """Fail closed when a declared skill has no stable taxonomy definition."""
    taxonomy_ids = {skill.id for skill in taxonomy.skills}
    unknown = sorted(profile.skill_ids - taxonomy_ids)
    if unknown:
        raise ConfigurationError(
            "Invalid profile configuration: skills reference unknown taxonomy IDs: "
            + ", ".join(unknown)
        )
