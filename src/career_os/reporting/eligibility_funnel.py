"""Stable Markdown rendering for the eligibility-funnel audit."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from uuid import UUID

from career_os.application.eligibility_funnel import EligibilityFunnelAudit, FunnelGroup


def render_eligibility_funnel(audit: EligibilityFunnelAudit) -> str:
    """Render an audit without filesystem, clock, database, or network I/O."""
    stages = audit.stages
    cohort_count = len(audit.cohort_job_ids)
    evidence = {item.job_id: item for item in audit.representative_jobs}
    lines = [
        f"# Eligibility funnel audit: {_inline(audit.filters.name)}",
        "",
        "## Cohort and method",
        "",
        "- Cohort: every canonical job currently stored in PostgreSQL at the read-only "
        "repeatable-read snapshot; no historical state is reconstructed.",
        f"- Canonical cohort: **{cohort_count}** jobs.",
        f"- Earliest first-seen timestamp: {_timestamp(audit.cohort_first_seen_at)}.",
        f"- Collection cutoff: {_timestamp(audit.collection_cutoff)} "
        "(maximum stored `last_seen_at` in the cohort).",
        f"- Taxonomy versions present: {_values(audit.taxonomy_versions)}.",
        "- Enrichment normalizer versions present: "
        f"{_values(audit.enrichment_normalizer_versions)}.",
        "- Exclusion policy: each non-matching job is assigned to its first failed predicate; "
        "reason buckets are mutually exclusive and partition the cohort together with "
        "final matches.",
        "- Geography staging: a job passes evidence with a normalized target-country row or, "
        "when configured, explicit `workplace_type=remote`; the next stage requires either the "
        "target-country row or an exact accepted remote scope. Local jobs are not required "
        "to be remote.",
        "- Reconciliation: final staged IDs exactly equal the production market-selection query.",
        "- Large evidence sets: Markdown shows a SHA-256 digest of the sorted newline-delimited "
        "UUIDs plus the first five IDs; rerunning the command materializes the exact sets.",
        "",
        "## Exact market filters",
        "",
        "| Filter | Value |",
        "|---|---|",
        f"| Name | `{_inline(audit.filters.name)}` |",
        f"| Active only | `{str(audit.filters.active_only).lower()}` |",
        f"| Title include (ANY) | {_codes(audit.filters.title_include)} |",
        f"| Title exclude (NONE) | {_codes(audit.filters.title_exclude)} |",
        f"| Role families | {_codes(audit.filters.role_families)} |",
        f"| Seniorities | {_codes(audit.filters.seniorities)} |",
        f"| Country codes | {_codes(audit.filters.country_codes)} |",
        f"| Include remote | `{str(audit.filters.include_remote).lower()}` |",
        f"| Accepted remote scopes | {_codes(audit.filters.remote_scopes)} |",
        f"| Companies | {_codes(audit.filters.company_names)} |",
        "",
        "## Overall funnel",
        "",
        "| Stage | Jobs | Cohort share | Lost at stage | Evidence IDs / digest |",
        "|---|---:|---:|---:|---|",
    ]
    previous = cohort_count
    for stage in stages:
        count = len(stage.job_ids)
        lost = 0 if stage.key == "cohort" else previous - count
        lines.append(
            f"| {_inline(stage.label)} (`{stage.key}`) | {count} | "
            f"{_percent(count, cohort_count)} | {lost} | {_id_evidence(stage.job_ids)} |"
        )
        previous = count

    lines.extend(
        [
            "",
            "## Funnel by source and company",
            "",
            "Counts are cumulative. Column keys match the overall funnel above.",
            "",
            "| Source | Kind | Company | cohort | active | title incl. | title accepted | role | "
            "seniority | geo evidence | eligible geography | configured company | final |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for group in audit.groups:
        counts = dict(group.counts)
        lines.append(
            f"| `{_inline(group.source_key)}` | `{_inline(group.source_kind)}` | "
            f"{_inline(group.company_name)} | "
            + " | ".join(str(counts[stage.key]) for stage in stages)
            + " |"
        )

    lines.extend(
        [
            "",
            "## Mutually exclusive first-failure reasons",
            "",
            "| Reason | Jobs | Cohort share | Representative job IDs |",
            "|---|---:|---:|---|",
        ]
    )
    for exclusion in audit.exclusions:
        lines.append(
            f"| {_inline(exclusion.label)} (`{exclusion.reason}`) | {len(exclusion.job_ids)} | "
            f"{_percent(len(exclusion.job_ids), cohort_count)} | "
            f"{_ids(exclusion.representative_job_ids)} |"
        )
    lines.append(
        f"| Final match (not excluded) | {len(audit.matching_job_ids)} | "
        f"{_percent(len(audit.matching_job_ids), cohort_count)} | "
        f"{_ids(audit.matching_job_ids)} |"
    )

    lines.extend(
        [
            "",
            "## Representative exclusion evidence",
            "",
            "Representatives are the first stable UUIDs in each reason bucket; they are inspection "
            "examples, not a random or statistically representative sample.",
            "",
            "| Reason | Job ID | Source | Company | Title / stored URL | Role | Seniority | "
            "Workplace | Remote scope | Countries |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    by_reason = {item.reason: item for item in audit.exclusions}
    for reason in by_reason:
        for job_id in by_reason[reason].representative_job_ids:
            job = evidence[job_id]
            lines.append(
                f"| `{reason}` | `{job.job_id}` | `{_inline(job.source_key)}` | "
                f"{_inline(job.company_name)} | {_job(job.title, job.source_url)} | "
                f"{_optional_code(job.role_family)} | {_optional_code(job.seniority)} | "
                f"{_optional_code(job.workplace_type)} | {_optional_code(job.remote_scope)} | "
                f"{_codes(job.country_codes)} |"
            )

    zero_yield = tuple(group for group in audit.groups if dict(group.counts)["final_match"] == 0)
    productive = tuple(group for group in audit.groups if dict(group.counts)["final_match"] > 0)
    lines.extend(
        [
            "",
            "## D1 recommendations",
            "",
            "### D2 source-panel input",
            "",
            f"- Productive source/company rows to retain for review: **{len(productive)}**: "
            f"{_group_names(productive)}.",
            f"- Zero-final-yield source/company rows to review or replace: **{len(zero_yield)}**: "
            f"{_group_names(zero_yield)}.",
            "- Prioritize replacements with explicit public ATS contracts and demonstrated senior "
            "backend/platform hiring relevant to Poland/EU; do not weaken evidence rules to "
            "improve yield.",
            "",
            "### Possible D4 calibration input",
            "",
            "- Jobs excluded only because no title-inclusion term matched, while every later "
            f"current predicate passed: **{len(audit.title_include_only_exclusion_job_ids)}**. "
            f"Evidence IDs: {_id_evidence(audit.title_include_only_exclusion_job_ids)}.",
            "- Inspect the largest title, role-family, seniority, and geography first-failure "
            "buckets above for concrete false negatives. Representative IDs make each proposed "
            "change auditable.",
            "- A rule change is justified only by demonstrated false negatives plus "
            "counterexamples; "
            "D1 itself changes no title, role, seniority, location, or remote-scope rule.",
            "",
        ]
    )
    return "\n".join(lines)


def _timestamp(value: datetime | None) -> str:
    return "none" if value is None else f"`{value.isoformat().replace('+00:00', 'Z')}`"


def _values(values: tuple[str, ...]) -> str:
    return ", ".join(f"`{_inline(value)}`" for value in values) or "none"


def _codes(values: tuple[str, ...]) -> str:
    return _values(values)


def _ids(values: tuple[UUID, ...]) -> str:
    return ", ".join(f"`{value}`" for value in values) or "none"


def _id_evidence(values: tuple[UUID, ...]) -> str:
    if len(values) <= 10:
        return _ids(values)
    serialized = "\n".join(str(value) for value in values).encode()
    digest = sha256(serialized).hexdigest()
    return f"`sha256:{digest}`; first 5: {_ids(values[:5])}"


def _optional_code(value: str | None) -> str:
    return "unknown" if value is None or not value.strip() else f"`{_inline(value)}`"


def _inline(value: str) -> str:
    return value.replace("|", "\\|").replace("`", "\\`").replace("\n", " ")


def _job(title: str, url: str | None) -> str:
    label = _inline(title)
    return label if url is None else f"[{label}]({url.replace(' ', '%20').replace(')', '%29')})"


def _percent(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "undefined"
    value = (Decimal(numerator) / Decimal(denominator) * Decimal(100)).quantize(Decimal("0.01"))
    return f"{value}%"


def _group_names(groups: tuple[FunnelGroup, ...]) -> str:
    names = sorted({group.company_name for group in groups})
    return ", ".join(f"`{_inline(name)}`" for name in names) or "none"
