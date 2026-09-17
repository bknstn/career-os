"""H5 bounded review assembly from explicit, digest-bound manual judgments."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from career_os.config.career_profiles import CareerProfile, Evidence, Identifier, Record, Text
from career_os.config.skills import _load_yaml_mapping
from career_os.config.sources import ConfigurationError, _validation_message
from career_os.config.vacancy_evidence import VacancySnapshot, fingerprint


class ProfileReference(Record):
    collection: Literal["claims", "skills", "employment"]
    id: Identifier


class Assessment(Record):
    evidence_id: Identifier
    relation: Literal["relevant", "partial", "not_established"]
    profile_refs: tuple[ProfileReference, ...]
    rationale: Text
    next_check: Text


class ConstraintCheck(Record):
    dimension: Literal["role", "location", "work_mode", "employment", "base_pay"]
    status: Literal["compatible", "unknown", "conflict"]
    source_excerpt: str
    rationale: Text
    next_check: Text


class ReviewPlan(Record):
    schema_version: Literal["h5.review-plan.v1"]
    method: Literal["manual_evidence_mapping"]
    reviewed_on: date
    profile_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    vacancy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    priority_evidence_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=3)
    assessments: tuple[Assessment, ...]
    constraints: tuple[ConstraintCheck, ...]

    @model_validator(mode="after")
    def unique_coverage(self) -> ReviewPlan:
        if len(set(self.priority_evidence_ids)) != len(self.priority_evidence_ids):
            raise ValueError("duplicate priority evidence IDs")
        ids = [item.evidence_id for item in self.assessments]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate assessment IDs")
        dimensions = [item.dimension for item in self.constraints]
        if len(set(dimensions)) != len(dimensions) or set(dimensions) != {
            "role",
            "location",
            "work_mode",
            "employment",
            "base_pay",
        }:
            raise ValueError("all five constraint dimensions must appear exactly once")
        for item in self.assessments:
            refs = [(ref.collection, ref.id) for ref in item.profile_refs]
            if len(set(refs)) != len(refs):
                raise ValueError("duplicate profile references")
        return self


class ResolvedProfileEvidence(Record):
    reference: ProfileReference
    statement: Text
    evidence: Evidence


class ReviewRow(Record):
    evidence_id: Identifier
    section: Text
    summary: Text
    relation: Text
    profile_evidence: tuple[ResolvedProfileEvidence, ...]
    rationale: Text
    next_check: Text


class CareerReview(Record):
    schema_version: Literal["h5.career-review.v1"] = "h5.career-review.v1"
    method: Literal["manual_evidence_mapping"] = "manual_evidence_mapping"
    reviewed_on: date
    profile: CareerProfile
    vacancy: VacancySnapshot
    plan: ReviewPlan
    rows: tuple[ReviewRow, ...]
    constraint_status: Literal["conflict", "unresolved", "compatible_by_manual_review"]


def load_review_plan(path: str | Path) -> ReviewPlan:
    data = _load_yaml_mapping(path, subject="career review plan")
    if set(data) != {"review"}:
        raise ConfigurationError("Review root must contain only review")
    try:
        return ReviewPlan.model_validate(data["review"])
    except ValidationError as error:
        raise ConfigurationError(_validation_message("career review plan", error)) from error


def build_career_review(
    profile: CareerProfile, vacancy: VacancySnapshot, plan: ReviewPlan
) -> CareerReview:
    """Reconcile evidence; never infer competence, years, eligibility or probability."""
    if plan.profile_sha256 != fingerprint(profile) or plan.vacancy_sha256 != fingerprint(vacancy):
        raise ConfigurationError("Review inputs changed; manual mapping must be reviewed again")
    if plan.reviewed_on < max(profile.evidence_as_of, vacancy.captured_on):
        raise ConfigurationError("Review date precedes input evidence")
    if {a.evidence_id for a in plan.assessments} != {e.id for e in vacancy.evidence}:
        raise ConfigurationError("Assessments must cover every vacancy evidence ID exactly once")
    actionable_ids = {a.evidence_id for a in plan.assessments if a.relation != "relevant"}
    required_ids = {e.id for e in vacancy.evidence if e.section == "required"}
    if not set(plan.priority_evidence_ids) <= actionable_ids & required_ids:
        raise ConfigurationError("Review priorities must reference unresolved required evidence")
    resolved: dict[tuple[str, str], ResolvedProfileEvidence] = {}
    for claim in profile.claims:
        resolved[("claims", claim.id)] = ResolvedProfileEvidence(
            reference=ProfileReference(collection="claims", id=claim.id),
            statement=f"{claim.context}: {claim.statement}",
            evidence=claim.evidence,
        )
    for skill in profile.skills:
        resolved[("skills", skill.id)] = ResolvedProfileEvidence(
            reference=ProfileReference(collection="skills", id=skill.id),
            statement=(
                f"{skill.id}: {skill.level}; стаж: "
                f"{skill.years if skill.years is not None else 'неизвестен'}"
            ),
            evidence=skill.evidence,
        )
    for job in profile.employment:
        resolved[("employment", job.id)] = ResolvedProfileEvidence(
            reference=ProfileReference(collection="employment", id=job.id),
            statement=f"{job.employer}: {job.title}, {job.start} — {job.end or 'ongoing'}",
            evidence=job.evidence,
        )
    by_id = {item.evidence_id: item for item in plan.assessments}
    rows = []
    for evidence in vacancy.evidence:
        assessment = by_id[evidence.id]
        refs = []
        for ref in assessment.profile_refs:
            record = resolved.get((ref.collection, ref.id))
            if record is None:
                raise ConfigurationError("Unknown profile evidence reference")
            refs.append(record)
        asserted = [
            r for r in refs if r.evidence.status in ("user_confirmed", "document_unreviewed")
        ]
        if assessment.relation in ("relevant", "partial") and not asserted:
            raise ConfigurationError("Relevant/partial mapping requires asserted profile evidence")
        if assessment.relation == "relevant" and len(asserted) != len(refs):
            raise ConfigurationError("Unknowns/interpretations cannot support a relevant verdict")
        rows.append(
            ReviewRow(
                evidence_id=evidence.id,
                section=evidence.section,
                summary=evidence.summary,
                relation=assessment.relation,
                profile_evidence=tuple(refs),
                rationale=assessment.rationale,
                next_check=assessment.next_check,
            )
        )
    for check in plan.constraints:
        if check.source_excerpt and check.source_excerpt not in vacancy.source_text:
            raise ConfigurationError("Constraint excerpt must be in vacancy snapshot")
        if check.status != "unknown" and not check.source_excerpt.strip():
            raise ConfigurationError("Constraint verdict requires explicit vacancy evidence")
    statuses = {c.status for c in plan.constraints}
    status: Literal["conflict", "unresolved", "compatible_by_manual_review"] = (
        "conflict"
        if "conflict" in statuses
        else "unresolved"
        if "unknown" in statuses
        else "compatible_by_manual_review"
    )
    return CareerReview(
        reviewed_on=plan.reviewed_on,
        profile=profile,
        vacancy=vacancy,
        plan=plan,
        rows=tuple(rows),
        constraint_status=status,
    )
