from __future__ import annotations

import pytest

from career_os.radar.roles import (
    CLASSIFIER_VERSION,
    ClassificationStatus,
    EvidenceAxis,
    EvidenceSource,
    RoleContext,
    RoleFamily,
    RoleScope,
    Seniority,
    classify_role,
)

_CLOSED_HEADS = (
    "Test Driver",
    "Senior Recruiter",
    "Senior Partner Manager",
    "Strategic Account Executive",
    "AI Product Manager",
    "Product Manager II",
    "Product Counsel",
    "Staff Product Marketing Manager",
    "Marketing Analytics Director",
    "Head of SEO",
    "Head of Marketing",
    "Events Manager",
    "SMM&Comms Specialist",
    "Manager, Scaled Customer Success",
    "Senior Finance Analyst",
    "Senior Staff Accountant",
    "Business Development Manager",
)


def test_classifier_version_and_closed_nontechnical_heads_are_exact() -> None:
    assert CLASSIFIER_VERSION == "1.1.0"
    assert len(_CLOSED_HEADS) == 17
    for head in _CLOSED_HEADS:
        result = classify_role(head)
        scope_evidence = [item for item in result.evidence if item.axis is EvidenceAxis.SCOPE]
        assert result.status is ClassificationStatus.OUT_OF_SCOPE, head
        assert result.scope is RoleScope.OUT_OF_SCOPE, head
        assert result.role_family is None, head
        assert len(scope_evidence) == 1, head
        assert scope_evidence[0].rule_id == "closed_nontechnical_role_head", head
        assert scope_evidence[0].source is EvidenceSource.TITLE, head
        assert scope_evidence[0].matched_text == head, head


@pytest.mark.parametrize(
    "delimiter",
    [",", " -", " \u2013", " \u2014", " |", " ("],
)
def test_closed_head_accepts_only_explicit_title_delimiters(delimiter: str) -> None:
    title = f"Senior Recruiter{delimiter} Software Engineer Platform"
    result = classify_role(title)
    scope_evidence = [item for item in result.evidence if item.axis is EvidenceAxis.SCOPE]
    assert result.status is ClassificationStatus.OUT_OF_SCOPE
    assert [item.rule_id for item in scope_evidence] == ["closed_nontechnical_role_head"]
    assert scope_evidence[0].matched_text == "Senior Recruiter"
    assert RoleContext.PLATFORM in result.context
    assert not any(item.rule_id == "technical_role_head" for item in result.evidence)


def test_closed_head_preserves_case_whitespace_and_seniority_from_the_head() -> None:
    result = classify_role("  sEnIoR   ReCrUiTeR   | Platform Engineer  ")
    scope_evidence = next(item for item in result.evidence if item.axis is EvidenceAxis.SCOPE)
    seniority_evidence = next(
        item for item in result.evidence if item.axis is EvidenceAxis.SENIORITY
    )
    assert result.status is ClassificationStatus.OUT_OF_SCOPE
    assert scope_evidence.matched_text == "sEnIoR   ReCrUiTeR"
    assert result.seniority is Seniority.SENIOR
    assert seniority_evidence.matched_text == "sEnIoR"
    assert RoleContext.PLATFORM in result.context
    assert not any(item.rule_id == "technical_role_head" for item in result.evidence)
    assert result.managerial_function is None


def test_closed_nontechnical_seniority_keeps_existing_marker_conflicts() -> None:
    senior_partner_manager = classify_role("Senior Partner Manager")
    assert senior_partner_manager.seniority is None
    assert {
        item.matched_text
        for item in senior_partner_manager.evidence
        if item.rule_id == "conflicting_seniority_markers"
    } == {"Senior", "Manager"}

    staff_product_marketing_manager = classify_role("Staff Product Marketing Manager")
    assert staff_product_marketing_manager.seniority is None
    assert {
        item.matched_text
        for item in staff_product_marketing_manager.evidence
        if item.rule_id == "conflicting_seniority_markers"
    } == {"Staff", "Manager"}


def test_closed_nontechnical_head_precedes_trailing_engineering_manager_words() -> None:
    result = classify_role("Business Development Manager - Engineering Manager")
    assert result.status is ClassificationStatus.OUT_OF_SCOPE
    assert result.scope is RoleScope.OUT_OF_SCOPE
    assert result.seniority is Seniority.MANAGER
    assert result.managerial_function is None
    assert [item.rule_id for item in result.evidence if item.axis is EvidenceAxis.SCOPE] == [
        "closed_nontechnical_role_head"
    ]
    assert not any(item.axis is EvidenceAxis.MANAGERIAL_FUNCTION for item in result.evidence)


@pytest.mark.parametrize(
    "title",
    [
        "Test Driver Platform Engineer",
        "Test Driver: Platform Engineer",
        "Recruiter Senior",
        "Senior Manager Partner",
        "Staff Product Marketing Manager Engineer",
    ],
)
def test_closed_heads_do_not_match_without_exact_order_and_boundary(title: str) -> None:
    result = classify_role(title)
    assert not any(item.rule_id == "closed_nontechnical_role_head" for item in result.evidence)
    if title.startswith("Test Driver"):
        assert result.scope is RoleScope.IN_SCOPE
        assert result.status is ClassificationStatus.CLASSIFIED
        assert result.role_family is RoleFamily.PLATFORM_INFRASTRUCTURE_ENGINEERING
    elif title.startswith("Staff Product Marketing Manager"):
        assert result.status is ClassificationStatus.UNCLASSIFIED
    elif title == "Senior Manager Partner":
        assert result.status is ClassificationStatus.UNKNOWN_SCOPE


def test_full_stack_developer_hyphen_variant_preserves_family_and_evidence() -> None:
    result = classify_role("Full-Stack Developer (Node.js) | Website Builder")
    assert result.status is ClassificationStatus.CLASSIFIED
    assert result.role_family is RoleFamily.FULLSTACK_ENGINEERING
    assert RoleContext.FULLSTACK in result.context
    assert any(
        item.axis is EvidenceAxis.ROLE
        and item.rule_id == "fullstack_developer"
        and item.matched_text == "Full-Stack Developer"
        for item in result.evidence
    )


@pytest.mark.parametrize(
    ("title", "role_rule", "matched_role", "contexts"),
    [
        (
            "Manager, Software Engineering (Resilience Engineering)",
            "manager_software_engineering",
            "Manager, Software Engineering",
            set(),
        ),
        (
            "Engineer Manager - Platform",
            "engineer_manager",
            "Engineer Manager",
            {RoleContext.PLATFORM},
        ),
    ],
)
def test_engineering_management_order_variants_keep_family_and_manager_evidence(
    title: str,
    role_rule: str,
    matched_role: str,
    contexts: set[RoleContext],
) -> None:
    result = classify_role(title)
    assert result.status is ClassificationStatus.CLASSIFIED
    assert result.role_family is RoleFamily.ENGINEERING_MANAGEMENT
    assert result.seniority is Seniority.MANAGER
    assert result.managerial_function is True
    assert contexts <= set(result.context)
    assert any(
        item.axis is EvidenceAxis.ROLE
        and item.rule_id == role_rule
        and item.matched_text == matched_role
        for item in result.evidence
    )
    assert any(
        item.axis is EvidenceAxis.MANAGERIAL_FUNCTION
        and item.source is EvidenceSource.TITLE
        and item.matched_text == matched_role
        for item in result.evidence
    )


@pytest.mark.parametrize(
    ("title", "status", "scope"),
    [
        ("Technical Program Manager", ClassificationStatus.UNCLASSIFIED, RoleScope.IN_SCOPE),
        (
            "Senior Technical Program Manager",
            ClassificationStatus.UNCLASSIFIED,
            RoleScope.IN_SCOPE,
        ),
        ("Android Mobile Engineer", ClassificationStatus.UNCLASSIFIED, RoleScope.IN_SCOPE),
        ("Mobile Engineer", ClassificationStatus.UNCLASSIFIED, RoleScope.IN_SCOPE),
        ("Sales Engineer", ClassificationStatus.UNCLASSIFIED, RoleScope.IN_SCOPE),
        ("Senior Salesforce Engineer", ClassificationStatus.UNCLASSIFIED, RoleScope.IN_SCOPE),
        (
            "Senior Technical Product Marketing Manager",
            ClassificationStatus.UNCLASSIFIED,
            RoleScope.IN_SCOPE,
        ),
        (
            "Software Engineer, Marketing Platform",
            ClassificationStatus.UNCLASSIFIED,
            RoleScope.IN_SCOPE,
        ),
        (
            "Customer Success Architect, EMEA",
            ClassificationStatus.CONFLICT,
            RoleScope.IN_SCOPE,
        ),
        (
            "Customer Success Engineer, LATAM (Spanish, Portugese)",
            ClassificationStatus.CONFLICT,
            RoleScope.IN_SCOPE,
        ),
        ("Customer Success Architect, CEUR", ClassificationStatus.CONFLICT, RoleScope.IN_SCOPE),
        ("Customer Success Architect", ClassificationStatus.CONFLICT, RoleScope.IN_SCOPE),
    ],
)
def test_uncertain_and_neighboring_technical_heads_remain_unresolved(
    title: str, status: ClassificationStatus, scope: RoleScope
) -> None:
    result = classify_role(title)
    assert result.status is status
    assert result.scope is scope
    assert result.role_family is None
    assert not any(item.rule_id == "closed_nontechnical_role_head" for item in result.evidence)
