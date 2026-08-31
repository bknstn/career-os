"""Focused runtime coverage for the H1 hybrid result boundary."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from career_os.api.app import _report_response
from career_os.application.report import build_hybrid_result
from career_os.config.markets import MarketFilters
from career_os.config.profiles import Profile
from career_os.config.skills import SkillTaxonomy
from career_os.domain.hybrid_results import HybridResult, QualityReviewInput
from career_os.domain.markets import CompensationAggregate, MarketAggregates
from career_os.persistence.report_queries import (
    ReportCompensationEvidence,
    ReportEvidenceSnapshot,
    ReportJobEvidence,
    ReportLocationEvidence,
    ReportSkillEvidence,
    ReportSourceEvidence,
)
from career_os.reporting.markdown import render_markdown_report

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 29, 8, tzinfo=UTC)


def test_no_review_keeps_opportunities_and_emits_schema_valid_limited_result() -> None:
    result = _result(_jobs(2), configured_sources=("alpha", "beta", "unused"))

    assert result.opportunities.count == 2
    assert [gate.status for gate in result.quality.gates[:2]] == [
        "not_evaluable",
        "failed",
    ]
    assert result.quality.state == "evidence_limited"
    assert result.quality.claim_policy["broad_market_claims"] == "disabled"
    with pytest.raises(TypeError):
        result.quality.observed_values["matching_job_count"] = 99  # type: ignore[index]

    schema = json.loads(
        (PROJECT_ROOT / "docs/h1-hybrid-result-contract.schema.json").read_text(encoding="utf-8")
    )
    errors = list(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            result.to_payload()
        )
    )
    assert errors == [], [(list(error.path), error.message) for error in errors]
    response = _report_response(result)
    response_payload = response.model_dump(mode="json")
    response_errors = list(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(response_payload)
    )
    assert response_errors == [], [(list(error.path), error.message) for error in response_errors]
    assert response_payload["observations"]["sample"]["source_coverage"]["source_share"] == 2 / 3


def test_reviewed_twenty_jobs_can_pass_all_gates_without_broad_claims() -> None:
    jobs = _jobs(20, company_count=5)
    matching = tuple(job.job_id for job in jobs)
    result = _result(
        jobs,
        configured_sources=tuple(f"company-{index}" for index in range(5)),
        review=QualityReviewInput(ref="review.md#rows", reviewed_job_ids=matching),
    )

    assert result.quality.state == "accepted"
    assert result.quality.failed_gates == ()
    assert result.quality.claim_policy == {
        "opportunities": "available",
        "panel_observations": "panel_bounded",
        "broad_market_claims": "disabled",
        "skill_semantics": "mentions_only",
        "fit_probability": "not_provided",
    }


def test_d9_like_reviewed_sample_keeps_all_jobs_when_concentration_fails() -> None:
    jobs = tuple(
        replace(
            job,
            company_name="Affirm" if index < 11 else f"Other {index}",
            source_key="affirm" if index < 11 else f"other-{index}",
        )
        for index, job in enumerate(_jobs(26, company_count=11))
    )
    matching = tuple(job.job_id for job in jobs)
    configured_sources = tuple(sorted({job.source_key for job in jobs}))
    result = _result(
        jobs,
        configured_sources=configured_sources,
        review=QualityReviewInput(ref="review.md#d9", reviewed_job_ids=matching),
    )

    assert result.opportunities.count == 26
    assert result.quality.state == "evidence_limited"
    assert result.quality.failed_gates == ("max_company_concentration",)
    concentration = result.observations.sample.max_company_concentration
    assert concentration is not None
    assert concentration.company_name == "Affirm"
    assert concentration.job_count == 11
    assert concentration.job_share == Decimal(11) / Decimal(26)


def test_skill_observation_reports_distinct_employer_breadth() -> None:
    jobs = _jobs(3)
    jobs = (jobs[0], replace(jobs[1], skill_mentions=()), jobs[2])
    result = _result(jobs, configured_sources=("alpha", "beta"))

    python = result.observations.skills[0]
    assert python.job_count == 2
    assert python.company_names == ("Alpha",)
    assert python.company_count == 1
    assert python.denominator_company_names == ("Alpha", "Beta")
    assert python.company_share == Decimal("0.5")


def test_unknown_compensation_intervals_remain_separate_per_job() -> None:
    jobs = _jobs(2, unknown_interval=True)
    result = _result(jobs, configured_sources=("alpha", "beta"))

    groups = result.observations.compensation
    assert len(groups) == 2
    assert all(group.interval == "unknown" for group in groups)
    assert all(group.job_count == 1 for group in groups)
    assert {job_id for group in groups for job_id in group.job_ids} == {job.job_id for job in jobs}


def test_zero_matching_jobs_keeps_panel_denominator_and_null_ratios() -> None:
    result = _result((), configured_sources=("alpha", "beta"))

    assert result.quality.state == "evidence_limited"
    assert result.opportunities.items == ()
    assert result.observations.sample.collection_cutoff is None
    assert result.observations.sample.source_coverage.configured_source_count == 2
    assert result.observations.sample.source_coverage.matching_source_count == 0
    assert result.observations.sample.source_coverage.source_share == Decimal("0")
    assert result.quality.compensation_coverage is None
    assert result.observations.sample.max_company_concentration is None


def test_invalid_review_ids_are_rejected_before_result_construction() -> None:
    with pytest.raises(ValueError, match="stable order"):
        QualityReviewInput(
            ref="review.md",
            reviewed_job_ids=(UUID(int=2), UUID(int=1)),
        )

    jobs = _jobs(1)
    with pytest.raises(ValueError, match="subset"):
        _result(
            jobs,
            configured_sources=("alpha",),
            review=QualityReviewInput(ref="review.md", reviewed_job_ids=(UUID(int=999),)),
        )


def test_missing_source_url_is_explicit_in_api_and_markdown() -> None:
    job = _jobs(1)[0]
    result = _result((replace(job, source_url=None),), configured_sources=("alpha",))

    payload = _report_response(result).model_dump(mode="json")
    assert payload["opportunities"]["items"][0]["source"]["url"] is None
    markdown = render_markdown_report(result)
    assert "source URL unknown" in markdown


def test_api_and_markdown_project_the_same_ids_and_quality_values() -> None:
    result = _result(_jobs(3), configured_sources=("alpha", "beta", "unused"))
    payload = _report_response(result).model_dump(mode="json")
    markdown = render_markdown_report(result)

    for job_id in payload["opportunities"]["job_ids"]:
        assert job_id in markdown
    for gate in payload["quality"]["gates"]:
        assert gate["gate_id"] in markdown
    serialized = json.dumps(payload, sort_keys=True)
    assert '"rank_score"' not in serialized
    assert '"demanded_skills"' not in serialized
    assert '"gaps"' not in serialized
    assert payload["quality"]["observed_values"]["matching_job_count"] == 3
    assert "- Matching jobs: **3**" in markdown


def test_result_rejects_compensation_aggregate_that_changes_source_evidence() -> None:
    result = _result(_jobs(1), configured_sources=("alpha",))
    group = result.observations.compensation[0]
    changed_group = replace(group, min_value=Decimal("101"))

    with pytest.raises(ValueError, match="range does not reconcile"):
        replace(
            result,
            observations=replace(result.observations, compensation=(changed_group,)),
        )


def _result(
    jobs: tuple[ReportJobEvidence, ...],
    *,
    configured_sources: tuple[str, ...],
    review: QualityReviewInput | None = None,
) -> HybridResult:
    filters = MarketFilters(name="runtime-test")
    profile = Profile(
        name="profile",
        target_role_families=("backend_engineering",),
        seniority="senior",
        skills=(),
    )
    taxonomy = SkillTaxonomy.model_validate(
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
    matching = tuple(job.job_id for job in jobs)
    compensation_groups: dict[
        tuple[str, str, str, UUID | None], list[ReportCompensationEvidence]
    ] = {}
    for job in jobs:
        for item in job.compensation:
            if item.currency is None or (item.min_value is None and item.max_value is None):
                continue
            interval = item.interval or "unknown"
            key = (
                item.component_type,
                item.currency,
                interval,
                job.job_id if interval == "unknown" else None,
            )
            compensation_groups.setdefault(key, []).append(item)
    market = MarketAggregates(
        market_name=filters.name,
        matching_job_ids=matching,
        skill_denominator_job_ids=matching,
        compensation_job_ids=tuple(
            job.job_id
            for job in jobs
            if any(
                item.currency is not None
                and (item.min_value is not None or item.max_value is not None)
                for item in job.compensation
            )
        ),
        collection_cutoff=max((job.last_seen_at for job in jobs), default=None),
        taxonomy_version=taxonomy.version,
        normalizer_version="job-normalization-v3",
        companies=(),
        skills=(),
        seniorities=(),
        workplace_types=(),
        countries=(),
        compensation=tuple(
            CompensationAggregate(
                component_type=key[0],
                currency=key[1],
                interval=key[2],
                location_tier=group[0].location_tier,
                component_ids=tuple(item.component_id for item in group),
                job_ids=tuple(
                    sorted(
                        job.job_id
                        for job in jobs
                        if any(
                            component.component_id == item.component_id
                            for item in group
                            for component in job.compensation
                        )
                    )
                ),
                denominator_job_ids=matching,
                min_value=min(item.min_value for item in group if item.min_value is not None),
                max_value=max(item.max_value for item in group if item.max_value is not None),
            )
            for key, group in sorted(compensation_groups.items(), key=lambda pair: pair[0])
        ),
    )
    source_companies = {job.source_key: job.company_name for job in jobs}
    sources = tuple(
        ReportSourceEvidence(
            key=key,
            kind="greenhouse",
            company_name=source_companies.get(key, _source_company(key)),
            enabled=True,
        )
        for key in configured_sources
    )
    return build_hybrid_result(
        filters=filters,
        profile=profile,
        market=market,
        evidence=ReportEvidenceSnapshot(configured_sources=sources, jobs=jobs),
        taxonomy=taxonomy,
        quality_review=review,
    )


def _jobs(
    count: int,
    *,
    company_count: int = 2,
    unknown_interval: bool = False,
) -> tuple[ReportJobEvidence, ...]:
    jobs = []
    for index in range(count):
        company_index = index % company_count
        source_key = (
            f"company-{company_index}"
            if company_count > 2
            else "alpha"
            if company_index == 0
            else "beta"
        )
        company_name = (
            "Alpha"
            if company_index == 0
            else "Beta"
            if company_count == 2
            else f"Company {company_index}"
        )
        component = (
            ReportCompensationEvidence(
                component_id=UUID(int=1000 + index),
                component_type="base_salary",
                min_value=Decimal("100"),
                max_value=Decimal("200"),
                currency="USD",
                interval=None if unknown_interval else "year",
                location_tier=None,
                source_method="structured_source",
            ),
        )
        mention = ReportSkillEvidence(
            id=UUID(int=2000 + index),
            skill_id="python",
            matched_text="Python",
            section="requirements",
            method="exact",
            confidence=Decimal("1"),
            taxonomy_version="1.0.0",
        )
        jobs.append(
            ReportJobEvidence(
                job_id=UUID(int=index + 1),
                company_name=company_name,
                title="Backend Engineer",
                source_url="https://jobs.example.test/job",
                source_key=source_key,
                source_kind="greenhouse",
                first_seen_at=NOW,
                last_seen_at=NOW + timedelta(minutes=index),
                status="active",
                role_family="backend_engineering",
                seniority="senior",
                workplace_type="remote",
                remote_scope="EU",
                description_text="Build backend systems.",
                locations=(
                    ReportLocationEvidence(
                        ordinal=0,
                        city=None,
                        region=None,
                        country_code="PL",
                        is_primary=True,
                        confidence=Decimal("1"),
                    ),
                ),
                skill_mentions=(mention,),
                compensation=component,
            )
        )
    return tuple(jobs)


def _source_company(key: str) -> str:
    if key == "alpha":
        return "Alpha"
    if key == "beta":
        return "Beta"
    if key.startswith("company-"):
        return f"Company {key.removeprefix('company-')}"
    return key.title()
