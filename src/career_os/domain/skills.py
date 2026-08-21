"""Source-neutral skill evidence produced by deterministic enrichment."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

_SKILL_ID_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]*$")
_SECTION_VALUES: Final = frozenset({"title", "requirements", "responsibilities", "other"})

SkillEvidenceSection = Literal["title", "requirements", "responsibilities", "other"]
SkillEvidenceMethod = Literal["alias", "exact", "pattern"]


@dataclass(frozen=True, slots=True)
class SkillEvidence:
    """One exact, reproducible skill mention in a canonical job text section."""

    skill_id: str
    matched_text: str
    section: SkillEvidenceSection
    method: SkillEvidenceMethod
    confidence: Decimal
    taxonomy_version: str

    def __post_init__(self) -> None:
        if not _SKILL_ID_PATTERN.fullmatch(self.skill_id):
            raise ValueError("skill_id must be lowercase snake_case")
        if not isinstance(self.matched_text, str) or not self.matched_text.strip():
            raise ValueError("matched_text must be non-blank")
        if self.section not in _SECTION_VALUES:
            raise ValueError("section must be a supported canonical text section")
        if self.method not in {"alias", "exact", "pattern"}:
            raise ValueError("method must be a deterministic extraction method")
        is_confidence_valid = isinstance(self.confidence, Decimal) and Decimal(
            "0"
        ) <= self.confidence <= Decimal("1")
        if not is_confidence_valid:
            raise ValueError("confidence must be a Decimal between zero and one")
        if not isinstance(self.taxonomy_version, str) or not self.taxonomy_version.strip():
            raise ValueError("taxonomy_version must be non-blank")
