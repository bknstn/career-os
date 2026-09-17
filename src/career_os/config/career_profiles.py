"""File-backed H3 profile with explicit evidence status; no career scoring."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from career_os.config.profiles import ProfileSkill
from career_os.config.skills import SkillTaxonomy, _load_yaml_mapping
from career_os.config.sources import ConfigurationError, _validation_message
from career_os.domain.normalization import RoleFamily, Seniority

Text = Annotated[str, Field(min_length=1)]
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
Month = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
EvidenceStatus = Literal["user_confirmed", "document_unreviewed", "interpretation", "unknown"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ProfileSource(Record):
    id: Identifier
    kind: Literal["user_statement", "document", "analysis"]
    reference: Text
    locator: Text
    recorded_on: date


class Evidence(Record):
    status: EvidenceStatus
    source_ids: tuple[Identifier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_sources(self) -> Evidence:
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("duplicate source references")
        return self


class CareerClaim(Record):
    id: Identifier
    category: Literal[
        "current_role",
        "domain",
        "scope",
        "architecture",
        "technical_leadership",
        "people_management",
        "business_goal",
        "outcome",
        "preference",
        "constraint",
        "skill_context",
        "language",
        "education",
        "career_direction",
    ]
    context: Text
    statement: Text
    evidence: Evidence


class Employment(Record):
    id: Identifier
    employer: Text
    title: Text
    start: Month
    end: Month | None
    evidence: Evidence

    @model_validator(mode="after")
    def chronological(self) -> Employment:
        if self.end is not None and self.end < self.start:
            raise ValueError("employment end precedes start")
        if self.evidence.status not in ("user_confirmed", "document_unreviewed"):
            raise ValueError("employment requires asserted evidence")
        return self


class CareerSkill(ProfileSkill):
    evidence: Evidence

    @model_validator(mode="after")
    def declared_level(self) -> CareerSkill:
        if self.evidence.status not in ("user_confirmed", "document_unreviewed"):
            raise ValueError("skill proficiency requires asserted evidence")
        return self


class CareerTarget(Record):
    """User-declared intent, not an executable H2 selection or eligibility verdict."""

    role_families: tuple[RoleFamily, ...] = Field(min_length=1)
    primary_seniority: Seniority
    exploratory_seniorities: tuple[Seniority, ...]
    country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
    remote_from_country: bool
    onsite_or_hybrid_cities: tuple[Text, ...]
    relocation: bool
    employment_model: Text
    minimum_base_pay: Decimal = Field(gt=0, allow_inf_nan=False)
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    pay_period: Literal["month", "year", "hour"]
    pay_basis: Literal["gross", "net"]
    accept_undisclosed_pay_for_review: bool
    evidence: Evidence

    @model_validator(mode="after")
    def explicit_intent(self) -> CareerTarget:
        if self.evidence.status != "user_confirmed":
            raise ValueError("target requires user-confirmed intent")
        for values in (
            self.role_families,
            self.exploratory_seniorities,
            self.onsite_or_hybrid_cities,
        ):
            if len(set(values)) != len(values):
                raise ValueError("duplicate target values")
        if self.primary_seniority in self.exploratory_seniorities:
            raise ValueError("primary seniority must not also be exploratory")
        return self


class CareerProfile(Record):
    schema_version: Literal["h3.career-profile.v2"]
    name: Text
    evidence_as_of: date
    review_status: Literal["draft", "user_reviewed"]
    review_source_id: Identifier | None = None
    sources: tuple[ProfileSource, ...] = Field(min_length=1)
    target: CareerTarget
    employment: tuple[Employment, ...]
    skills: tuple[CareerSkill, ...]
    claims: tuple[CareerClaim, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def reconcile_evidence(self) -> CareerProfile:
        for label, records in (
            ("sources", self.sources),
            ("employment", self.employment),
            ("skills", self.skills),
            ("claims", self.claims),
        ):
            ids = [record.id for record in records]
            if len(set(ids)) != len(ids):
                raise ValueError(f"duplicate {label} IDs")
        sources = {source.id: source for source in self.sources}
        if any(source.recorded_on > self.evidence_as_of for source in self.sources):
            raise ValueError("source date exceeds evidence_as_of")
        evidence_records = (
            self.target.evidence,
            *(item.evidence for item in self.employment),
            *(item.evidence for item in self.skills),
            *(item.evidence for item in self.claims),
        )
        for evidence in evidence_records:
            if set(evidence.source_ids) - sources.keys():
                raise ValueError("unknown source reference")
            kinds = {sources[key].kind for key in evidence.source_ids}
            if evidence.status == "user_confirmed" and "user_statement" not in kinds:
                raise ValueError("user_confirmed requires a user_statement source")
            if evidence.status == "document_unreviewed" and "document" not in kinds:
                raise ValueError("document_unreviewed requires a document source")
        if self.review_status == "user_reviewed":
            source = sources.get(self.review_source_id or "")
            if source is None or source.kind != "user_statement":
                raise ValueError("user_reviewed requires an explicit user review source")
        elif self.review_source_id is not None:
            raise ValueError("draft must not declare a review source")
        return self


def load_career_profile(path: str | Path, *, taxonomy: SkillTaxonomy) -> CareerProfile:
    """Validate strict YAML, evidence references and canonical skill IDs offline."""
    parsed = _load_yaml_mapping(path, subject="career profile")
    if set(parsed) != {"career_profile"}:
        raise ConfigurationError("Career profile root must contain only career_profile")
    try:
        profile = CareerProfile.model_validate(parsed["career_profile"])
    except ValidationError as error:
        raise ConfigurationError(_validation_message("career profile", error)) from error
    unknown = {skill.id for skill in profile.skills} - {skill.id for skill in taxonomy.skills}
    if unknown:
        raise ConfigurationError("Unknown taxonomy IDs: " + ", ".join(sorted(unknown)))
    return profile
