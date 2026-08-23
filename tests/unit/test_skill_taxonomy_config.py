"""Strict validation tests for the versioned taxonomy and golden corpus boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from career_os.config.skills import load_golden_extraction_corpus, load_skill_taxonomy
from career_os.config.sources import ConfigurationError


def test_checked_in_taxonomy_and_golden_corpus_are_valid() -> None:
    taxonomy = load_skill_taxonomy(_taxonomy_path())
    corpus = load_golden_extraction_corpus(_corpus_path(), taxonomy=taxonomy)

    assert taxonomy.version == "1.0.2"
    assert 100 <= len(taxonomy.skills) <= 200
    assert len(taxonomy.categories) >= 10
    assert len(corpus.cases) >= 9


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("version: 1\nskills: []\n", "version"),
        (
            "version: 1.0.2\nskills:\n"
            "  - {id: java, label: Java, category: language, aliases: [Java]}\n"
            "  - {id: java, label: Other, category: language, aliases: [Other]}\n",
            "skill IDs",
        ),
        (
            "version: 1.0.2\nskills:\n"
            "  - {id: java, label: Java, category: language, aliases: [Java]}\n"
            "  - {id: kotlin, label: Kotlin, category: language, aliases: [java]}\n",
            "aliases",
        ),
        (
            "version: 1.0.2\nskills:\n"
            "  - {id: java, label: Java, category: language, aliases: [Java], "
            "negative_patterns: ['(']}\n",
            "invalid regex pattern",
        ),
        (
            "version: 1.0.2\nskills:\n"
            "  - {id: go, label: Go, category: language, aliases: [Go], "
            "ambiguous_aliases: [Go]}\n",
            "ambiguous aliases",
        ),
    ],
)
def test_taxonomy_rejects_semver_duplicates_invalid_regex_and_missing_ambiguity_rule(
    tmp_path: Path, document: str, message: str
) -> None:
    path = tmp_path / "skills.yaml"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=message):
        load_skill_taxonomy(path)


def test_taxonomy_treats_aliases_as_literals_and_normalizes_unicode_for_duplicates(
    tmp_path: Path,
) -> None:
    literal = tmp_path / "literal.yaml"
    literal.write_text(
        "version: 1.0.2\nskills:\n"
        "  - {id: cpp, label: C++, category: language, aliases: ['C++', '(']}\n",
        encoding="utf-8",
    )
    assert load_skill_taxonomy(literal).skills[0].aliases == ["C++", "("]

    compatibility_duplicate = tmp_path / "compatibility.yaml"
    compatibility_duplicate.write_text(
        "version: 1.0.2\nskills:\n"
        "  - {id: java, label: Java, category: language, aliases: [Java]}\n"
        "  - {id: kotlin, label: Kotlin, category: language, aliases: ['\uff2aava']}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="duplicate aliases"):
        load_skill_taxonomy(compatibility_duplicate)


def test_taxonomy_owns_extensible_ambiguity_declarations_and_requires_exactly_one_rule(
    tmp_path: Path,
) -> None:
    valid = tmp_path / "future-ambiguity.yaml"
    valid.write_text(
        "version: 1.0.2\nskills:\n"
        "  - {id: java, label: Java, category: language, aliases: [Java], "
        "ambiguous_aliases: [Java]}\n"
        "ambiguous_alias_rules:\n"
        "  - {skill_id: java, alias: Java, positive_context_patterns: ['java developer']}\n",
        encoding="utf-8",
    )
    assert load_skill_taxonomy(valid).ambiguous_alias_rules[0].skill_id == "java"

    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text(
        "version: 1.0.2\nskills:\n"
        "  - {id: go, label: Go, category: language, aliases: [Go], "
        "ambiguous_aliases: [Go, go]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="duplicate ambiguous aliases"):
        load_skill_taxonomy(duplicate)

    non_owned = tmp_path / "non-owned.yaml"
    non_owned.write_text(
        "version: 1.0.2\nskills:\n"
        "  - {id: go, label: Go, category: language, aliases: [Go], "
        "ambiguous_aliases: [Java]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="not owned"):
        load_skill_taxonomy(non_owned)

    duplicate_rule = tmp_path / "duplicate-rule.yaml"
    duplicate_rule.write_text(
        "version: 1.0.2\nskills:\n"
        "  - {id: go, label: Go, category: language, aliases: [Go], "
        "ambiguous_aliases: [Go]}\n"
        "ambiguous_alias_rules:\n"
        "  - {skill_id: go, alias: Go, positive_context_patterns: ['Go developer']}\n"
        "  - {skill_id: go, alias: go, positive_context_patterns: ['Go language']}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="duplicate ambiguous alias rules"):
        load_skill_taxonomy(duplicate_rule)


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        (
            "  - {skill_id: missing, alias: Go, positive_context_patterns: ['Go developer']}\n",
            "unknown skill",
        ),
        (
            "  - {skill_id: java, alias: Go, positive_context_patterns: ['Go developer']}\n",
            "undeclared or non-owned",
        ),
        (
            "  - {skill_id: go, alias: Java, positive_context_patterns: ['Java developer']}\n",
            "undeclared or non-owned",
        ),
    ],
)
def test_taxonomy_rejects_invalid_ambiguous_rule_cross_references(
    tmp_path: Path, rule: str, message: str
) -> None:
    path = tmp_path / "skills.yaml"
    path.write_text(
        "version: 1.0.2\nskills:\n"
        "  - {id: go, label: Go, category: language, aliases: [Go], "
        "ambiguous_aliases: [Go]}\n"
        "  - {id: java, label: Java, category: language, aliases: [Java]}\n"
        "ambiguous_alias_rules:\n" + rule,
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match=message):
        load_skill_taxonomy(path)


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        (
            "  - {skill_id: go, alias: Go, positive_context_patterns: [' ']}\n",
            "positive_context_patterns.0",
        ),
        (
            "  - {skill_id: go, alias: Go, positive_context_patterns: ['(']}\n",
            "invalid regex pattern",
        ),
    ],
)
def test_taxonomy_rejects_blank_or_invalid_ambiguous_rule_context(
    tmp_path: Path, rule: str, message: str
) -> None:
    path = tmp_path / "invalid-rule.yaml"
    path.write_text(
        "version: 1.0.2\nskills:\n"
        "  - {id: go, label: Go, category: language, aliases: [Go], "
        "ambiguous_aliases: [Go]}\n"
        "ambiguous_alias_rules:\n" + rule,
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match=message):
        load_skill_taxonomy(path)


def test_taxonomy_loader_reports_read_yaml_root_and_unknown_key_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="does not exist"):
        load_skill_taxonomy(tmp_path / "missing.yaml")
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("skills: [", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Malformed YAML"):
        load_skill_taxonomy(malformed)
    unknown = tmp_path / "unknown.yaml"
    unknown.write_text("version: 1.0.2\nskills: []\nunknown: true\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="unknown"):
        load_skill_taxonomy(unknown)
    root = tmp_path / "root.yaml"
    root.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="mapping"):
        load_skill_taxonomy(root)
    invalid_utf8 = tmp_path / "invalid-utf8.yaml"
    invalid_utf8.write_bytes(b"\xff")
    with pytest.raises(ConfigurationError, match="UTF-8"):
        load_skill_taxonomy(invalid_utf8)


@pytest.mark.parametrize(
    ("skill", "message"),
    [
        ("{id: Java, label: Java, category: language, aliases: [Java]}", "skills.0.id"),
        ("{id: java, label: Java, category: generic, aliases: [Java]}", "skills.0.category"),
        (
            "{id: java, label: Java, category: language, aliases: [Java], unexpected: true}",
            "skills.0.unexpected",
        ),
        ("{id: java, label: Java, category: language, aliases: [' ']}", "skills.0.aliases.0"),
        (
            "{id: java, label: Java, category: language, aliases: [Java], "
            "ambiguous_aliases: [Java]}",
            "ambiguous aliases",
        ),
    ],
)
def test_taxonomy_rejects_nested_invalid_skill_values(
    tmp_path: Path, skill: str, message: str
) -> None:
    path = tmp_path / "invalid-skill.yaml"
    path.write_text(f"version: 1.0.2\nskills:\n  - {skill}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match=message):
        load_skill_taxonomy(path)


def test_golden_corpus_rejects_unknown_references_overlap_and_missing_ambiguity_coverage(
    tmp_path: Path,
) -> None:
    taxonomy = load_skill_taxonomy(_taxonomy_path())
    bad_reference = tmp_path / "unknown.yaml"
    bad_reference.write_text(
        "taxonomy_version: 1.0.2\ncases:\n"
        "  - {id: one, description: valid, expected_skill_ids: [missing], "
        "forbidden_skill_ids: [java]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="unknown taxonomy"):
        load_golden_extraction_corpus(bad_reference, taxonomy=taxonomy)

    overlap = tmp_path / "overlap.yaml"
    overlap.write_text(
        "taxonomy_version: 1.0.2\ncases:\n"
        "  - {id: one, description: valid, expected_skill_ids: [java], "
        "forbidden_skill_ids: [java]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="overlapping"):
        load_golden_extraction_corpus(overlap, taxonomy=taxonomy)

    no_context = tmp_path / "no-context.yaml"
    no_context.write_text(
        "taxonomy_version: 1.0.2\ncases:\n"
        "  - {id: one, description: Java, expected_skill_ids: [java], "
        "forbidden_skill_ids: [go]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="ambiguous alias"):
        load_golden_extraction_corpus(no_context, taxonomy=taxonomy)


def test_golden_corpus_requires_matching_taxonomy_version_exact_ids_and_assertions(
    tmp_path: Path,
) -> None:
    taxonomy = load_skill_taxonomy(_taxonomy_path())
    mismatch = tmp_path / "mismatch.yaml"
    mismatch.write_text(
        "taxonomy_version: 1.0.3\ncases:\n"
        "  - {id: one, description: Java, expected_skill_ids: [java]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="taxonomy_version"):
        load_golden_extraction_corpus(mismatch, taxonomy=taxonomy)

    uppercase_reference = tmp_path / "uppercase.yaml"
    uppercase_reference.write_text(
        "taxonomy_version: 1.0.2\ncases:\n"
        "  - {id: one, description: Java, expected_skill_ids: [JAVA]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="lowercase snake_case"):
        load_golden_extraction_corpus(uppercase_reference, taxonomy=taxonomy)

    no_assertion = tmp_path / "no-assertion.yaml"
    no_assertion.write_text(
        "taxonomy_version: 1.0.2\ncases:\n  - {id: one, description: Java}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="expected or forbidden"):
        load_golden_extraction_corpus(no_assertion, taxonomy=taxonomy)


def test_golden_corpus_rejects_duplicate_assertions_and_invalid_sections(tmp_path: Path) -> None:
    taxonomy = load_skill_taxonomy(_taxonomy_path())
    duplicate_expected = tmp_path / "duplicate-expected.yaml"
    duplicate_expected.write_text(
        "taxonomy_version: 1.0.2\ncases:\n"
        "  - {id: one, description: Java, expected_skill_ids: [java, java]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="duplicate skill IDs"):
        load_golden_extraction_corpus(duplicate_expected, taxonomy=taxonomy)

    blank_section = tmp_path / "blank-section.yaml"
    blank_section.write_text(
        "taxonomy_version: 1.0.2\ncases:\n"
        "  - {id: one, sections: {' ': Java}, expected_skill_ids: [java]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="sections"):
        load_golden_extraction_corpus(blank_section, taxonomy=taxonomy)

    duplicate_section = tmp_path / "duplicate-section.yaml"
    duplicate_section.write_text(
        "taxonomy_version: 1.0.2\ncases:\n"
        "  - id: one\n    sections:\n      requirements: Java\n      requirements: Kotlin\n"
        "    expected_skill_ids: [java]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="Malformed YAML"):
        load_golden_extraction_corpus(duplicate_section, taxonomy=taxonomy)


def test_checked_in_corpus_requires_literal_evidence_for_non_ambiguous_expected_skills(
    tmp_path: Path,
) -> None:
    taxonomy = load_skill_taxonomy(_taxonomy_path())
    incoherent = tmp_path / "incoherent.yaml"
    incoherent.write_text(
        _corpus_path()
        .read_text(encoding="utf-8")
        .replace("expected_skill_ids: [go, kafka, postgresql]", "expected_skill_ids: [go, kotlin]"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="declared literal alias"):
        load_golden_extraction_corpus(incoherent, taxonomy=taxonomy)


def test_each_expected_ambiguous_skill_requires_context_in_its_own_case(tmp_path: Path) -> None:
    taxonomy = load_skill_taxonomy(_taxonomy_path())
    incoherent = tmp_path / "unrelated-go.yaml"
    incoherent.write_text(
        _corpus_path().read_text(encoding="utf-8")
        + "  - id: unrelated_expected_go\n"
        + "    description: Build Java services.\n"
        + "    expected_skill_ids: [go]\n"
        + "    forbidden_skill_ids: [c]\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="expected ambiguous skill"):
        load_golden_extraction_corpus(incoherent, taxonomy=taxonomy)


def test_golden_corpus_rejects_read_yaml_root_unknown_key_and_duplicate_case_id(
    tmp_path: Path,
) -> None:
    taxonomy = load_skill_taxonomy(_taxonomy_path())
    with pytest.raises(ConfigurationError, match="does not exist"):
        load_golden_extraction_corpus(tmp_path / "missing.yaml", taxonomy=taxonomy)
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("taxonomy_version: 1.0.2\ncases: [", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Malformed YAML"):
        load_golden_extraction_corpus(malformed, taxonomy=taxonomy)
    root = tmp_path / "root.yaml"
    root.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="mapping"):
        load_golden_extraction_corpus(root, taxonomy=taxonomy)
    unknown = tmp_path / "unknown.yaml"
    unknown.write_text(
        "taxonomy_version: 1.0.2\ncases:\n  - {id: one, description: valid, unexpected: true}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="unexpected"):
        load_golden_extraction_corpus(unknown, taxonomy=taxonomy)
    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text(
        "taxonomy_version: 1.0.2\ncases:\n"
        "  - {id: one, description: Go developer, expected_skill_ids: [go]}\n"
        "  - {id: ONE, description: Go live, forbidden_skill_ids: [go]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="duplicate case IDs"):
        load_golden_extraction_corpus(duplicate, taxonomy=taxonomy)


def _taxonomy_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "taxonomy" / "skills.v1.yaml"


def _corpus_path() -> Path:
    return (
        Path(__file__).resolve().parents[1] / "fixtures" / "taxonomy" / "golden_extraction.v1.yaml"
    )
