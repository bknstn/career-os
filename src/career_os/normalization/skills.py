"""Deterministic, taxonomy-driven skill extraction with exact evidence spans."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, cast

from career_os.config.skills import SkillTaxonomy, SkillTaxonomySkill
from career_os.domain.skills import SkillEvidence, SkillEvidenceSection

_REGEX_FLAGS: Final = re.IGNORECASE
_KNOWN_SECTION_NAMES: Final = frozenset({"requirements", "responsibilities"})
_ALIAS_CONFIDENCE: Final = Decimal("1.000")


@dataclass(frozen=True, slots=True)
class _Match:
    """An internal candidate retaining source offsets until overlap resolution."""

    skill: SkillTaxonomySkill
    alias: str
    start: int
    end: int
    matched_text: str


@dataclass(frozen=True, slots=True)
class _TextSection:
    """One supplied text segment and its durable canonical section label."""

    ordinal: int
    section: SkillEvidenceSection
    text: str


class SkillExtractor:
    """Match taxonomy aliases deterministically without network or model calls.

    Aliases are always literals.  Word boundaries use ``\\w`` semantics on both
    sides of an alias unless the taxonomy explicitly disables them.  Ambiguous
    aliases are admitted only by their taxonomy-owned positive context and are
    rejected by taxonomy-owned negative context.  Context and skill-level
    negative rules apply only to the alias occurrence they overlap.
    """

    def __init__(self, taxonomy: SkillTaxonomy) -> None:
        self._taxonomy = taxonomy
        self._rules_by_alias = {
            rule.alias.casefold(): rule for rule in taxonomy.ambiguous_alias_rules
        }
        self._skills = tuple(sorted(taxonomy.skills, key=lambda skill: skill.id))

    @property
    def taxonomy_version(self) -> str:
        """The exact taxonomy version stamped on every emitted evidence row."""
        return self._taxonomy.version

    def extract(
        self,
        *,
        title: str | None = None,
        description: str | None = None,
        sections: Mapping[str, str] | None = None,
    ) -> tuple[SkillEvidence, ...]:
        """Return every distinct literal mention in a stable order.

        Dedicated title and description text are considered first.  Additional
        sections are ordered by their casefolded input key, then their original
        key, so mapping iteration order cannot affect extraction.  Unknown
        section names intentionally map to ``other`` because task 21 owns
        source-aware section normalizers.
        """
        text_sections = self._text_sections(title=title, description=description, sections=sections)
        evidence: list[tuple[int, int, int, str, str, SkillEvidence]] = []
        for text_section in text_sections:
            for match in self._matches_for_section(text_section.text):
                evidence.append(
                    (
                        text_section.ordinal,
                        match.start,
                        -len(match.matched_text),
                        match.skill.id,
                        match.alias.casefold(),
                        SkillEvidence(
                            skill_id=match.skill.id,
                            matched_text=match.matched_text,
                            section=text_section.section,
                            method="alias",
                            confidence=_ALIAS_CONFIDENCE,
                            taxonomy_version=self.taxonomy_version,
                        ),
                    )
                )
        return tuple(item[-1] for item in sorted(evidence))

    def _text_sections(
        self,
        *,
        title: str | None,
        description: str | None,
        sections: Mapping[str, str] | None,
    ) -> tuple[_TextSection, ...]:
        candidates: list[tuple[SkillEvidenceSection, str]] = []
        if title is not None:
            _require_text(title, field_name="title")
            candidates.append(("title", title))
        if description is not None:
            _require_text(description, field_name="description")
            candidates.append(("other", description))
        if sections is not None:
            for name, value in sorted(
                sections.items(), key=lambda item: (item[0].casefold(), item[0])
            ):
                _require_text(name, field_name="section name")
                _require_text(value, field_name=f"section {name!r}")
                section: SkillEvidenceSection = (
                    cast(SkillEvidenceSection, name.casefold())
                    if name.casefold() in _KNOWN_SECTION_NAMES
                    else "other"
                )
                candidates.append((section, value))
        return tuple(
            _TextSection(ordinal=ordinal, section=section, text=text)
            for ordinal, (section, text) in enumerate(candidates)
        )

    def _matches_for_section(self, text: str) -> tuple[_Match, ...]:
        matches: list[_Match] = []
        for skill in self._skills:
            skill_negative_matches = _pattern_matches(skill.negative_patterns, text)
            skill_matches: list[_Match] = []
            for alias in skill.aliases:
                rule = self._rules_by_alias.get(alias.casefold())
                positive_context_matches = (
                    _pattern_matches(rule.positive_context_patterns, text)
                    if rule is not None
                    else ()
                )
                negative_context_matches = (
                    _pattern_matches(rule.negative_patterns, text) if rule is not None else ()
                )
                for occurrence in _literal_occurrences(
                    alias, text, requires_word_boundary=skill.requires_word_boundary
                ):
                    if _overlaps_any(occurrence, skill_negative_matches):
                        continue
                    if rule is not None and not _ambiguous_occurrence_is_allowed(
                        occurrence,
                        positive_context_matches,
                        negative_context_matches,
                    ):
                        continue
                    skill_matches.append(
                        _Match(
                            skill=skill,
                            alias=alias,
                            start=occurrence.start(),
                            end=occurrence.end(),
                            matched_text=occurrence.group(0),
                        )
                    )
            matches.extend(_without_overlapping_aliases(skill_matches))
        return tuple(matches)


def _literal_occurrences(
    alias: str, text: str, *, requires_word_boundary: bool
) -> tuple[re.Match[str], ...]:
    literal = re.escape(alias)
    pattern = rf"(?<!\w){literal}(?!\w)" if requires_word_boundary else literal
    return tuple(re.finditer(pattern, text, flags=_REGEX_FLAGS))


def _without_overlapping_aliases(matches: list[_Match]) -> tuple[_Match, ...]:
    """Prefer the longest alias when aliases for one skill cover the same text."""
    selected: list[_Match] = []
    for candidate in sorted(
        matches,
        key=lambda match: (match.start, -len(match.matched_text), match.alias.casefold()),
    ):
        overlaps = any(
            candidate.start < existing.end and existing.start < candidate.end
            for existing in selected
        )
        if overlaps:
            continue
        selected.append(candidate)
    return tuple(selected)


def _ambiguous_occurrence_is_allowed(
    occurrence: re.Match[str],
    positive_context_matches: tuple[re.Match[str], ...],
    negative_context_matches: tuple[re.Match[str], ...],
) -> bool:
    return _overlaps_any(occurrence, positive_context_matches) and not _overlaps_any(
        occurrence, negative_context_matches
    )


def _pattern_matches(patterns: list[str], text: str) -> tuple[re.Match[str], ...]:
    return tuple(
        match for pattern in patterns for match in re.finditer(pattern, text, flags=_REGEX_FLAGS)
    )


def _overlaps_any(occurrence: re.Match[str], context_matches: tuple[re.Match[str], ...]) -> bool:
    return any(
        occurrence.start() < context.end() and context.start() < occurrence.end()
        for context in context_matches
    )


def _require_text(value: str, *, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
