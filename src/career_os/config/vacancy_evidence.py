"""H4 manually selected evidence, anchored to a retained vacancy snapshot."""

from __future__ import annotations

from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from career_os.config.career_profiles import Identifier, Record, Text
from career_os.config.skills import _load_yaml_mapping
from career_os.config.sources import ConfigurationError, _validation_message

Section = Literal["required", "preferred", "responsibility", "context"]
HEADINGS: dict[str, Section] = {
    "You have:": "required",
    "We prefer:": "preferred",
    "You will:": "responsibility",
}


def fingerprint(record: Record) -> str:
    """Digest the canonical model, not YAML formatting or file location."""
    return sha256(record.model_dump_json().encode()).hexdigest()


class VacancyEvidence(Record):
    id: Identifier
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: Text
    section: Section
    summary: Text
    signal: Literal["skill", "experience", "education", "scope", "leadership", "domain"]


class VacancySnapshot(Record):
    schema_version: Literal["h4.vacancy-evidence.v1"]
    vacancy_id: Text
    company: Text
    title: Text
    source_url: Text
    retained_reference: Text
    captured_on: date
    source_text: Text
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    annotation_method: Literal["manual"]
    evidence: tuple[VacancyEvidence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def anchored_and_complete(self) -> VacancySnapshot:
        if sha256(self.source_text.encode()).hexdigest() != self.source_sha256:
            raise ValueError("source snapshot digest mismatch")
        ids = [item.id for item in self.evidence]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate vacancy evidence IDs")
        # This bounded first contract supports these explicit English section headings.
        # Every bullet must be annotated once, including preferences and mixed clauses.
        section: Section = "context"
        expected: dict[tuple[int, int], Section] = {}
        offset = 0
        for line in self.source_text.splitlines(keepends=True):
            stripped = line.strip()
            if stripped in HEADINGS:
                section = HEADINGS[stripped]
            elif stripped.startswith("• "):
                start = offset + line.index("• ") + 2
                end = offset + len(line.rstrip())
                if section == "context":
                    raise ValueError("bullet outside supported section")
                expected[(start, end)] = section
            elif stripped and section != "context" and stripped != "#LI-Hybrid":
                raise ValueError("unsupported text/heading within classified sections")
            offset += len(line)
        seen: set[tuple[int, int]] = set()
        for item in self.evidence:
            span = (item.start, item.end)
            if span in seen:
                raise ValueError("duplicate evidence span")
            seen.add(span)
            if self.source_text[item.start : item.end] != item.quote:
                raise ValueError("quote does not match source span")
            if span not in expected or expected[span] != item.section:
                raise ValueError("evidence must preserve complete bullet and section")
        if seen != set(expected):
            raise ValueError("every source bullet must be classified exactly once")
        return self


def load_vacancy_snapshot(path: str | Path) -> VacancySnapshot:
    data = _load_yaml_mapping(path, subject="vacancy evidence")
    if set(data) != {"vacancy"}:
        raise ConfigurationError("Vacancy root must contain only vacancy")
    try:
        return VacancySnapshot.model_validate(data["vacancy"])
    except ValidationError as error:
        raise ConfigurationError(_validation_message("vacancy evidence", error)) from error
