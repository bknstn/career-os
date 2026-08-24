"""Deterministic Markdown rendering with explicit denominators and evidence IDs."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import quote
from uuid import UUID

from career_os.application.report import MarketProfileReport
from career_os.domain.markets import MarketBucket


def render_markdown_report(report: MarketProfileReport) -> str:
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
