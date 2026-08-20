"""Strict, file-backed skill taxonomy and golden-corpus configuration."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from yaml.resolver import BaseResolver

from career_os.config.sources import ConfigurationError, _validation_message

NonBlankText = Annotated[str, Field(min_length=1)]
SkillCategory = Literal[
    "language",
    "framework",
    "database",
    "messaging",
    "cloud",
    "containers",
    "iac",
    "cicd",
    "observability",
    "security",
    "api",
    "testing",
    "data",
    "ml",
]
_SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SKILL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def _stable_skill_id(value: str) -> str:
    if not _SKILL_ID_PATTERN.fullmatch(value):
        raise ValueError("must be lowercase snake_case")
    return value


class _UniqueKeyYamlLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys at every nesting level."""


def _construct_unique_mapping(
    loader: _UniqueKeyYamlLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.YAMLError("duplicate mapping key")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyYamlLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


class SkillTaxonomySkill(BaseModel):
    """A stable, high-signal skill definition; IDs are never recycled."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str
    label: NonBlankText
    category: SkillCategory
    aliases: list[NonBlankText] = Field(min_length=1)
    ambiguous_aliases: list[NonBlankText] = Field(default_factory=list)
    negative_patterns: list[NonBlankText] = Field(default_factory=list)
    requires_word_boundary: bool = True

    @field_validator("id")
    @classmethod
    def stable_id(cls, value: str) -> str:
        return _stable_skill_id(value)

    @field_validator("negative_patterns")
    @classmethod
    def regexes_compile(cls, values: list[str]) -> list[str]:
        for value in values:
            try:
                re.compile(value)
            except re.error as error:
                raise ValueError("contains an invalid regex pattern") from error
        return values

    @model_validator(mode="after")
    def aliases_are_unique_after_normalization(self) -> SkillTaxonomySkill:
        duplicates = _duplicates(_normalized(value) for value in self.aliases)
        if duplicates:
            raise ValueError("contains duplicate aliases after normalization")
        ambiguous = {_normalized(value) for value in self.ambiguous_aliases}
        if len(ambiguous) != len(self.ambiguous_aliases):
            raise ValueError("contains duplicate ambiguous aliases after normalization")
        if not ambiguous <= {_normalized(value) for value in self.aliases}:
            raise ValueError("contains ambiguous aliases not owned by the skill")
        return self


class AmbiguousAliasRule(BaseModel):
    """Context that must accompany a deliberately ambiguous source alias."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    skill_id: str
    alias: NonBlankText
    positive_context_patterns: list[NonBlankText] = Field(min_length=1)
    negative_patterns: list[NonBlankText] = Field(default_factory=list)

    @field_validator("skill_id")
    @classmethod
    def stable_skill_id(cls, value: str) -> str:
        return _stable_skill_id(value)

    @field_validator("positive_context_patterns", "negative_patterns")
    @classmethod
    def regexes_compile(cls, values: list[str]) -> list[str]:
        for value in values:
            try:
                re.compile(value)
            except re.error as error:
                raise ValueError("contains an invalid regex pattern") from error
        return values


class SkillTaxonomy(BaseModel):
    """Versioned taxonomy plus explicit context rules for ambiguous aliases."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    version: str
    skills: list[SkillTaxonomySkill] = Field(min_length=1)
    ambiguous_alias_rules: list[AmbiguousAliasRule] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def semantic_version(cls, value: str) -> str:
        if not _SEMVER_PATTERN.fullmatch(value):
            raise ValueError("must be a semantic MAJOR.MINOR.PATCH version")
        return value

    @model_validator(mode="after")
    def cross_references_are_unambiguous(self) -> SkillTaxonomy:
        duplicate_ids = _duplicates(_normalized(skill.id) for skill in self.skills)
        if duplicate_ids:
            raise ValueError("contains duplicate skill IDs after normalization")
        alias_owners: dict[str, str] = {}
        for skill in self.skills:
            for alias in skill.aliases:
                normalized = _normalized(alias)
                owner = alias_owners.setdefault(normalized, skill.id)
                if owner != skill.id:
                    raise ValueError("contains duplicate aliases after normalization")

        skills_by_id = {skill.id: skill for skill in self.skills}
        ambiguous_alias_owners = {
            _normalized(alias): skill.id
            for skill in self.skills
            for alias in skill.ambiguous_aliases
        }
        rules_by_alias: dict[str, AmbiguousAliasRule] = {}
        for rule in self.ambiguous_alias_rules:
            alias = _normalized(rule.alias)
            if alias in rules_by_alias:
                raise ValueError("contains duplicate ambiguous alias rules")
            rule_skill = skills_by_id.get(rule.skill_id)
            if rule_skill is None:
                raise ValueError("contains a rule referencing an unknown skill")
            if ambiguous_alias_owners.get(alias) != rule.skill_id:
                raise ValueError("contains a rule for an undeclared or non-owned ambiguous alias")
            rules_by_alias[alias] = rule

        missing_rules = set(ambiguous_alias_owners) - set(rules_by_alias)
        if missing_rules:
            raise ValueError("contains ambiguous aliases without context rules")
        return self

    @property
    def categories(self) -> frozenset[str]:
        return frozenset(skill.category for skill in self.skills)


class GoldenExtractionCase(BaseModel):
    """One future extraction expectation, structurally validated in task 19."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: NonBlankText
    title: str | None = None
    description: str | None = None
    sections: dict[NonBlankText, NonBlankText] = Field(default_factory=dict)
    expected_skill_ids: list[NonBlankText] = Field(default_factory=list)
    forbidden_skill_ids: list[NonBlankText] = Field(default_factory=list)
    note: str | None = None

    @model_validator(mode="after")
    def meaningful_and_disjoint(self) -> GoldenExtractionCase:
        content = [self.title, self.description, *self.sections.values()]
        if not any(isinstance(value, str) and value.strip() for value in content):
            raise ValueError("requires at least one non-blank title, description, or section")
        expected = {_normalized(value) for value in self.expected_skill_ids}
        forbidden = {_normalized(value) for value in self.forbidden_skill_ids}
        if expected & forbidden:
            raise ValueError("contains overlapping expected and forbidden skill IDs")
        if not expected and not forbidden:
            raise ValueError("requires at least one expected or forbidden skill ID")
        if len(expected) != len(self.expected_skill_ids) or len(forbidden) != len(
            self.forbidden_skill_ids
        ):
            raise ValueError("contains duplicate skill IDs after normalization")
        return self

    @field_validator("expected_skill_ids", "forbidden_skill_ids")
    @classmethod
    def stable_skill_ids(cls, values: list[str]) -> list[str]:
        for value in values:
            _stable_skill_id(value)
        return values

    @property
    def evidence_text(self) -> str:
        values = [self.title, self.description, *self.sections.values()]
        return "\n".join(value for value in values if value)


class GoldenExtractionCorpus(BaseModel):
    """Typed golden cases that refer only to a supplied validated taxonomy."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    taxonomy_version: str
    cases: list[GoldenExtractionCase] = Field(min_length=1)

    @field_validator("taxonomy_version")
    @classmethod
    def semantic_version(cls, value: str) -> str:
        if not _SEMVER_PATTERN.fullmatch(value):
            raise ValueError("must be a semantic MAJOR.MINOR.PATCH version")
        return value

    @model_validator(mode="after")
    def unique_case_ids(self) -> GoldenExtractionCorpus:
        if _duplicates(_normalized(case.id) for case in self.cases):
            raise ValueError("contains duplicate case IDs after normalization")
        return self


def load_skill_taxonomy(path: str | Path) -> SkillTaxonomy:
    """Load and fully validate a UTF-8 YAML taxonomy without extraction I/O."""
    parsed = _load_yaml_mapping(path, subject="skill taxonomy")
    try:
        return SkillTaxonomy.model_validate(parsed)
    except ValidationError as error:
        raise ConfigurationError(_validation_message("skill taxonomy", error)) from error


def load_golden_extraction_corpus(
    path: str | Path, *, taxonomy: SkillTaxonomy
) -> GoldenExtractionCorpus:
    """Load corpus structure and validate all skill references and ambiguity coverage."""
    parsed = _load_yaml_mapping(path, subject="golden extraction corpus")
    try:
        corpus = GoldenExtractionCorpus.model_validate(parsed)
    except ValidationError as error:
        raise ConfigurationError(_validation_message("golden extraction corpus", error)) from error

    if corpus.taxonomy_version != taxonomy.version:
        raise ConfigurationError(
            "Invalid golden extraction corpus: taxonomy_version does not match the taxonomy"
        )
    skill_ids = {skill.id for skill in taxonomy.skills}
    for case in corpus.cases:
        unknown = set(case.expected_skill_ids + case.forbidden_skill_ids) - skill_ids
        if unknown:
            raise ConfigurationError(
                "Invalid golden extraction corpus: case skill references unknown taxonomy IDs"
            )
    _validate_corpus_coverage(corpus, taxonomy)
    return corpus


def _load_yaml_mapping(path: str | Path, *, subject: str) -> dict[object, object]:
    config_path = Path(path)
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ConfigurationError(
            f"{subject.capitalize()} file does not exist: {config_path}"
        ) from error
    except UnicodeDecodeError as error:
        raise ConfigurationError(
            f"{subject.capitalize()} file was not valid UTF-8: {config_path}"
        ) from error
    except OSError as error:
        raise ConfigurationError(f"Could not read {subject} file: {config_path}") from error
    try:
        parsed = yaml.load(raw, Loader=_UniqueKeyYamlLoader)
    except yaml.YAMLError as error:
        raise ConfigurationError(f"Malformed YAML in {subject}: {config_path}") from error
    if not isinstance(parsed, dict):
        raise ConfigurationError(
            f"{subject.capitalize()} must contain a mapping at the document root: {config_path}"
        )
    return parsed


def _validate_corpus_coverage(corpus: GoldenExtractionCorpus, taxonomy: SkillTaxonomy) -> None:
    expected = {value for case in corpus.cases for value in case.expected_skill_ids}
    forbidden = {value for case in corpus.cases for value in case.forbidden_skill_ids}
    if not expected or not forbidden:
        raise ConfigurationError(
            "Invalid golden extraction corpus: requires expected and forbidden examples"
        )
    skills_by_id = {skill.id: skill for skill in taxonomy.skills}
    rules_by_skill_id: dict[str, list[AmbiguousAliasRule]] = {}
    for rule in taxonomy.ambiguous_alias_rules:
        rules_by_skill_id.setdefault(rule.skill_id, []).append(rule)
    for case in corpus.cases:
        for skill_id in case.expected_skill_ids:
            ambiguous_rules = rules_by_skill_id.get(skill_id)
            if ambiguous_rules is not None:
                if not any(
                    _matches_any(rule.positive_context_patterns, case.evidence_text)
                    for rule in ambiguous_rules
                ):
                    raise ConfigurationError(
                        "Invalid golden extraction corpus: expected ambiguous skill has no "
                        "positive context in its case evidence"
                    )
                continue
            skill = skills_by_id[skill_id]
            if not any(_contains_alias(case.evidence_text, alias) for alias in skill.aliases):
                raise ConfigurationError(
                    "Invalid golden extraction corpus: expected non-ambiguous skill has no "
                    "declared literal alias in case evidence"
                )
    for rule in taxonomy.ambiguous_alias_rules:
        alias = _normalized(rule.alias)
        skill_id = rule.skill_id
        positive = any(
            skill_id in case.expected_skill_ids
            and _matches_any(rule.positive_context_patterns, case.evidence_text)
            for case in corpus.cases
        )
        negative = any(
            skill_id in case.forbidden_skill_ids
            and _contains_alias(case.evidence_text, alias)
            and (
                _matches_any(rule.negative_patterns, case.evidence_text)
                if rule.negative_patterns
                else not _matches_any(rule.positive_context_patterns, case.evidence_text)
            )
            for case in corpus.cases
        )
        if not positive or not negative:
            raise ConfigurationError(
                "Invalid golden extraction corpus: each ambiguous alias needs "
                "positive and forbidden context cases"
            )


def _contains_alias(text: str, alias: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text, flags=re.IGNORECASE) is not None


def _matches_any(patterns: list[str], text: str) -> bool:
    """Apply taxonomy context regexes case-insensitively to corpus evidence."""
    return any(re.search(pattern, text, flags=re.IGNORECASE) is not None for pattern in patterns)


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _duplicates(values: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates
