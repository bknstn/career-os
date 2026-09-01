"""H2 Target Universe v1 contracts and no-inference boundary tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError
from referencing import Registry, Resource
from sqlalchemy.dialects import postgresql

from career_os.api.schemas import TargetUniverseReportResponse
from career_os.application.report import HybridResultBuilder, MarketReportService
from career_os.config.profiles import Profile
from career_os.config.skills import SkillTaxonomy
from career_os.config.target_universes import TargetUniverse, load_target_universe
from career_os.domain.hybrid_results import HybridResult, QualityReviewInput
from career_os.domain.markets import MarketAggregates
from career_os.domain.target_universe_results import (
    GATE_IDS,
    OBSERVATION_SCOPE,
    TargetUniverseResult,
)
from career_os.persistence.market_queries import build_market_job_ids_query
from career_os.persistence.report_queries import (
    ReportEvidenceSnapshot,
    ReportJobEvidence,
    ReportSourceEvidence,
)
from career_os.reporting.markdown import render_markdown_report

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 1, 8, tzinfo=UTC)


def test_template_is_strict_but_not_a_claimed_dogfood_universe() -> None:
    universe = load_target_universe(Path("config/target-universe.template.yaml"))

    assert universe.name == "example-review-required"
    assert universe.source_keys == (
        "affirm-greenhouse",
        "gitlab-greenhouse",
        "hostinger-ashby",
        "kestra-ashby",
    )
    assert universe.geography.remote_scopes == ("poland", "eu", "europe")
    assert universe.as_market_filters().company_names == ()
    assert universe.as_market_filters().title_exclude == ("frontend", "mobile", "qa")


@pytest.mark.parametrize(
    "value",
    [
        {
            "name": "not-a-panel",
            "role_families": ["backend_engineering"],
            "seniorities": ["senior"],
            "source_panel": [],
            "geography": {"country_codes": ["PL"]},
        },
        {
            "name": "unordered-panel",
            "role_families": ["backend_engineering"],
            "seniorities": ["senior"],
            "source_panel": [
                {"source_key": "beta", "company_name": "Beta"},
                {"source_key": "alpha", "company_name": "Alpha"},
            ],
            "geography": {"country_codes": ["PL"], "include_remote": True},
        },
        {
            "name": "implicit-remote-scope",
            "role_families": ["backend_engineering"],
            "seniorities": ["senior"],
            "source_panel": [{"source_key": "alpha", "company_name": "Alpha"}],
            "geography": {"country_codes": ["PL"], "remote_scopes": ["EU"]},
        },
    ],
)
def test_target_universe_rejects_implicit_or_ambiguous_boundaries(value: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TargetUniverse.model_validate(value)


def test_h2_result_exposes_universe_not_derived_market_filters() -> None:
    universe = _universe()
    result = TargetUniverseResult(
        target_universe=universe,
        evidence_result=_empty_evidence_result(universe),
    )

    payload = result.to_payload()
    response = TargetUniverseReportResponse.model_validate(payload)
    markdown = render_markdown_report(result)

    assert payload["contract_version"] == "h2.target-universe.v1"
    assert "filters" not in payload
    assert payload["scope"] == {
        "kind": "target_universe",
        "name": "backend-pl",
        "target_universe": universe.model_dump(mode="json"),
    }
    observations = payload["observations"]
    assert isinstance(observations, dict)
    assert observations["scope"] == OBSERVATION_SCOPE
    assert response.scope.target_universe == universe
    assert response.quality.claim_policy.target_universe_observations == "qualified"
    assert [gate["gate_id"] for gate in payload["quality"]["gates"]] == list(GATE_IDS)
    assert _schema_errors(payload) == []
    assert _schema_errors(response.model_dump(mode="json")) == []
    assert _schema_errors({**payload, "filters": {}})
    assert markdown.startswith("# Target Universe result: backend-pl\n")
    assert "## Bounded observations (target universe / configured source panel)" in markdown
    assert "## Exact target universe and profile metadata" in markdown
    assert "## Exact filters and profile metadata" not in markdown
    assert "fit score" in markdown


def test_complete_review_of_a_one_company_universe_is_accepted_without_sample_gates() -> None:
    universe = _universe()
    evidence = _reviewed_evidence_result(universe)
    result = TargetUniverseResult(target_universe=universe, evidence_result=evidence)

    assert evidence.quality.state == "evidence_limited"  # H1's internal sample policy.
    assert result.quality.state == "accepted"
    assert result.quality.failed_gates == ()
    assert result.quality.claim_policy["target_universe_observations"] == "bounded"
    assert "represented_companies" not in {gate.gate_id for gate in result.quality.gates}
    assert "max_company_concentration" not in {gate.gate_id for gate in result.quality.gates}
    assert result.quality.observed_values["matching_opportunity_count"] == 1
    assert _schema_errors(result.to_payload()) == []
    assert "Target-universe observations: **bounded**" in render_markdown_report(result)


def test_target_company_display_name_and_source_key_are_preserved_for_transport() -> None:
    universe = TargetUniverse.model_validate(
        {
            "name": "display-preservation",
            "role_families": ["backend_engineering"],
            "seniorities": ["senior"],
            "source_panel": [{"source_key": "Acme-ATS", "company_name": "Acme Labs"}],
            "geography": {"country_codes": ["PL"]},
        }
    )

    assert universe.source_keys == ("Acme-ATS",)
    assert universe.source_companies == {"Acme-ATS": "Acme Labs"}
    assert universe.normalized_source_companies == {"Acme-ATS": "acme labs"}
    assert universe.model_dump(mode="json")["source_panel"] == [
        {"source_key": "Acme-ATS", "company_name": "Acme Labs"}
    ]

    with pytest.raises(ValueError, match="enabled configured source keys"):
        MarketReportService._validate_target_universe_panel(
            universe,
            ReportEvidenceSnapshot(
                configured_sources=(
                    ReportSourceEvidence("acme-ats", "greenhouse", "Acme Labs", True),
                ),
                jobs=(),
            ),
        )


def test_result_rejects_a_source_panel_that_does_not_match_its_explicit_universe() -> None:
    universe = _universe()
    evidence = _empty_evidence_result(universe)
    changed_snapshot = ReportEvidenceSnapshot(
        configured_sources=(ReportSourceEvidence("other", "greenhouse", "Other", True),),
        jobs=(),
    )
    changed = HybridResultBuilder(_taxonomy()).build(
        filters=universe.as_market_filters(),
        profile=_profile(),
        market=_empty_market(universe),
        evidence=changed_snapshot,
    )

    with pytest.raises(ValueError, match="configured source panel"):
        TargetUniverseResult(target_universe=universe, evidence_result=changed)
    assert evidence.observations.sample.source_coverage.configured_source_keys == ("alpha",)


def test_target_universe_resolution_requires_an_enabled_matching_source_identity() -> None:
    universe = _universe()

    with pytest.raises(ValueError, match="enabled configured source keys"):
        MarketReportService._validate_target_universe_panel(
            universe,
            ReportEvidenceSnapshot(
                configured_sources=(ReportSourceEvidence("alpha", "greenhouse", "Alpha", False),),
                jobs=(),
            ),
        )
    with pytest.raises(ValueError, match="company_name"):
        MarketReportService._validate_target_universe_panel(
            universe,
            ReportEvidenceSnapshot(
                configured_sources=(
                    ReportSourceEvidence("alpha", "greenhouse", "Different", True),
                ),
                jobs=(),
            ),
        )


def test_target_universe_query_uses_exact_source_keys_without_a_join() -> None:
    sql = str(
        build_market_job_ids_query(_universe().as_market_filters(), source_keys=("alpha",)).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert " JOIN " not in sql
    assert "sources.key IN ('alpha')" in sql
    assert "frontend" in sql


def _universe() -> TargetUniverse:
    return TargetUniverse.model_validate(
        {
            "name": "backend-pl",
            "role_families": ["backend_engineering"],
            "seniorities": ["senior"],
            "source_panel": [{"source_key": "alpha", "company_name": "Alpha"}],
            "geography": {
                "country_codes": ["PL"],
                "include_remote": True,
                "remote_scopes": ["EU"],
            },
            "exclusions": {"title_contains": ["frontend"]},
        }
    )


def _profile() -> Profile:
    return Profile(
        name="current-backend",
        target_role_families=("backend_engineering",),
        seniority="senior",
        skills=(),
    )


def _taxonomy() -> SkillTaxonomy:
    return SkillTaxonomy.model_validate(
        {
            "version": "1.0.0",
            "skills": [
                {
                    "id": "python",
                    "label": "Python",
                    "category": "language",
                    "aliases": ["Python"],
                }
            ],
        }
    )


def _empty_market(universe: TargetUniverse) -> MarketAggregates:
    return MarketAggregates(
        market_name=universe.name,
        matching_job_ids=(),
        skill_denominator_job_ids=(),
        compensation_job_ids=(),
        collection_cutoff=None,
        taxonomy_version="1.0.0",
        normalizer_version="job-normalization-v5",
        companies=(),
        skills=(),
        seniorities=(),
        workplace_types=(),
        countries=(),
        compensation=(),
    )


def _empty_evidence_result(universe: TargetUniverse) -> HybridResult:
    return HybridResultBuilder(_taxonomy()).build(
        filters=universe.as_market_filters(),
        profile=_profile(),
        market=_empty_market(universe),
        evidence=ReportEvidenceSnapshot(
            configured_sources=(ReportSourceEvidence("alpha", "greenhouse", "Alpha", True),),
            jobs=(),
        ),
    )


def _reviewed_evidence_result(universe: TargetUniverse) -> HybridResult:
    job_id = UUID(int=1)
    market = MarketAggregates(
        market_name=universe.name,
        matching_job_ids=(job_id,),
        skill_denominator_job_ids=(),
        compensation_job_ids=(),
        collection_cutoff=NOW,
        taxonomy_version="1.0.0",
        normalizer_version="job-normalization-v5",
        companies=(),
        skills=(),
        seniorities=(),
        workplace_types=(),
        countries=(),
        compensation=(),
    )
    return HybridResultBuilder(_taxonomy()).build(
        filters=universe.as_market_filters(),
        profile=_profile(),
        market=market,
        evidence=ReportEvidenceSnapshot(
            configured_sources=(ReportSourceEvidence("alpha", "greenhouse", "Alpha", True),),
            jobs=(
                ReportJobEvidence(
                    job_id=job_id,
                    company_name="Alpha",
                    title="Senior Backend Engineer",
                    source_url="https://jobs.example.test/alpha/one",
                    source_key="alpha",
                    source_kind="greenhouse",
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                    role_family="backend_engineering",
                    seniority="senior",
                    workplace_type="onsite",
                    description_text="",
                ),
            ),
        ),
        quality_review=QualityReviewInput(ref="review.md#one", reviewed_job_ids=(job_id,)),
    )


def _schema_errors(payload: dict[str, object]) -> list[object]:
    h1 = json.loads(
        (PROJECT_ROOT / "docs/h1-hybrid-result-contract.schema.json").read_text(encoding="utf-8")
    )
    h2 = json.loads(
        (PROJECT_ROOT / "docs/h2-target-universe-result-contract.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(h2)
    registry = Registry().with_resources(
        (
            (h1["$id"], Resource.from_contents(h1)),
            (h2["$id"], Resource.from_contents(h2)),
        )
    )
    return list(
        Draft202012Validator(h2, registry=registry, format_checker=FormatChecker()).iter_errors(
            payload
        )
    )
