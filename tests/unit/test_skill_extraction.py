"""Regression tests for deterministic taxonomy-driven skill extraction."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from career_os.config.skills import (
    SkillTaxonomy,
    load_golden_extraction_corpus,
    load_skill_taxonomy,
)
from career_os.domain.skills import SkillEvidence
from career_os.normalization.skills import SkillExtractor


def test_checked_in_golden_corpus_executes_with_exact_evidence() -> None:
    taxonomy = load_skill_taxonomy(_taxonomy_path())
    corpus = load_golden_extraction_corpus(_corpus_path(), taxonomy=taxonomy)
    extractor = SkillExtractor(taxonomy)

    for case in corpus.cases:
        evidence = extractor.extract(
            title=case.title,
            description=case.description,
            sections=case.sections,
        )
        actual_skill_ids = {item.skill_id for item in evidence}

        assert set(case.expected_skill_ids) <= actual_skill_ids, case.id
        assert not set(case.forbidden_skill_ids) & actual_skill_ids, case.id
        assert all(item.matched_text in case.evidence_text for item in evidence)
        assert all(item.taxonomy_version == corpus.taxonomy_version for item in evidence)


def test_extraction_is_literal_boundary_aware_and_deterministically_ordered() -> None:
    extractor = SkillExtractor(
        SkillTaxonomy.model_validate(
            {
                "version": "1.0.0",
                "skills": [
                    {
                        "id": "apache_kafka",
                        "label": "Apache Kafka",
                        "category": "messaging",
                        "aliases": ["Kafka", "Apache Kafka"],
                    },
                    {
                        "id": "javascript",
                        "label": "JavaScript",
                        "category": "language",
                        "aliases": ["JS"],
                    },
                    {
                        "id": "substring",
                        "label": "substring",
                        "category": "data",
                        "aliases": ["ml"],
                        "requires_word_boundary": False,
                    },
                ],
            }
        )
    )

    evidence = extractor.extract(
        title="Apache Kafka and Kafka",
        sections={"zeta": "JSON is unrelated; contains ml.", "Requirements": "Kafka"},
    )

    assert [(item.skill_id, item.matched_text, item.section) for item in evidence] == [
        ("apache_kafka", "Apache Kafka", "title"),
        ("apache_kafka", "Kafka", "title"),
        ("apache_kafka", "Kafka", "requirements"),
        ("substring", "ml", "other"),
    ]
    casefold_tie_first = extractor.extract(sections={"beta": "Kafka", "Beta": "Apache Kafka"})
    casefold_tie_reversed = extractor.extract(sections={"Beta": "Apache Kafka", "beta": "Kafka"})
    assert casefold_tie_first == casefold_tie_reversed
    assert [item.matched_text for item in casefold_tie_first] == ["Apache Kafka", "Kafka"]


def test_skill_and_ambiguous_negative_patterns_take_precedence_case_insensitively() -> None:
    extractor = SkillExtractor(
        SkillTaxonomy.model_validate(
            {
                "version": "1.0.0",
                "skills": [
                    {
                        "id": "python",
                        "label": "Python",
                        "category": "language",
                        "aliases": ["Python"],
                        "negative_patterns": ["python snake"],
                    },
                    {
                        "id": "go",
                        "label": "Go",
                        "category": "language",
                        "aliases": ["Go"],
                        "ambiguous_aliases": ["Go"],
                    },
                ],
                "ambiguous_alias_rules": [
                    {
                        "skill_id": "go",
                        "alias": "Go",
                        "positive_context_patterns": ["go developer"],
                        "negative_patterns": ["go live"],
                    }
                ],
            }
        )
    )

    assert extractor.extract(description="A PYTHON SNAKE is not the language.") == ()
    python_evidence = extractor.extract(
        description="A Python snake is an animal; Python is used for services."
    )
    assert [(item.skill_id, item.matched_text) for item in python_evidence] == [
        ("python", "Python")
    ]
    go_evidence = extractor.extract(description="GO DEVELOPER for distributed systems.")
    assert go_evidence[0].skill_id == "go"
    mixed_go_evidence = extractor.extract(description="Go to market, then work as a Go developer.")
    assert [(item.skill_id, item.matched_text) for item in mixed_go_evidence] == [("go", "Go")]


def test_checked_in_ambiguous_context_rules_admit_only_the_participating_occurrence() -> None:
    extractor = SkillExtractor(load_skill_taxonomy(_taxonomy_path()))

    mixed_r_evidence = extractor.extract(description="Partner with R&D; use R programming.")

    assert [(item.skill_id, item.matched_text) for item in mixed_r_evidence] == [("r", "R")]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"skill_id": "Python"}, "skill_id"),
        ({"matched_text": " "}, "matched_text"),
        ({"section": "summary"}, "section"),
        ({"method": "manual"}, "method"),
        ({"confidence": Decimal("1.001")}, "confidence"),
        ({"taxonomy_version": " "}, "taxonomy_version"),
    ],
)
def test_skill_evidence_rejects_invalid_domain_values(
    overrides: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "skill_id": "python",
        "matched_text": "Python",
        "section": "other",
        "method": "alias",
        "confidence": Decimal("1.000"),
        "taxonomy_version": "1.0.0",
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        SkillEvidence(**values)  # type: ignore[arg-type]


def _taxonomy_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "taxonomy" / "skills.v1.yaml"


def _corpus_path() -> Path:
    fixtures_path = Path(__file__).resolve().parents[1] / "fixtures" / "taxonomy"
    return fixtures_path / "golden_extraction.v1.yaml"
