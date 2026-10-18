"""Occupation gaps, adversarial title boundaries, and preserved B decisions."""

import pytest

from career_os.radar.mvp_roles import classify_mvp_role
from career_os.radar.role_contract import adapt_classification
from career_os.radar.roles import classify_role


@pytest.mark.parametrize(
    ("title", "occupation"),
    [
        ("Autonomous GSE Operator", "nontechnical"),
        ("Junior HR Specialist", "nontechnical"),
        ("Senior Professional Services Technical Architect - Security", "solutions_architecture"),
        ("Director, Strategic Partnerships", "nontechnical"),
        ("Major Account Executive, UK Public Sector", "nontechnical"),
        ("Sales Development Representative", "nontechnical"),
        ("Head of Product Growth", "nontechnical"),
        ("Creative Marketing Manager", "nontechnical"),
        ("Senior Product Manager, Agentic Source Code", "nontechnical"),
        ("Tax Accountant", "nontechnical"),
        ("Principal/Staff Software Engineer", "technical_engineering"),
        ("Senior Staff Software Engineer, Perception", "technical_engineering"),
        ("Senior / Staff Electrical Engineer, Autonomous Vehicles", "technical_engineering"),
        ("Staff CIAM Software Engineer", "technical_engineering"),
        ("Senior Go Engineer (Connectivity)", "technical_engineering"),
        ("Senior Technical Recruiter, Robotics", "nontechnical"),
        ("Staff Product Manager, AI Software Factory", "nontechnical"),
        ("Senior Technical Product Manager \u2013 Human Biology", "nontechnical"),
        ("Professional Services - Technical Architect", "solutions_architecture"),
        ("Senior Cloud Security Engineer", "technical_engineering"),
        ("Automation QA Engineer (Java)", "technical_engineering"),
        ("iOS Developer", "technical_engineering"),
        ("Linux System Administrator | Infrastructure Monitoring", "technical_engineering"),
        ("Senior Finance Manager", "nontechnical"),
        ("Senior Commercial & Technology Legal Counsel", "nontechnical"),
        ("Senior Customer Success Manager - German Speaking (Remote)", "nontechnical"),
        # Compositional cases absent from the retained title inventory.
        ("Principal HR Business Partner | France", "nontechnical"),
        ("Sr. Tax Accountant (Canada)", "nontechnical"),
        ("Junior/Intermediate Java Developer — Payments", "technical_engineering"),
        ("Staff Motion Designer II", "nontechnical"),
        ("Regional Business Development Representative | APAC", "nontechnical"),
        ("Senior Manager, Technical Recruiting", "nontechnical"),
        ("Manager, Fraud Operations", "nontechnical"),
        ("Director, Sales Strategy and Operations", "nontechnical"),
        ("Manager of Customer Success", "nontechnical"),
        ("Senior AWS DevOps", "technical_engineering"),
        ("Principal Data Architect", "technical_engineering"),
        ("GCP Data Architect", "technical_engineering"),
        ("Full-stack Engineer (Python + GCP)", "technical_engineering"),
        ("Head of Forward Deployed Engineering", "engineering_management"),
        ("Director, Engineering, Platform Operations", "engineering_management"),
    ],
)
def test_closed_role_heads_assign_broad_occupation_with_original_evidence(
    title: str, occupation: str
) -> None:
    result = classify_mvp_role(title)
    assert result.occupation == occupation
    evidence = [item for item in result.evidence if item.rule_id.startswith("mvp_")]
    assert evidence
    assert all(item.source == "title" and item.matched_text in title for item in evidence)
    assert result.specialty is None
    if occupation == "technical_engineering":
        assert result.specialty_state == "missing"


@pytest.mark.parametrize(
    "title",
    [
        "Analyst",
        "Senior Manager",
        "Head of Data",
        "Data Scientist",
        "Director of Data",
        "Apply for the future",
        "GTM (German-speaking) Talent Pool",
        "Senior HR Specialist (Talent Pool)",
        "Tax Accountant General Application",
        "Chief Executive Officer [B2B SaaS] - Talent Pool",
        "Manager, Technical Program Management",
        "Manager of Customer Success Engineering",
        "Staff Software Engineerer",
        "Java Developer Relations Specialist",
        "Product Managerial Assistant",
        "HR Specialist Engineer",
        "Finance Manager Engineer",
        "Sales Engineer",
        "Technical Account Manager",
        "Senior Technical Program Manager",
        "Support Engineer",
        "Customer Success Architect",
    ],
)
def test_unestablished_or_conflicting_roles_stay_unknown(title: str) -> None:
    assert classify_mvp_role(title).occupation == "unknown"


@pytest.mark.parametrize(
    "title",
    [
        "Backend Engineer",
        "Frontend Engineer",
        "Product Engineer",
        "Engineering Manager",
        "Solutions Architect",
        "Product Manager",
        "Software Engineer",
        "Support Engineer",
        "Customer Success Engineer",
        "Backend / Data Engineer",
        "Developer Advocate",
    ],
)
def test_existing_decisions_and_conflicts_are_preserved(title: str) -> None:
    description = "Responsibilities\n- Build data pipelines.\n- Build backend services."
    assert classify_mvp_role(title, description) == adapt_classification(
        title, classify_role(title, description)
    )


def test_company_copy_cannot_establish_a_missing_occupation() -> None:
    description = "We build software in Python and Go. Our engineering team uses Kubernetes."
    assert classify_mvp_role("Join our team", description).occupation == "unknown"
    assert classify_mvp_role("Junior HR Specialist", description).occupation == "nontechnical"


def test_programming_language_does_not_establish_backend_specialty() -> None:
    result = classify_mvp_role("Senior Go Engineer")
    assert result.occupation == "technical_engineering"
    assert result.specialty is None
    assert result.reason == "no_family_evidence"
