"""Deterministic Markdown rendering with explicit denominators and evidence IDs."""

# Schema-shaped tables intentionally keep their columns explicit.
# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import quote
from uuid import UUID

from career_os.application.report import MarketProfileReport
from career_os.config.markets import MarketFilters
from career_os.config.profiles import Profile
from career_os.domain.hybrid_results import (
    HybridBucket,
    HybridConcentration,
    HybridJobCompensation,
    HybridLocation,
    HybridResult,
    HybridSkillMention,
)
from career_os.domain.markets import MarketBucket


def _render_legacy_markdown(report: MarketProfileReport) -> str:
    """Render a stable report without database, filesystem, clock, or network I/O."""
    market = report.market
    comparison = report.comparison
    labels = dict(report.skill_labels)
    lines = [
        f"# Market report: {_inline(report.filters.name)}",
        "",
        f"- Profile: {_code(report.profile.name)}",
        f"- Collection cutoff: {_cutoff(market.collection_cutoff)}",
        "- Freshness basis: latest `last_seen_at` among the exact matching jobs; "
        "no collection-time age is inferred.",
        f"- Taxonomy version: {_code(market.taxonomy_version)}",
        f"- Enrichment normalizer version: {_code(market.normalizer_version)}",
        "- Interpretation: skills are canonical mentions in stored title/description "
        "evidence; this report does not claim that every mention is a requirement.",
        "",
        "## Exact market filters",
        "",
    ]
    lines.extend(_filters_table(report))
    lines.extend(["", "## Exact profile", ""])
    lines.append(
        "Target role families and seniority are declared profile metadata; they do not silently "
        "re-filter the exact market job selection."
    )
    lines.extend(
        [
            "",
            f"- Target role families: {_values(report.profile.target_role_families)}.",
            f"- Seniority: {_code(report.profile.seniority)}.",
            "",
        ]
    )
    if report.profile.skills:
        lines.extend(
            _table(
                ("Skill", "Skill ID", "Level", "Years"),
                (
                    (
                        _cell(labels[skill.id]),
                        _code(skill.id),
                        _code(skill.level),
                        _profile_years(skill.years),
                    )
                    for skill in report.profile.skills
                ),
            )
        )
    else:
        lines.append("No profile skills declared.")
    lines.extend(
        [
            "",
            "## Sample",
            "",
            f"- Matching jobs: **{market.matching_job_count}**. Evidence job IDs: "
            f"{_ids(market.matching_job_ids)}.",
            f"- Companies: **{market.company_count}**. Evidence job IDs: "
            f"{_ids(_union_ids(market.companies))}.",
            f"- Skill denominator: **{len(market.skill_denominator_job_ids)}** jobs with "
            f"non-empty descriptions. Evidence job IDs: {_ids(market.skill_denominator_job_ids)}.",
            "- Compensation coverage: "
            f"**{_share(market.compensation_coverage, market.matching_job_count)}** "
            f"({len(market.compensation_job_ids)}/{market.matching_job_count} jobs). Evidence "
            f"job IDs: {_ids(market.compensation_job_ids)}.",
            "",
            "### Companies",
            "",
        ]
    )
    lines.extend(_bucket_table(market.companies))
    lines.extend(["", "## Top mentioned skills", ""])
    if market.skills:
        lines.extend(
            _table(
                ("Skill", "Skill ID", "Jobs", "Share", "Denominator", "Evidence job IDs"),
                (
                    (
                        _cell(labels[item.value]),
                        _code(item.value),
                        str(item.job_count),
                        _share(item.job_share, item.denominator),
                        str(item.denominator),
                        _ids(item.job_ids),
                    )
                    for item in sorted(
                        market.skills, key=lambda value: (-value.job_count, value.value)
                    )
                ),
            )
        )
    else:
        skill_denominator = len(market.skill_denominator_job_ids)
        if skill_denominator == 0:
            lines.append(
                "No mentioned skills. Skill shares are **undefined** because the denominator is 0; "
                "evidence job IDs: none."
            )
        else:
            lines.append(
                f"No mentioned skills to report among {skill_denominator} jobs with non-empty "
                f"descriptions. Denominator evidence job IDs: "
                f"{_ids(market.skill_denominator_job_ids)}."
            )
    lines.extend(["", "## Distributions", "", "### Seniority", ""])
    lines.extend(_bucket_table(market.seniorities))
    lines.extend(["", "### Workplace type", ""])
    lines.extend(_bucket_table(market.workplace_types))
    lines.extend(["", "### Country", ""])
    if market.countries:
        lines.extend(_bucket_table(market.countries))
    else:
        lines.append(
            "No normalized country evidence. Country share is **undefined**; denominator: 0; "
            "evidence job IDs: none."
        )
    lines.extend(["", "## Compensation", ""])
    lines.append(
        f"Coverage: **{_share(market.compensation_coverage, market.matching_job_count)}** "
        f"({len(market.compensation_job_ids)}/{market.matching_job_count} jobs). Evidence job IDs: "
        f"{_ids(market.compensation_job_ids)}. Amounts remain in original currency and interval; "
        "components and location tiers are not combined, converted, or annualized. Components "
        "with unknown interval remain separate per job."
    )
    lines.append("")
    if market.compensation:
        lines.extend(
            _table(
                (
                    "Component",
                    "Currency",
                    "Interval",
                    "Tier",
                    "Observed min",
                    "Observed max",
                    "Components",
                    "Jobs",
                    "Share",
                    "Evidence job IDs",
                    "Component IDs",
                ),
                (
                    (
                        _code(item.component_type),
                        _code(item.currency),
                        _code(item.interval),
                        _cell(item.location_tier) if item.location_tier is not None else "unknown",
                        _amount(item.min_value),
                        _amount(item.max_value),
                        str(item.component_count),
                        str(item.job_count),
                        _share(item.job_share, item.denominator),
                        _ids(item.job_ids),
                        _ids(item.component_ids),
                    )
                    for item in market.compensation
                ),
            )
        )
    else:
        lines.append("No reportable compensation groups; coverage is 0 jobs.")
    lines.extend(["", "## Profile mention coverage", ""])
    demanded_coverage = _optional_share(
        comparison.demanded_skill_coverage, comparison.demanded_skill_count
    )
    lines.append(
        "Mention-vocabulary coverage: "
        f"**{demanded_coverage}** "
        f"({comparison.covered_skill_count}/{comparison.demanded_skill_count} mentioned skills). "
        f"Mention evidence job IDs: {_ids(_union_ids(comparison.demanded_skills))}."
    )
    lines.extend(["", "### Profile-covered mentions", ""])
    if comparison.covered_skills:
        lines.extend(_skill_bucket_table(comparison.covered_skills, labels))
    else:
        lines.append("No profile-covered mentions; evidence job IDs: none.")
    lines.extend(["", "### Neutral profile skills", ""])
    if comparison.neutral_profile_skill_ids:
        lines.append(
            ", ".join(
                f"{_cell(labels[skill_id])} ({_code(skill_id)})"
                for skill_id in comparison.neutral_profile_skill_ids
            )
            + ". These profile skills have no current market-mention evidence and are not "
            "penalized."
        )
    else:
        lines.append("None.")
    lines.extend(["", "### Ranked uncovered mentions", ""])
    lines.append(
        "Inclusive thresholds: job count >= "
        f"{comparison.gap_thresholds.min_job_count}; job share >= "
        f"{_decimal(comparison.gap_thresholds.min_job_share)}. Rank formula: "
        "`job_share * log1p(job_count)`."
    )
    lines.append("")
    if comparison.gaps:
        lines.extend(
            _table(
                ("Rank", "Skill", "Skill ID", "Jobs", "Share", "Score", "Evidence job IDs"),
                (
                    (
                        str(index),
                        _cell(labels[gap.skill_id]),
                        _code(gap.skill_id),
                        str(gap.job_count),
                        _share(gap.job_share, len(gap.denominator_job_ids)),
                        f"{gap.rank_score:.6f}",
                        _ids(gap.job_ids),
                    )
                    for index, gap in enumerate(comparison.gaps, start=1)
                ),
            )
        )
    else:
        lines.append(
            "No uncovered mentions meet the configured thresholds; evidence job IDs: none."
        )
    lines.extend(["", "## Representative jobs", ""])
    if comparison.representative_jobs:
        jobs = {item.job_id: item for item in report.jobs}
        lines.extend(
            _table(
                (
                    "Job",
                    "Company",
                    "Skill coverage",
                    "Covered mentions",
                    "Mentioned",
                    "Uncovered mentions",
                    "Evidence job ID",
                ),
                (
                    (
                        _job_link(jobs[item.job_id].title, jobs[item.job_id].source_url),
                        _cell(jobs[item.job_id].company_name),
                        f"{_optional_share(item.skill_coverage, item.denominator)} "
                        f"({item.numerator}/{item.denominator})",
                        ", ".join(_code(value) for value in item.covered_skill_ids) or "none",
                        ", ".join(_code(value) for value in item.mentioned_skill_ids) or "none",
                        ", ".join(_code(value) for value in item.gap_skill_ids) or "none",
                        _code(str(item.job_id)),
                    )
                    for item in comparison.representative_jobs
                ),
            )
        )
    else:
        lines.append(
            "No representative jobs have known mention-based skill coverage; "
            "evidence job IDs: none."
        )
    lines.extend(["", "## Full matching-job evidence", ""])
    if report.jobs:
        lines.extend(
            _table(
                ("Evidence job ID", "Company", "Job / stored source URL"),
                (
                    (
                        _code(str(job.job_id)),
                        _cell(job.company_name),
                        _job_link(job.title, job.source_url),
                    )
                    for job in report.jobs
                ),
            )
        )
    else:
        lines.append("No matching jobs; evidence job IDs: none.")
    return "\n".join(lines) + "\n"


def _filters_table(report: MarketProfileReport) -> list[str]:
    filters = report.filters
    return _table(
        ("Filter", "Exact value"),
        (
            ("name", _code(filters.name)),
            ("title_include (ANY)", _values(filters.title_include)),
            ("title_exclude (NONE)", _values(filters.title_exclude)),
            ("role_families", _values(filters.role_families)),
            ("seniorities", _values(filters.seniorities)),
            ("country_codes", _values(filters.country_codes)),
            ("include_remote", _code(str(filters.include_remote).lower())),
            ("remote_scopes (exact normalized scope)", _values(filters.remote_scopes)),
            ("company_names", _values(filters.company_names)),
            ("active_only", _code(str(filters.active_only).lower())),
        ),
    )


def _bucket_table(buckets: Sequence[MarketBucket]) -> list[str]:
    if not buckets:
        return ["No values; denominator: 0; share: **undefined**; evidence job IDs: none."]
    return _table(
        ("Value", "Jobs", "Share", "Denominator", "Evidence job IDs"),
        (
            (
                _cell(item.value),
                str(item.job_count),
                _share(item.job_share, item.denominator),
                str(item.denominator),
                _ids(item.job_ids),
            )
            for item in buckets
        ),
    )


def _skill_bucket_table(buckets: Sequence[MarketBucket], labels: dict[str, str]) -> list[str]:
    return _table(
        ("Skill", "Skill ID", "Jobs", "Share", "Denominator", "Evidence job IDs"),
        (
            (
                _cell(labels[item.value]),
                _code(item.value),
                str(item.job_count),
                _share(item.job_share, item.denominator),
                str(item.denominator),
                _ids(item.job_ids),
            )
            for item in buckets
        ),
    )


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        if len(row) != len(headers):
            raise ValueError("Markdown table row width does not match its headers")
        output.append("| " + " | ".join(row) + " |")
    return output


def _cell(value: str | None) -> str:
    if value is None:
        return "unknown"
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\\", "&#92;")
        .replace("|", "&#124;")
        .replace("\r\n", "<br>")
        .replace("\r", "<br>")
        .replace("\n", "<br>")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
    )


def _inline(value: str) -> str:
    return _cell(value).replace("#", "&#35;")


def _code(value: str) -> str:
    return "`" + _cell(value).replace("`", "&#96;") + "`"


def _job_link(title: str, source_url: str | None) -> str:
    label = _cell(title)
    if source_url is None or not source_url.strip():
        return f"{label} (source URL unknown)"
    encoded = quote(source_url, safe=":/?#@!$&'*+,;=%~-._")
    return f"[{label}]({encoded})"


def _values(values: Sequence[str]) -> str:
    return ", ".join(_code(value) for value in values) if values else "none (no restriction)"


def _ids(values: Sequence[UUID]) -> str:
    return ", ".join(_code(str(value)) for value in values) if values else "none"


def _union_ids(buckets: Sequence[MarketBucket]) -> tuple[UUID, ...]:
    return tuple(sorted({job_id for bucket in buckets for job_id in bucket.job_ids}))


def _share(value: Decimal, denominator: int) -> str:
    if denominator == 0:
        return "undefined"
    return f"{_decimal(value * 100)}%"


def _optional_share(value: Decimal | None, denominator: int) -> str:
    return "undefined" if value is None or denominator == 0 else f"{_decimal(value * 100)}%"


def _decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _amount(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    return format(value, "f")


def _profile_years(value: Decimal | None) -> str:
    return "unknown" if value is None else _decimal(value)


def _cutoff(value: datetime | None) -> str:
    if value is None:
        return "unknown (no matching jobs)"
    return _code(value.isoformat().replace("+00:00", "Z"))


def render_markdown_report(report: MarketProfileReport | HybridResult) -> str:
    """Render the canonical H1 result (with a compatibility path for old unit fixtures)."""
    if isinstance(report, HybridResult):
        return _render_hybrid_markdown(report)
    return _render_legacy_markdown(report)


def _render_hybrid_markdown(report: HybridResult) -> str:
    """Pure deterministic presentation of one immutable H1 application result."""
    quality = report.quality
    observations = report.observations
    sample = observations.sample
    failed = quality.failed_gates
    lines = [
        f"# Market Explorer result: {_inline(report.filters.name)}",
        "",
        f"- Contract: {_code('h1.hybrid-result.v1')}",
        f"- Result state: {_code(quality.state)}",
        f"- Opportunities: **available** ({report.opportunities.count} jobs); broad-market claims: **disabled**.",
        f"- Panel observations: **{'panel-bounded' if quality.state == 'accepted' else 'qualified'}**; scope: {_code('configured_source_panel')}.",
    ]
    if failed:
        lines.append(
            "- Failed gates: "
            + ", ".join(
                f"{_code(gate.gate_id)} observed {_value(gate.observed)} (threshold {_value(gate.threshold)})"
                for gate in quality.gates
                if gate.gate_id in failed
            )
            + "."
        )
    else:
        lines.append("- Failed gates: none.")
    lines.extend(
        [
            "- Skill semantics: canonical **mentions only**; no requirement, demand, gap, fit score, or hiring probability is inferred.",
            "",
            "## Concrete opportunities",
            "",
            "Every eligible matching job remains available, including when panel observations are evidence-limited.",
            "",
        ]
    )
    if report.opportunities.items:
        lines.extend(
            _table(
                (
                    "Evidence job ID",
                    "Company",
                    "Title",
                    "First seen",
                    "Last seen",
                    "Locations / remote scope",
                    "Mention evidence",
                    "Compensation evidence",
                    "Source URL",
                ),
                (
                    (
                        _code(str(item.job_id)),
                        _cell(item.company_name),
                        _cell(item.title),
                        _code(_timestamp(item.freshness.first_seen_at)),
                        _code(_timestamp(item.freshness.last_seen_at)),
                        _location_text(item.locations, item.remote_scope),
                        _mention_text(item.skill_mentions),
                        _compensation_text(item.compensation),
                        _job_link("source", item.source.url),
                    )
                    for item in report.opportunities.items
                ),
            )
        )
    else:
        lines.append("No matching opportunities; evidence job IDs: none.")

    lines.extend(
        [
            "",
            "## Panel-bounded observations (configured source panel)",
            "",
            f"All observations below describe only this configured public-source panel and its {len(sample.job_ids)}-job matching sample.",
            "",
            "### Evidence quality",
            "",
        ]
    )
    lines.extend(
        _table(
            ("Gate", "Status", "Threshold", "Observed", "Basis", "Provenance"),
            (
                (
                    _code(gate.gate_id),
                    gate.status,
                    _value(gate.operator) + " " + _value(gate.threshold),
                    _value(gate.observed),
                    evaluation.basis.replace("_", " "),
                    _cell(evaluation.provenance.ref),
                )
                for gate, (_, evaluation) in zip(
                    quality.gates, quality.evaluation_provenance, strict=True
                )
            ),
        )
    )
    lines.extend(
        [
            "",
            "### Sample",
            "",
            f"- Matching jobs: **{len(sample.job_ids)}**; evidence job IDs: {_ids(sample.job_ids)}.",
            f"- Companies: **{len(sample.company_names)}** ({_values(sample.company_names)}).",
            f"- Maximum company concentration: {_concentration_text(sample.max_company_concentration)}.",
            f"- Collection cutoff: {_cutoff(sample.collection_cutoff)} (maximum persisted `last_seen_at`; no collection-time age inferred).",
            f"- Skill denominator: **{len(sample.skill_denominator_job_ids)}** jobs with non-empty descriptions; evidence job IDs: {_ids(sample.skill_denominator_job_ids)}.",
            f"- Compensation denominator: **{len(sample.compensation_job_ids)}** matching jobs; evidence job IDs: {_ids(sample.compensation_job_ids)}.",
            "",
            "### Source coverage",
            "",
        ]
    )
    coverage = sample.source_coverage
    lines.append(
        f"- Configured sources: **{coverage.configured_source_count}** ({_values(coverage.configured_source_keys)}); matching sources: **{coverage.matching_source_count}**; source coverage: **{_h_share(coverage.source_share)}**."
    )
    if coverage.job_ids_by_source:
        for source_key, source_job_ids in coverage.job_ids_by_source:
            lines.append(f"- {_code(source_key)}: job IDs {_ids(source_job_ids)}.")
    else:
        lines.append("No configured sources; source coverage is **undefined**.")

    lines.extend(["", "### Companies", ""])
    lines.extend(_hybrid_bucket_table(observations.companies))
    lines.extend(["", "### Skill mentions", ""])
    if observations.skills:
        lines.extend(
            _table(
                (
                    "Skill",
                    "Semantics",
                    "Jobs",
                    "Job share",
                    "Companies",
                    "Company share",
                    "Evidence job IDs",
                    "Denominator job IDs",
                ),
                (
                    (
                        f"{_cell(item.label)} ({_code(item.skill_id)})",
                        "mention",
                        str(item.job_count),
                        _h_share(item.job_share),
                        f"{item.company_count} ({_values(item.company_names)})",
                        f"{_h_share(item.company_share)} of {len(item.denominator_company_names)} described companies",
                        _ids(item.job_ids),
                        _ids(item.denominator_job_ids),
                    )
                    for item in observations.skills
                ),
            )
        )
    else:
        lines.append(
            "No mentioned skills; shares are **undefined** when the description denominator is 0."
        )

    lines.extend(["", "### Distributions", "", "#### Seniority", ""])
    lines.extend(_hybrid_bucket_table(observations.seniorities))
    lines.extend(["", "#### Workplace type", ""])
    lines.extend(_hybrid_bucket_table(observations.workplace_types))
    lines.extend(["", "#### Country", ""])
    lines.extend(_hybrid_bucket_table(observations.countries))
    lines.extend(["", "### Compensation", ""])
    lines.append(
        f"Coverage: **{_h_share(quality.compensation_coverage)}** ({len(sample.compensation_job_ids)}/{len(sample.job_ids)} jobs); evidence job IDs: {_ids(sample.compensation_job_ids)}. Amounts remain in original currency and interval; no conversion, annualization, component composition, or pooling is performed. Components with unknown interval remain separate per job."
    )
    lines.append("")
    if observations.compensation:
        lines.extend(
            _table(
                (
                    "Component",
                    "Currency",
                    "Interval",
                    "Tier",
                    "Observed min",
                    "Observed max",
                    "Components",
                    "Jobs",
                    "Share",
                    "Evidence job IDs",
                    "Component IDs",
                ),
                (
                    (
                        _code(item.component_type),
                        _code(item.currency),
                        _code(item.interval),
                        _cell(item.location_tier) if item.location_tier is not None else "unknown",
                        _amount(item.min_value),
                        _amount(item.max_value),
                        str(item.component_count),
                        str(item.job_count),
                        _h_share(item.job_share),
                        _ids(item.job_ids),
                        _ids(item.component_ids),
                    )
                    for item in observations.compensation
                ),
            )
        )
    else:
        lines.append("No reportable compensation groups; coverage is 0 jobs.")

    profile_mentions = observations.profile_mentions
    lines.extend(["", "### Profile mention observations", ""])
    lines.append(
        "These are mention observations, not requirements, gaps, recommendations, or a fit score."
    )
    lines.append(
        f"- Profile skills: {_values(profile_mentions.profile_skill_ids)}; covered mentions: {_values(profile_mentions.covered_skill_ids)}; neutral skills: {_values(profile_mentions.neutral_skill_ids)}."
    )
    lines.append(
        "- Uncovered mentions: "
        + (
            ", ".join(
                f"{_cell(item.label)} ({_code(item.skill_id)})"
                for item in profile_mentions.uncovered_mentions
            )
            or "none"
        )
        + "."
    )
    lines.extend(["", "## Exact filters and profile metadata", ""])
    lines.append(
        "The exact validated query filters and reviewed profile are retained as audit context; they do not become an inferred target universe."
    )
    lines.extend(
        ["", *_hybrid_filters_table(report.filters), "", *_hybrid_profile_table(report.profile)]
    )
    return "\n".join(lines) + "\n"


def _hybrid_bucket_table(buckets: Sequence[HybridBucket]) -> list[str]:
    if not buckets:
        return ["No values; denominator: 0; share: **undefined**; evidence job IDs: none."]
    return _table(
        ("Value", "Jobs", "Share", "Denominator", "Evidence job IDs"),
        (
            (
                _cell(item.value),
                str(item.job_count),
                _h_share(item.job_share),
                str(len(item.denominator_job_ids)),
                _ids(item.job_ids),
            )
            for item in buckets
        ),
    )


def _hybrid_filters_table(filters: MarketFilters) -> list[str]:
    return _table(
        ("Filter", "Exact value"),
        (
            ("name", _code(filters.name)),
            ("title_include (ANY)", _values(filters.title_include)),
            ("title_exclude (NONE)", _values(filters.title_exclude)),
            ("role_families", _values(filters.role_families)),
            ("seniorities", _values(filters.seniorities)),
            ("country_codes", _values(filters.country_codes)),
            ("include_remote", _code(str(filters.include_remote).lower())),
            ("remote_scopes (exact normalized scope)", _values(filters.remote_scopes)),
            ("company_names", _values(filters.company_names)),
            ("active_only", _code(str(filters.active_only).lower())),
        ),
    )


def _hybrid_profile_table(profile: Profile) -> list[str]:
    lines = [
        f"- Target role families: {_values(profile.target_role_families)}.",
        f"- Seniority: {_code(profile.seniority)}.",
        "",
    ]
    if profile.skills:
        lines.extend(
            _table(
                ("Skill ID", "Level", "Years"),
                (
                    (_code(item.id), _code(item.level), _profile_years(item.years))
                    for item in profile.skills
                ),
            )
        )
    else:
        lines.append("No profile skills declared.")
    return lines


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _value(value: object) -> str:
    if value is None:
        return "undefined"
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str):
        return _code(value)
    return str(value)


def _h_share(value: Decimal | None) -> str:
    return "undefined" if value is None else f"{_decimal(value * 100)}%"


def _location_text(locations: Sequence[HybridLocation], remote_scope: str | None) -> str:
    values = []
    for location in locations:
        parts = [item for item in (location.city, location.region, location.country_code) if item]
        if parts:
            values.append(" / ".join(parts))
    if remote_scope:
        values.append(remote_scope)
    return _cell(" / ".join(values)) if values else "unknown"


def _mention_text(mentions: Sequence[HybridSkillMention]) -> str:
    return (
        ", ".join(f"{_cell(item.matched_text)} ({_code(str(item.id))})" for item in mentions)
        or "none"
    )


def _compensation_text(components: Sequence[HybridJobCompensation]) -> str:
    values = []
    for item in components:
        amount = f"{_amount(item.min_value)}-{_amount(item.max_value)}"
        values.append(
            f"{item.currency} {amount}/{item.interval or 'unknown'}, {item.location_tier or 'unknown'} ({_code(str(item.component_id))})"
        )
    return "; ".join(values) or "none"


def _concentration_text(item: HybridConcentration | None) -> str:
    if item is None:
        return "undefined (no matching jobs)"
    return f"{_cell(item.company_name)}, {item.job_count}/{len(item.denominator_job_ids)} jobs, {_h_share(item.job_share)}; evidence job IDs: {_ids(item.job_ids)}"
