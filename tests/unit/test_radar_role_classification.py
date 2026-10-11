from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from career_os.radar.roles import (
    CLASSIFIER_VERSION,
    FAMILIES,
    ROLE_TAXONOMY,
    ClassificationStatus,
    EvidenceAxis,
    EvidenceSource,
    RoleContext,
    RoleFamily,
    RoleScope,
    RoleTaxonomy,
    Seniority,
    classify_role,
)

_CORPUS_PATH = Path(__file__).parents[1] / "fixtures/radar/golden_role_classification.v1.yaml"


def _corpus() -> dict[str, object]:
    parsed = yaml.safe_load(_CORPUS_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _case_results() -> list[tuple[dict[str, object], object]]:
    corpus = _corpus()
    cases = corpus["cases"]
    assert isinstance(cases, list)
    return [(case, classify_role(case["title"], case.get("description"))) for case in cases]


def test_versioned_golden_corpus_has_provenance_and_expected_labels() -> None:
    corpus = _corpus()
    assert set(corpus) == {"taxonomy_version", "cases"}
    assert corpus["taxonomy_version"] == ROLE_TAXONOMY.version
    cases = corpus["cases"]
    assert isinstance(cases, list)
    assert len(cases) == len({case["id"] for case in cases})

    retained = [case for case in cases if case["origin"] == "retained_db"]
    synthetic = [case for case in cases if case["origin"] == "synthetic"]
    assert len(retained) == 7
    assert synthetic
    for case in retained:
        assert set(case) == {"id", "origin", "provenance", "title", "expected"}
        provenance = case["provenance"]
        assert set(provenance) == {"source_key", "job_id"}
        assert isinstance(provenance["source_key"], str) and provenance["source_key"]
        UUID(provenance["job_id"])
    for case in synthetic:
        assert set(case) == {"id", "origin", "title", "expected"} | (
            {"description"} if "description" in case else set()
        )
        assert "provenance" not in case

    expected_families = {
        family for case in cases if (family := case["expected"]["family"]) is not None
    }
    assert expected_families == {family.value for family in RoleFamily}

    for case, result in _case_results():
        expected = case["expected"]
        assert result.classifier_version == CLASSIFIER_VERSION, case["id"]
        assert result.taxonomy_version == corpus["taxonomy_version"], case["id"]
        assert result.status.value == expected["status"], case["id"]
        assert result.scope.value == expected["scope"], case["id"]
        assert (result.role_family.value if result.role_family else None) == expected["family"], (
            case["id"]
        )
        assert (result.seniority.value if result.seniority else None) == expected["seniority"], (
            case["id"]
        )
        assert result.managerial_function is expected["managerial_function"], case["id"]
        assert [value.value for value in result.context] == expected["context"], case["id"]


def test_taxonomy_validation_freezes_exactly_eleven_r1_families() -> None:
    assert isinstance(ROLE_TAXONOMY.families, tuple)
    assert len(FAMILIES) == 11
    assert {definition.id for definition in FAMILIES} == set(RoleFamily)

    with pytest.raises(ValueError, match="immutable tuple"):
        RoleTaxonomy(version="1.0.0", families=list(FAMILIES))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"MAJOR.MINOR.PATCH"):
        RoleTaxonomy(version="v1", families=FAMILIES)
    with pytest.raises(ValueError, match="unique"):
        RoleTaxonomy(version="1.0.1", families=(*FAMILIES, FAMILIES[0]))
    with pytest.raises(ValueError, match="eleven"):
        RoleTaxonomy(version="1.0.1", families=FAMILIES[:-1])

    next_taxonomy = replace(ROLE_TAXONOMY, version="2.0.0")
    result = classify_role("Backend Engineer", taxonomy=next_taxonomy)
    assert result.taxonomy_version == "2.0.0"


def test_role_family_precedence_and_context_are_separate_axes() -> None:
    backend = classify_role("Senior Backend Engineer, AI Platform")
    assert backend.role_family is RoleFamily.BACKEND_ENGINEERING
    assert backend.seniority is Seniority.SENIOR
    assert set(backend.context) == {RoleContext.AI, RoleContext.BACKEND, RoleContext.PLATFORM}
    assert any(
        item.axis is EvidenceAxis.ROLE
        and item.source is EvidenceSource.TITLE
        and item.rule_id == "backend_engineer"
        and item.matched_text == "Backend Engineer"
        for item in backend.evidence
    )

    platform = classify_role("Staff Software Engineer, ML Platform")
    assert platform.role_family is RoleFamily.PLATFORM_INFRASTRUCTURE_ENGINEERING
    assert platform.seniority is Seniority.STAFF
    assert set(platform.context) == {RoleContext.MACHINE_LEARNING, RoleContext.PLATFORM}
    assert any(item.rule_id == "platform_context_specialty" for item in platform.evidence)

    assert classify_role("ML Engineer").role_family is RoleFamily.MACHINE_LEARNING_ENGINEERING
    assert classify_role("AI Engineer").role_family is RoleFamily.AI_ENGINEERING
    assert classify_role("AI Product Engineer").role_family is RoleFamily.AI_PRODUCT_ENGINEERING
    assert classify_role("Product Engineer").role_family is RoleFamily.PRODUCT_ENGINEERING
    assert classify_role("Product Engineer (Backend)").role_family is RoleFamily.BACKEND_ENGINEERING
    assert classify_role("Solutions Engineer").role_family is RoleFamily.SOLUTIONS_ARCHITECTURE


def test_unknown_scope_out_of_scope_and_technical_unclassified_are_distinct() -> None:
    assert classify_role("Analyst").status is ClassificationStatus.UNKNOWN_SCOPE
    assert classify_role("Product Manager").status is ClassificationStatus.OUT_OF_SCOPE
    support = classify_role("AI Technical Support Specialist")
    assert support.status is ClassificationStatus.UNCLASSIFIED
    assert support.scope is RoleScope.IN_SCOPE
    assert support.role_family is None
    assert RoleContext.AI in support.context
    assert classify_role("Sales Engineer").role_bucket == "unclassified"


def test_conflicts_and_ambiguous_titles_keep_evidence_without_a_family() -> None:
    compound = classify_role("AI Product Engineer / ML Engineer")
    assert compound.status is ClassificationStatus.AMBIGUOUS
    assert compound.role_family is None
    assert {item.rule_id for item in compound.evidence if item.axis is EvidenceAxis.ROLE} >= {
        "ai_product_engineer",
        "machine_learning_engineer",
    }

    disjunction = classify_role("Backend and Frontend Engineer")
    assert disjunction.status is ClassificationStatus.AMBIGUOUS
    assert disjunction.role_family is None
    assert any(item.rule_id == "cross_family_disjunction" for item in disjunction.evidence)

    conflict = classify_role(
        "Backend Engineer",
        "Responsibilities:\n• Deploy machine learning models.",
    )
    assert conflict.status is ClassificationStatus.CONFLICT
    assert conflict.role_family is None
    assert {item.source for item in conflict.evidence if item.axis is EvidenceAxis.ROLE} == {
        EvidenceSource.TITLE,
        EvidenceSource.DESCRIPTION_DUTY,
    }


def test_seniority_conflict_is_unknown_and_staff_principal_lead_are_not_families() -> None:
    mixed = classify_role("Mid/Senior Backend Engineer")
    assert mixed.role_family is RoleFamily.BACKEND_ENGINEERING
    assert mixed.seniority is None
    assert sum(item.rule_id == "conflicting_seniority_markers" for item in mixed.evidence) == 2

    staff_principal = classify_role("Staff Principal Engineer")
    assert staff_principal.role_family is None
    assert staff_principal.status is ClassificationStatus.UNCLASSIFIED
    assert staff_principal.seniority is None
    assert classify_role("Tech Lead").seniority is Seniority.LEAD
    assert classify_role("Tech Lead").managerial_function is None


def test_description_fallback_uses_direct_duties_and_rejects_marketing_or_negation() -> None:
    direct = classify_role(
        "Software Engineer",
        "Responsibilities:\n• Build backend systems and APIs.",
    )
    assert direct.role_family is RoleFamily.BACKEND_ENGINEERING
    assert any(item.source is EvidenceSource.DESCRIPTION_DUTY for item in direct.evidence)

    requirements = classify_role(
        "Software Engineer",
        "Requirements:\n• Experience building backend systems.\n"
        "Company: We build AI and ML tools for customers.",
    )
    assert requirements.status is ClassificationStatus.UNCLASSIFIED
    assert requirements.role_family is None

    marketing = classify_role(
        "Software Engineer",
        "Responsibilities:\n• Design marketing pages about AI.",
    )
    assert marketing.status is ClassificationStatus.UNCLASSIFIED
    assert marketing.role_family is None

    negated = classify_role(
        "Software Engineer",
        "Responsibilities:\n• Do not build backend systems.",
    )
    assert negated.status is ClassificationStatus.UNCLASSIFIED
    assert negated.role_family is None


def test_managerial_function_requires_title_or_direct_positive_duty() -> None:
    manager_title = classify_role("Engineering Manager, Product Engineering")
    assert manager_title.managerial_function is True

    assigned = classify_role(
        "Software Engineer",
        "Responsibilities:\n• Manage a team of engineers.",
    )
    assert assigned.managerial_function is True
    assert any(
        item.axis is EvidenceAxis.MANAGERIAL_FUNCTION
        and item.source is EvidenceSource.DESCRIPTION_DUTY
        for item in assigned.evidence
    )

    no_management_assignment = classify_role(
        "Software Engineer",
        "Responsibilities:\n"
        "• Experience in people management is helpful.\n"
        "• Support managers who manage a team.\n"
        "• Mentor engineers.\n"
        "Team: Our managers lead teams across the company.",
    )
    assert no_management_assignment.managerial_function is None

    negative = classify_role(
        "Software Engineer",
        "Responsibilities:\n• Do not manage a team or direct reports.",
    )
    assert negative.managerial_function is None


def test_result_is_frozen_and_rejects_incoherent_status_scope() -> None:
    result = classify_role("Backend Engineer")
    with pytest.raises(FrozenInstanceError):
        result.role_family = None  # type: ignore[misc]
    with pytest.raises(ValueError, match="status and scope"):
        replace(result, scope=RoleScope.OUT_OF_SCOPE)
    with pytest.raises(ValueError, match="status and scope"):
        replace(
            classify_role("Analyst"),
            status=ClassificationStatus.AMBIGUOUS,
            scope=RoleScope.OUT_OF_SCOPE,
        )
