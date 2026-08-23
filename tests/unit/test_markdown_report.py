"""Pure Markdown rendering and report-snapshot reconciliation tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from career_os.application.profile_fit import ProfileComparisonService
from career_os.application.report import MarketProfileReport
from career_os.config.markets import MarketFilters
from career_os.config.profiles import Profile
from career_os.config.skills import SkillTaxonomy
from career_os.domain.markets import MarketAggregates, MarketBucket
from career_os.persistence.report_queries import ReportJobEvidence
from career_os.reporting import render_markdown_report

JOB_ID = UUID("00000000-0000-0000-0000-000000000001")


def test_renderer_escapes_table_text_and_links_to_exact_stored_source_url() -> None:
    report = _report(
        company="A | B\n<script>",
        title="Senior [Backend] | Engineer",
        source_url="https://jobs.example.test/a-path/(one)|two?x=1&y=2",
        label="Python | Language",
    )

    rendered = render_markdown_report(report)

    assert "A &#124; B<br>&lt;script&gt;" in rendered
    assert "Senior &#91;Backend&#93; &#124; Engineer" in rendered
    assert "https://jobs.example.test/a-path/%28one%29%7Ctwo?x=1&y=2" in rendered
    assert "Python &#124; Language" in rendered
    assert "does not claim that every mention is a requirement" in rendered
    assert "fit probability" not in rendered
    assert "Target role families: `backend_engineering`" in rendered
    assert "Python &#124; Language | `python` | `strong` | unknown" in rendered
    assert str(JOB_ID) in rendered


def test_empty_market_preserves_unknown_cutoff_and_undefined_denominators() -> None:
    report = _report(empty=True)

    rendered = render_markdown_report(report)

    assert "Collection cutoff: unknown (no matching jobs)" in rendered
    assert "Matching jobs: **0**. Evidence job IDs: none." in rendered
    assert "Compensation coverage: **undefined** (0/0 jobs)" in rendered
    assert "Skill shares are **undefined** because the denominator is 0" in rendered
    assert "Demanded-skill coverage: **undefined** (0/0 demanded skills)" in rendered
    assert "No profile skills declared." in rendered
    assert "No matching jobs; evidence job IDs: none." in rendered


def test_described_jobs_without_mentions_do_not_claim_an_undefined_nonzero_share() -> None:
    rendered = render_markdown_report(_report(no_mentions=True))

    assert "No mentioned skills to report among 1 jobs with non-empty descriptions" in rendered
    assert "undefined because the denominator is 1" not in rendered


def test_missing_stored_source_url_remains_explicit_and_is_not_invented() -> None:
    rendered = render_markdown_report(_report(source_url=None))

    assert "Backend Engineer (source URL unknown)" in rendered


def test_report_snapshot_rejects_missing_or_mismatched_job_evidence() -> None:
    report = _report()

    with pytest.raises(ValueError, match="exactly cover"):
        MarketProfileReport(
            filters=report.filters,
            profile=report.profile,
            market=report.market,
            comparison=report.comparison,
            jobs=(),
            skill_labels=report.skill_labels,
        )


@pytest.mark.parametrize(
    "source_url",
    (
        "javascript:alert(1)",
        "//jobs.example.test/one",
        "https://jobs.example.test/a\nnext",
        "https://user:secret@jobs.example.test/one",
        "https://jobs.example.test\\@evil.test/one",
        "https://jobs.example.test:not-a-port/one",
    ),
)
def test_report_job_evidence_rejects_unsafe_or_nonabsolute_urls(source_url: str) -> None:
    with pytest.raises(ValueError, match=r"safe absolute HTTP\(S\)"):
        ReportJobEvidence(JOB_ID, "Example", "Backend Engineer", source_url)


def _report(
    *,
    empty: bool = False,
    company: str = "Example",
    title: str = "Backend Engineer",
    source_url: str | None = "https://jobs.example.test/one",
    label: str = "Python",
    no_mentions: bool = False,
) -> MarketProfileReport:
    taxonomy = SkillTaxonomy.model_validate(
        {
            "version": "1.0.0",
            "skills": [
                {"id": "python", "label": label, "category": "language", "aliases": ["Python"]}
            ],
        }
    )
    profile = Profile.model_validate(
        {
            "name": "current",
            "target_role_families": ["backend_engineering"],
            "seniority": "senior",
            "skills": [] if empty or no_mentions else [{"id": "python", "level": "strong"}],
        }
    )
    matching = () if empty else (JOB_ID,)
    skill_denominator = () if empty else matching
    company_buckets = () if empty else (MarketBucket(company, matching, matching),)
    skill_buckets = () if empty or no_mentions else (MarketBucket("python", matching, matching),)
    market = MarketAggregates(
        market_name="backend",
        matching_job_ids=matching,
        skill_denominator_job_ids=skill_denominator,
        compensation_job_ids=(),
        collection_cutoff=None if empty else datetime(2026, 8, 21, 12, tzinfo=UTC),
        taxonomy_version="1.0.0",
        normalizer_version="job-normalization-v3",
        companies=company_buckets,
        skills=skill_buckets,
        seniorities=() if empty else (MarketBucket("senior", matching, matching),),
        workplace_types=() if empty else (MarketBucket("remote", matching, matching),),
        countries=(),
        compensation=(),
    )
    comparison = ProfileComparisonService(taxonomy).calculate(profile, market)
    jobs = (
        ()
        if empty
        else (
            ReportJobEvidence(
                job_id=JOB_ID,
                company_name=company,
                title=title,
                source_url=source_url,
            ),
        )
    )
    return MarketProfileReport(
        filters=MarketFilters(name="backend"),
        profile=profile,
        market=market,
        comparison=comparison,
        jobs=jobs,
        skill_labels=(("python", label),),
    )
