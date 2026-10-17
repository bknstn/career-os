"""Saved-inventory and immutable-history services for the Career Radar MVP."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import NotRequired, TypedDict
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy.orm import Session

from career_os.application.radar_mvp_history import PostingAnalysis, compare_mvp_history
from career_os.config.radar_panel import RadarPanelManifest, RadarPanelManifestIdentity
from career_os.config.skills import SkillTaxonomy
from career_os.config.sources import ConfigurationError
from career_os.domain.canonical import content_hash, stable_json_dumps
from career_os.domain.skills import SkillEvidence
from career_os.normalization.skills import SkillExtractor
from career_os.persistence.radar_mvp import (
    RadarMvpHistoryInputs,
    RadarMvpJob,
    RadarMvpReadRepository,
    RadarMvpSource,
)
from career_os.radar.role_contract import (
    OCCUPATIONS,
    SPECIALTIES,
    RoleAssignment,
    adapt_classification,
)
from career_os.radar.roles import ROLE_TAXONOMY, classify_role

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PANEL_PATH = _PROJECT_ROOT / "config/radar/panel.mvp.v1.yaml"
_SKILL_TAXONOMY_PATH = _PROJECT_ROOT / "config/taxonomy/skills.v1.yaml"
MVP_SCHEMA_VERSION = "career-radar.mvp.snapshot.v1"
MVP_DETAIL_SCHEMA_VERSION = "career-radar.mvp.job-detail.v1"
MVP_METHODOLOGY_SCHEMA_VERSION = "career-radar.mvp.methodology.v1"
LEGACY_CLASSIFIER_VERSION = "1.1.0"
ROLE_CONTRACT_VERSION = "2.0.0"

OCCUPATION_LABELS = {
    "technical_engineering": "Technical engineering",
    "engineering_management": "Engineering management",
    "solutions_architecture": "Solutions architecture",
    "nontechnical": "Nontechnical occupation",
    "unknown": "Unknown occupation",
}
SPECIALTY_LABELS = {
    item.id.value: item.label for item in ROLE_TAXONOMY.families if item.id.value in SPECIALTIES
}


class RadarMvpQueryError(ValueError):
    """A user-supplied Radar filter is invalid."""


class _Facet(TypedDict):
    id: str
    label: str
    count: int
    share: float | None
    category: NotRequired[str]


@dataclass(frozen=True, slots=True)
class RadarMvpFilters:
    q: str | None = None
    occupation: str | None = None
    specialty: str | None = None
    skill: str | None = None
    source: str | None = None
    page: int = 1
    page_size: int = 25
    include_inactive: bool = False
    enabled_only: bool = True
    period_days: int = 90

    def validate(self) -> None:
        if self.q is not None and (not self.q.strip() or len(self.q) > 200):
            raise RadarMvpQueryError("q must contain 1 to 200 non-whitespace characters")
        if self.occupation is not None and self.occupation not in OCCUPATIONS:
            raise RadarMvpQueryError("unknown occupation ID")
        if self.specialty is not None and self.specialty not in SPECIALTIES:
            raise RadarMvpQueryError("unknown specialty ID")
        if self.page < 1:
            raise RadarMvpQueryError("page must be positive")
        if not 1 <= self.page_size <= 100:
            raise RadarMvpQueryError("page_size must be between 1 and 100")
        if self.period_days not in {30, 90, 180}:
            raise RadarMvpQueryError("period_days must be 30, 90, or 180")


def _taxonomy_digest(taxonomy: SkillTaxonomy) -> str:
    return content_hash(taxonomy.model_dump(mode="json"))


@lru_cache(maxsize=8)
def _skill_extractor(taxonomy_json: str) -> SkillExtractor:
    return SkillExtractor(SkillTaxonomy.model_validate_json(taxonomy_json))


@lru_cache(maxsize=32_768)
def _skill_evidence_cached(
    extractor: SkillExtractor, title: str, description: str
) -> tuple[SkillEvidence, ...]:
    return extractor.extract(title=title, description=description)


@lru_cache(maxsize=32_768)
def _role_assignment_cached(title: str, description: str) -> RoleAssignment:
    legacy = classify_role(title, description)
    return adapt_classification(title, legacy)


def _safe_url(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            return None
        _ = parsed.port
    except ValueError:
        return None
    return value.strip()


def _dt(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 9) if denominator else None


def _query_text(job: RadarMvpJob, query: str) -> bool:
    needle = query.casefold()
    return any(
        needle in value.casefold()
        for value in (job.title, job.description, job.company_name, job.source_key)
    )


class RadarMvpService:
    """Build the current view and per-metric comparisons from supplied read session."""

    def __init__(
        self,
        *,
        session: Session,
        taxonomy: SkillTaxonomy,
        freshness_target_seconds: int,
    ) -> None:
        self._session = session
        self._repository = RadarMvpReadRepository(session)
        self._taxonomy = taxonomy
        self._freshness_target_seconds = freshness_target_seconds
        taxonomy_payload = stable_json_dumps(taxonomy.model_dump(mode="json"))
        self._extractor = _skill_extractor(taxonomy_payload)
        self._skill_digest = _taxonomy_digest(taxonomy)
        self._skill_definitions = {skill.id: skill for skill in taxonomy.skills}

    def snapshot(
        self, filters: RadarMvpFilters, *, now: datetime | None = None
    ) -> dict[str, object]:
        filters.validate()
        if filters.skill is not None and filters.skill not in self._skill_definitions:
            raise RadarMvpQueryError("unknown skill ID")
        generated = datetime.now(UTC) if now is None else now.astimezone(UTC)
        sources = self._repository.list_sources(
            now=generated,
            freshness_target_seconds=self._freshness_target_seconds,
        )
        all_source_by_key = {source.key: source for source in sources}
        if filters.source is not None and filters.source not in all_source_by_key:
            raise RadarMvpQueryError("unknown source key")
        eligible_sources = tuple(
            source for source in sources if not filters.enabled_only or source.enabled
        )
        eligible_keys = tuple(source.key for source in eligible_sources)
        panel_identity, panel_error = self._panel_identity()
        company_key_by_source = (
            {source.source_key: source.company_key for source in panel_identity.sources}
            if panel_identity is not None
            else {}
        )
        current_source_keys = (filters.source,) if filters.source is not None else eligible_keys
        base_jobs = self._repository.list_jobs(
            enabled_only=filters.enabled_only,
            include_inactive=filters.include_inactive,
            source_keys=current_source_keys,
            company_key_by_source=company_key_by_source,
        )
        analyzed = {job.id: self._analyze(job.title, job.description) for job in base_jobs}
        base_unknown = sum(analyzed[job.id][0].occupation == "unknown" for job in base_jobs)
        base_missing = sum(not job.description.strip() for job in base_jobs)

        selected: list[RadarMvpJob] = []
        for job in base_jobs:
            assignment, evidence = analyzed[job.id]
            if filters.q is not None and not _query_text(job, filters.q):
                continue
            if filters.occupation is not None and assignment.occupation != filters.occupation:
                continue
            if filters.specialty is not None and assignment.specialty != filters.specialty:
                continue
            skill_ids = {item.skill_id for item in evidence}
            if filters.skill is not None and filters.skill not in skill_ids:
                continue
            selected.append(job)

        selected.sort(
            key=lambda job: (
                -job.last_seen_at.astimezone(UTC).timestamp(),
                job.source_key,
                job.title.casefold(),
                str(job.id),
            )
        )
        total = len(selected)
        page_start = (filters.page - 1) * filters.page_size
        page_jobs = selected[page_start : page_start + filters.page_size]
        selected_sources = {job.source_key for job in selected}
        selected_companies = {job.company_key for job in selected}
        occupation_counts = Counter(analyzed[job.id][0].occupation for job in selected)
        technical_jobs = [
            job for job in selected if analyzed[job.id][0].occupation == "technical_engineering"
        ]
        specialty_counts = Counter(
            analyzed[job.id][0].specialty
            for job in technical_jobs
            if analyzed[job.id][0].specialty is not None
        )
        skill_counts: Counter[str] = Counter()
        for job in selected:
            skill_counts.update({item.skill_id for item in analyzed[job.id][1]})
        skill_rows: list[_Facet] = [
            {
                "id": skill_id,
                "label": definition.label,
                "category": definition.category,
                "count": skill_counts.get(skill_id, 0),
                "share": _ratio(skill_counts.get(skill_id, 0), total),
            }
            for skill_id, definition in self._skill_definitions.items()
        ]
        skill_rows.sort(
            key=lambda row: (
                -int(row["count"]),
                str(row["label"]).casefold(),
                str(row["id"]),
            )
        )
        occupations: list[_Facet] = [
            {
                "id": key,
                "label": OCCUPATION_LABELS[key],
                "count": occupation_counts.get(key, 0),
                "share": _ratio(occupation_counts.get(key, 0), total),
            }
            for key in OCCUPATIONS
        ]
        occupations.sort(
            key=lambda row: (
                -int(row["count"]),
                str(row["label"]).casefold(),
                str(row["id"]),
            )
        )
        specialties: list[_Facet] = [
            {
                "id": key,
                "label": SPECIALTY_LABELS[key],
                "count": specialty_counts.get(key, 0),
                "share": _ratio(specialty_counts.get(key, 0), len(technical_jobs)),
            }
            for key in SPECIALTIES
        ]
        specialties.sort(
            key=lambda row: (
                -int(row["count"]),
                str(row["label"]).casefold(),
                str(row["id"]),
            )
        )
        source_roster = [self._source_payload(source) for source in eligible_sources]
        missing_selected = sum(not job.description.strip() for job in selected)
        unknown_selected = sum(analyzed[job.id][0].occupation == "unknown" for job in selected)
        stale_count = sum(source.stale for source in eligible_sources)
        quality_issues: list[str] = []
        if stale_count:
            quality_issues.append("source_stale")
        if (
            _ratio(base_unknown, len(base_jobs)) is not None
            and base_unknown / len(base_jobs) > 0.20
        ):
            quality_issues.append("occupation_unknown_above_reference")
        if base_missing:
            quality_issues.append("description_missing")

        history = self._history(
            identity=panel_identity,
            identity_error=panel_error,
            sources=sources,
            eligible_sources=eligible_sources,
            filters=filters,
            now=generated,
        )
        input_rows = [
            {
                "source_key": job.source_key,
                "external_id": job.external_id,
                "content_hash": job.content_hash,
                "title": job.title,
                "description": job.description,
                "last_seen_at_utc": _dt(job.last_seen_at),
                "status": job.status,
            }
            for job in sorted(base_jobs, key=lambda item: (item.source_key, item.external_id))
        ]
        input_digest = content_hash(
            {
                "schema": MVP_SCHEMA_VERSION,
                "role_contract_version": ROLE_CONTRACT_VERSION,
                "skill_taxonomy_digest": self._skill_digest,
                "panel_manifest_digest": (
                    None if panel_identity is None else panel_identity.manifest_digest
                ),
                "filters": asdict(filters),
                "rows": input_rows,
            }
        )
        return {
            "schema": MVP_SCHEMA_VERSION,
            "generated_at_utc": generated.isoformat(),
            "data_as_of_utc": _dt(max((job.last_seen_at for job in base_jobs), default=None)),
            "mode": "saved_inventory",
            "filters": asdict(filters),
            "summary": {
                "posting_count": total,
                "source_count": len(selected_sources),
                "company_count": len(selected_companies),
                "technical_engineering_count": len(technical_jobs),
                "unknown_occupation_count": unknown_selected,
                "missing_description_count": missing_selected,
            },
            "quality": {
                "state": "evidence_limited" if quality_issues else "ready",
                "issues": quality_issues,
                "stale_source_count": stale_count,
                "base_source_cohort_posting_count": len(base_jobs),
                "base_unknown_occupation_count": base_unknown,
                "base_unknown_occupation_share": _ratio(base_unknown, len(base_jobs)),
                "base_missing_description_count": base_missing,
                "base_missing_description_share": _ratio(base_missing, len(base_jobs)),
                "selected_unknown_occupation_share": _ratio(unknown_selected, total),
                "selected_missing_description_share": _ratio(missing_selected, total),
            },
            "occupations": occupations,
            "specialties": specialties,
            "skills": skill_rows,
            "sources": source_roster,
            "filter_options": {
                "occupations": [
                    {"id": key, "label": OCCUPATION_LABELS[key]} for key in OCCUPATIONS
                ],
                "specialties": [{"id": key, "label": SPECIALTY_LABELS[key]} for key in SPECIALTIES],
                "skills": [
                    {
                        "id": skill_id,
                        "label": definition.label,
                        "category": definition.category,
                    }
                    for skill_id, definition in sorted(
                        self._skill_definitions.items(),
                        key=lambda item: (item[1].label.casefold(), item[0]),
                    )
                ],
                "sources": [
                    {
                        "key": source.key,
                        "company_name": source.company_name,
                        "enabled": source.enabled,
                    }
                    for source in eligible_sources
                ],
            },
            "jobs": {
                "items": [self._job_summary(job, analyzed[job.id]) for job in page_jobs],
                "total": total,
                "page": filters.page,
                "page_size": filters.page_size,
                "total_pages": (total + filters.page_size - 1) // filters.page_size,
            },
            "history": history,
            "provenance": {
                "classifier_version": LEGACY_CLASSIFIER_VERSION,
                "role_contract_version": ROLE_CONTRACT_VERSION,
                "skill_taxonomy_version": self._taxonomy.version,
                "skill_taxonomy_digest": self._skill_digest,
                "panel_version": None if panel_identity is None else panel_identity.panel_version,
                "panel_manifest_digest": (
                    None if panel_identity is None else panel_identity.manifest_digest
                ),
                "input_digest": input_digest,
            },
        }

    def detail(self, job_id: UUID) -> dict[str, object] | None:
        identity, _error = self._panel_identity()
        company_keys = (
            {source.source_key: source.company_key for source in identity.sources}
            if identity is not None
            else {}
        )
        job = self._repository.get_job(job_id, company_key_by_source=company_keys)
        if job is None:
            return None
        assignment, skill_evidence = self._analyze(job.title, job.description)
        role_evidence = [
            {
                "axis": item.axis,
                "source": item.source,
                "rule_id": item.rule_id,
                "matched_text": item.matched_text,
            }
            for item in assignment.evidence
        ]
        skill_rows = [
            {
                "id": item.skill_id,
                "label": self._skill_definitions[item.skill_id].label,
                "category": self._skill_definitions[item.skill_id].category,
                "matched_text": item.matched_text,
                "section": item.section,
                "method": item.method,
                "taxonomy_version": item.taxonomy_version,
            }
            for item in skill_evidence
        ]
        return {
            "schema": MVP_DETAIL_SCHEMA_VERSION,
            "job": self._job_summary(job, (assignment, skill_evidence)),
            "description": job.description,
            "role_evidence": role_evidence,
            "skill_evidence": skill_rows,
            "provenance": {
                "classifier_version": LEGACY_CLASSIFIER_VERSION,
                "role_contract_version": ROLE_CONTRACT_VERSION,
                "skill_taxonomy_version": self._taxonomy.version,
                "skill_taxonomy_digest": self._skill_digest,
                "input_digest": content_hash(
                    {
                        "source_key": job.source_key,
                        "external_id": job.external_id,
                        "title": job.title,
                        "description": job.description,
                        "content_hash": job.content_hash,
                        "role_contract_version": ROLE_CONTRACT_VERSION,
                        "skill_taxonomy_digest": self._skill_digest,
                    }
                ),
            },
        }

    def methodology(self) -> dict[str, object]:
        return radar_mvp_methodology(self._taxonomy)

    def _panel_identity(
        self,
    ) -> tuple[RadarPanelManifestIdentity | None, str | None]:
        try:
            return RadarPanelManifest.load_identity(_PANEL_PATH), None
        except (ConfigurationError, OSError, ValueError):
            return None, "panel_manifest_unavailable"

    def _history(
        self,
        *,
        identity: RadarPanelManifestIdentity | None,
        identity_error: str | None,
        sources: tuple[RadarMvpSource, ...],
        eligible_sources: tuple[RadarMvpSource, ...],
        filters: RadarMvpFilters,
        now: datetime,
    ) -> dict[str, object]:
        occupations = tuple((key, OCCUPATION_LABELS[key]) for key in OCCUPATIONS)
        specialties = tuple((key, SPECIALTY_LABELS[key]) for key in SPECIALTIES)
        skills = tuple(
            (skill.id, skill.label)
            for skill in sorted(self._taxonomy.skills, key=lambda item: item.id)
        )
        selected_source_keys: tuple[str, ...]
        if identity is None:
            selected_source_keys = ()
            inputs = RadarMvpHistoryInputs(None, (), identity_error)
        else:
            panel_keys = tuple(source.source_key for source in identity.sources)
            if filters.source is None:
                # History belongs to its registered panel; current source enablement
                # and active status do not retroactively remove a captured board.
                selected_source_keys = panel_keys
            elif filters.source in panel_keys:
                selected_source_keys = (filters.source,)
            else:
                selected_source_keys = ()
                inputs = RadarMvpHistoryInputs(None, (), "source_outside_panel")
            if selected_source_keys:
                inputs = self._repository.load_history_inputs(
                    identity=identity,
                    cutoff=now,
                    lookback_days=360,
                )
        return compare_mvp_history(
            panel=inputs.panel,
            scans=inputs.scans,
            input_reason=inputs.reason,
            cutoff=now,
            period_days=filters.period_days,
            selected_source_keys=selected_source_keys,
            sources=sources,
            q=filters.q,
            selected_occupation=filters.occupation,
            selected_specialty=filters.specialty,
            selected_skill=filters.skill,
            occupations=occupations,
            specialties=specialties,
            skills=skills,
            analyze=self._analyze,
        )

    def _analyze(self, title: str, description: str) -> PostingAnalysis:
        return (
            _role_assignment_cached(title, description),
            _skill_evidence_cached(self._extractor, title, description),
        )

    @staticmethod
    def _source_payload(source: RadarMvpSource) -> dict[str, object]:
        return {
            "key": source.key,
            "company_name": source.company_name,
            "enabled": source.enabled,
            "last_success_at_utc": _dt(source.last_success_at),
            "status": source.status,
            "stale": source.stale,
        }

    def _job_summary(
        self,
        job: RadarMvpJob,
        analysis: PostingAnalysis,
    ) -> dict[str, object]:
        assignment, evidence = analysis
        evidence_by_skill: dict[str, list[dict[str, object]]] = {}
        for item in evidence:
            evidence_by_skill.setdefault(item.skill_id, []).append(
                {
                    "matched_text": item.matched_text,
                    "section": item.section,
                }
            )
        return {
            "id": str(job.id),
            "title": job.title,
            "company_name": job.company_name,
            "source_key": job.source_key,
            "source_url": _safe_url(job.source_url),
            "apply_url": _safe_url(job.apply_url),
            "last_seen_at_utc": _dt(job.last_seen_at),
            "status": job.status,
            "occupation": assignment.occupation,
            "specialty": assignment.specialty,
            "specialty_state": assignment.specialty_state,
            "occupation_reason": assignment.reason,
            "skills": [
                {
                    "id": skill_id,
                    "label": self._skill_definitions[skill_id].label,
                }
                for skill_id in sorted(evidence_by_skill)
            ],
            "description_excerpt": " ".join(job.description.split())[:280],
        }


def radar_mvp_methodology(taxonomy: SkillTaxonomy) -> dict[str, object]:
    """Return static methodology without opening persistence."""
    return {
        "schema": MVP_METHODOLOGY_SCHEMA_VERSION,
        "mode": "saved_inventory",
        "versions": {
            "legacy_role_classifier": LEGACY_CLASSIFIER_VERSION,
            "role_contract": ROLE_CONTRACT_VERSION,
            "skill_taxonomy": taxonomy.version,
            "history_metric_contract": "1.0.0",
        },
        "definitions": {
            "posting_unit": "One source board plus external posting ID.",
            "occupation_share": "Detected occupation postings divided by selected postings.",
            "specialty_share": (
                "Detected specialty postings divided by detected technical-engineering postings."
            ),
            "skill_share": (
                "Distinct postings with a literal taxonomy mention divided by selected postings."
            ),
            "skill_semantics": (
                "Mentions are not required-skill prevalence, demand, or evidence of absence "
                "when not mentioned."
            ),
            "history": (
                "Paired immutable scan windows; current status and mutable job_skills are "
                "not used for historical counts."
            ),
            "history_source_scope": (
                "Registered panel sources ignore current enabled/active status; an explicit "
                "source filter narrows the panel."
            ),
            "direction": (
                "Up/down means a change beyond a descriptive ±1 percentage-point band; "
                "this is not statistical significance."
            ),
            "role_labels": (
                "Classifier labels are deterministic rule outputs, not ground truth or "
                "an accuracy estimate."
            ),
            "freshness": (
                "Stored active status does not guarantee freshness; source last-success "
                "and stale status are shown separately."
            ),
        },
        "limitations": [
            "Current results describe saved inventory, not the full external labor market.",
            "Unknown occupations and missing descriptions remain visible in coverage fields.",
            (
                "Historical comparisons are null/insufficient until their own matched-panel, "
                "coverage, and sample gates pass."
            ),
        ],
    }
