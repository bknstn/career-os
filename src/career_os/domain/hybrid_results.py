"""Strict, transport-neutral contracts for the H1 hybrid market result.

The API and Markdown layers consume :class:`HybridResult` only.  In
particular, this module deliberately contains no SQLAlchemy, HTTP, or file
I/O.  Its validation is the reconciliation layer for invariants that JSON
Schema cannot express (denominator identity, evidence unions, and gate state).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import UUID

if TYPE_CHECKING:
    from career_os.config.markets import MarketFilters
    from career_os.config.profiles import Profile

CONTRACT_VERSION = "h1.hybrid-result.v1"
GATE_IDS = (
    "reviewed_eligible_jobs",
    "represented_companies",
    "max_company_concentration",
    "demonstrated_eligibility_false_positives",
    "skill_semantics",
    "compensation_reconciled",
)
_OBSERVED_VALUE_KEYS = frozenset(
    {
        "matching_job_count",
        "company_count",
        "max_company_concentration",
        "reviewed_eligible_job_count",
        "represented_company_count",
        "demonstrated_eligibility_false_positive_count",
        "skill_semantics",
        "compensation_reconciled",
        "source_coverage",
        "compensation_coverage",
        "collection_cutoff",
    }
)
_SKILL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class HybridScope:
    """Explicit H1 boundary; target-universe data is intentionally absent."""

    name: str
    kind: str = "configured_source_panel"
    target_universe: None = None

    def __post_init__(self) -> None:
        _nonblank(self.name, "scope.name")
        if self.kind != "configured_source_panel":
            raise ValueError("H1 scope kind must be configured_source_panel")


def _nonblank(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-blank")


def _ids(value: tuple[UUID, ...], field: str) -> None:
    if any(not isinstance(item, UUID) for item in value):
        raise ValueError(f"{field} must contain UUIDs")
    if value != tuple(sorted(set(value))):
        raise ValueError(f"{field} must contain unique UUIDs in stable order")


def _strings(value: tuple[str, ...], field: str) -> None:
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must contain non-blank strings")
    if value != tuple(sorted(set(value))):
        raise ValueError(f"{field} must contain unique strings in stable order")


def _share(numerator: int, denominator: int) -> Decimal | None:
    return Decimal(numerator) / Decimal(denominator) if denominator else None


def _decimal(value: Decimal | None, field: str) -> None:
    if value is not None and (
        not isinstance(value, Decimal) or not value.is_finite() or value < Decimal(0)
    ):
        raise ValueError(f"{field} must be a finite non-negative Decimal or None")


def _timestamp(value: datetime, field: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _safe_url(value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError("url must be a string or None")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as error:
        raise ValueError("url must be a safe absolute HTTP(S) URL or None") from error
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or "\\" in value
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        raise ValueError("url must be a safe absolute HTTP(S) URL or None")


@dataclass(frozen=True, slots=True)
class QualityReviewInput:
    """Optional explicit review evidence supplied by CLI or API."""

    ref: str
    reviewed_job_ids: tuple[UUID, ...] = ()
    false_positive_job_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        _nonblank(self.ref, "ref")
        _ids(self.reviewed_job_ids, "reviewed_job_ids")
        _ids(self.false_positive_job_ids, "false_positive_job_ids")
        if not set(self.false_positive_job_ids) <= set(self.reviewed_job_ids):
            raise ValueError("false_positive_job_ids must be a subset of reviewed_job_ids")


@dataclass(frozen=True, slots=True)
class HybridSource:
    key: str
    kind: str
    url: str | None

    def __post_init__(self) -> None:
        _nonblank(self.key, "source.key")
        _nonblank(self.kind, "source.kind")
        _safe_url(self.url)


@dataclass(frozen=True, slots=True)
class HybridFreshness:
    first_seen_at: datetime
    last_seen_at: datetime

    def __post_init__(self) -> None:
        _timestamp(self.first_seen_at, "first_seen_at")
        _timestamp(self.last_seen_at, "last_seen_at")
        if self.last_seen_at < self.first_seen_at:
            raise ValueError("last_seen_at must not precede first_seen_at")


@dataclass(frozen=True, slots=True)
class HybridLocation:
    ordinal: int
    city: str | None
    region: str | None
    country_code: str | None
    is_primary: bool
    confidence: Decimal

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ValueError("ordinal must be a non-negative integer")
        if type(self.is_primary) is not bool:
            raise ValueError("is_primary must be a bool")
        _decimal(self.confidence, "confidence")
        if self.confidence > Decimal(1):
            raise ValueError("confidence must be at most one")
        if self.country_code is not None and (
            len(self.country_code) != 2
            or not self.country_code.isascii()
            or not self.country_code.isupper()
        ):
            raise ValueError("country_code must be an uppercase ISO alpha-2 code or None")


@dataclass(frozen=True, slots=True)
class HybridSkillMention:
    id: UUID
    skill_id: str
    matched_text: str
    section: str | None
    method: str
    confidence: Decimal
    taxonomy_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID):
            raise ValueError("skill mention id must be a UUID")
        if not _SKILL_ID_PATTERN.fullmatch(self.skill_id):
            raise ValueError("skill_id must be lowercase snake_case")
        _nonblank(self.matched_text, "matched_text")
        if self.section not in {"title", "requirements", "responsibilities", "other", None}:
            raise ValueError("invalid skill mention section")
        if self.method not in {"alias", "exact", "pattern", "manual", "llm"}:
            raise ValueError("invalid skill mention method")
        _nonblank(self.taxonomy_version, "taxonomy_version")
        _decimal(self.confidence, "confidence")
        if self.confidence > Decimal(1):
            raise ValueError("confidence must be at most one")


@dataclass(frozen=True, slots=True)
class HybridJobCompensation:
    component_id: UUID
    component_type: str
    min_value: Decimal | None
    max_value: Decimal | None
    currency: str | None
    interval: str | None
    location_tier: str | None
    source_method: str

    def __post_init__(self) -> None:
        if not isinstance(self.component_id, UUID):
            raise ValueError("component_id must be a UUID")
        _nonblank(self.component_type, "component_type")
        _nonblank(self.source_method, "source_method")
        _decimal(self.min_value, "min_value")
        _decimal(self.max_value, "max_value")
        if self.min_value is None and self.max_value is None:
            raise ValueError("a compensation component requires a numeric value")
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError("min_value must not exceed max_value")
        if self.currency is not None and (
            len(self.currency) != 3 or not self.currency.isascii() or not self.currency.isupper()
        ):
            raise ValueError("currency must be an uppercase ISO currency code or None")


@dataclass(frozen=True, slots=True)
class HybridOpportunity:
    job_id: UUID
    company_name: str
    title: str
    source: HybridSource
    freshness: HybridFreshness
    locations: tuple[HybridLocation, ...]
    remote_scope: str | None
    skill_mentions: tuple[HybridSkillMention, ...]
    compensation: tuple[HybridJobCompensation, ...]
    status: str
    role_family: str | None
    seniority: str | None
    workplace_type: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, UUID):
            raise ValueError("job_id must be a UUID")
        _nonblank(self.company_name, "company_name")
        _nonblank(self.title, "title")
        if self.status not in {"active", "inactive"}:
            raise ValueError("status must be active or inactive")
        ordinals = tuple(location.ordinal for location in self.locations)
        if ordinals != tuple(sorted(set(ordinals))):
            raise ValueError("locations must be unique and ordered by ordinal")
        mention_ids = tuple(item.id for item in self.skill_mentions)
        if mention_ids != tuple(sorted(set(mention_ids))):
            raise ValueError("skill_mentions must be unique and stable by evidence ID")
        component_ids = tuple(item.component_id for item in self.compensation)
        if component_ids != tuple(sorted(set(component_ids))):
            raise ValueError("compensation must be unique and stable by component ID")


@dataclass(frozen=True, slots=True)
class HybridBucket:
    value: str
    job_ids: tuple[UUID, ...]
    denominator_job_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        _nonblank(self.value, "value")
        _ids(self.job_ids, "job_ids")
        _ids(self.denominator_job_ids, "denominator_job_ids")
        if not set(self.job_ids) <= set(self.denominator_job_ids):
            raise ValueError("job_ids must be a subset of denominator_job_ids")

    @property
    def job_count(self) -> int:
        return len(self.job_ids)

    @property
    def denominator(self) -> int:
        return len(self.denominator_job_ids)

    @property
    def job_share(self) -> Decimal | None:
        return _share(self.job_count, self.denominator)


@dataclass(frozen=True, slots=True)
class HybridConcentration:
    company_name: str
    job_ids: tuple[UUID, ...]
    denominator_job_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        _nonblank(self.company_name, "company_name")
        _ids(self.job_ids, "job_ids")
        _ids(self.denominator_job_ids, "denominator_job_ids")
        if not set(self.job_ids) <= set(self.denominator_job_ids):
            raise ValueError("job_ids must be a subset of denominator_job_ids")

    @property
    def job_count(self) -> int:
        return len(self.job_ids)

    @property
    def job_share(self) -> Decimal | None:
        return _share(self.job_count, len(self.denominator_job_ids))


@dataclass(frozen=True, slots=True)
class HybridSkill:
    skill_id: str
    label: str
    job_ids: tuple[UUID, ...]
    denominator_job_ids: tuple[UUID, ...]
    company_names: tuple[str, ...]
    denominator_company_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _SKILL_ID_PATTERN.fullmatch(self.skill_id):
            raise ValueError("skill_id must be lowercase snake_case")
        _nonblank(self.label, "label")
        _ids(self.job_ids, "skill.job_ids")
        _ids(self.denominator_job_ids, "skill.denominator_job_ids")
        _strings(self.company_names, "company_names")
        _strings(self.denominator_company_names, "denominator_company_names")
        if not set(self.job_ids) <= set(self.denominator_job_ids):
            raise ValueError("skill job_ids must be a subset of denominator_job_ids")
        if not set(self.company_names) <= set(self.denominator_company_names):
            raise ValueError("skill company_names must be a subset of denominator_company_names")

    @property
    def semantics(self) -> str:
        return "mention"

    @property
    def job_count(self) -> int:
        return len(self.job_ids)

    @property
    def job_share(self) -> Decimal | None:
        return _share(self.job_count, len(self.denominator_job_ids))

    @property
    def company_count(self) -> int:
        return len(self.company_names)

    @property
    def company_share(self) -> Decimal | None:
        return _share(self.company_count, len(self.denominator_company_names))


@dataclass(frozen=True, slots=True)
class HybridCompensation:
    component_type: str
    currency: str
    interval: str
    location_tier: str | None
    component_ids: tuple[UUID, ...]
    job_ids: tuple[UUID, ...]
    denominator_job_ids: tuple[UUID, ...]
    min_value: Decimal | None
    max_value: Decimal | None

    def __post_init__(self) -> None:
        _nonblank(self.component_type, "component_type")
        if len(self.currency) != 3 or not self.currency.isascii() or not self.currency.isupper():
            raise ValueError("currency must be an uppercase ISO currency code")
        _nonblank(self.interval, "interval")
        _ids(self.component_ids, "component_ids")
        _ids(self.job_ids, "compensation.job_ids")
        _ids(self.denominator_job_ids, "compensation.denominator_job_ids")
        if not self.component_ids or not self.job_ids:
            raise ValueError("a compensation group requires component and job IDs")
        if not set(self.job_ids) <= set(self.denominator_job_ids):
            raise ValueError("compensation job_ids must be a subset of denominator_job_ids")
        if self.min_value is None and self.max_value is None:
            raise ValueError("a compensation group requires a numeric value")
        _decimal(self.min_value, "min_value")
        _decimal(self.max_value, "max_value")
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError("min_value must not exceed max_value")

    @property
    def component_count(self) -> int:
        return len(self.component_ids)

    @property
    def job_count(self) -> int:
        return len(self.job_ids)

    @property
    def job_share(self) -> Decimal | None:
        return _share(self.job_count, len(self.denominator_job_ids))


@dataclass(frozen=True, slots=True)
class HybridSourceCoverage:
    configured_source_keys: tuple[str, ...]
    matching_source_keys: tuple[str, ...]
    job_ids_by_source: tuple[tuple[str, tuple[UUID, ...]], ...]

    def __post_init__(self) -> None:
        _strings(self.configured_source_keys, "configured_source_keys")
        _strings(self.matching_source_keys, "matching_source_keys")
        if not set(self.matching_source_keys) <= set(self.configured_source_keys):
            raise ValueError("matching source keys must be configured source keys")
        source_keys = tuple(key for key, _ in self.job_ids_by_source)
        if source_keys != self.configured_source_keys:
            raise ValueError("job_ids_by_source must contain every configured source in order")
        nonempty_keys: list[str] = []
        seen_job_ids: set[UUID] = set()
        for key, job_ids in self.job_ids_by_source:
            _nonblank(key, "source_key")
            _ids(job_ids, f"job_ids_by_source[{key}]")
            if job_ids:
                nonempty_keys.append(key)
            if seen_job_ids.intersection(job_ids):
                raise ValueError("a job ID cannot belong to multiple sources")
            seen_job_ids.update(job_ids)
        if tuple(nonempty_keys) != self.matching_source_keys:
            raise ValueError("matching source keys must match non-empty source rows")

    @property
    def configured_source_count(self) -> int:
        return len(self.configured_source_keys)

    @property
    def matching_source_count(self) -> int:
        return len(self.matching_source_keys)

    @property
    def source_share(self) -> Decimal | None:
        return _share(self.matching_source_count, self.configured_source_count)


@dataclass(frozen=True, slots=True)
class HybridSample:
    job_ids: tuple[UUID, ...]
    company_names: tuple[str, ...]
    max_company_concentration: HybridConcentration | None
    source_coverage: HybridSourceCoverage
    collection_cutoff: datetime | None
    skill_denominator_job_ids: tuple[UUID, ...]
    compensation_job_ids: tuple[UUID, ...]

    @property
    def job_count(self) -> int:
        return len(self.job_ids)

    @property
    def company_count(self) -> int:
        return len(self.company_names)

    @property
    def skill_denominator_count(self) -> int:
        return len(self.skill_denominator_job_ids)

    @property
    def compensation_job_count(self) -> int:
        return len(self.compensation_job_ids)

    def __post_init__(self) -> None:
        _ids(self.job_ids, "sample.job_ids")
        _strings(self.company_names, "sample.company_names")
        _ids(self.skill_denominator_job_ids, "skill_denominator_job_ids")
        _ids(self.compensation_job_ids, "compensation_job_ids")
        if not set(self.skill_denominator_job_ids) <= set(self.job_ids):
            raise ValueError("skill denominator must be a subset of sample jobs")
        if not set(self.compensation_job_ids) <= set(self.job_ids):
            raise ValueError("compensation jobs must be a subset of sample jobs")
        source_job_ids = {
            job_id
            for _, source_ids in self.source_coverage.job_ids_by_source
            for job_id in source_ids
        }
        if source_job_ids != set(self.job_ids):
            raise ValueError("source coverage job IDs must exactly equal sample job IDs")
        if self.max_company_concentration is not None and set(
            self.max_company_concentration.denominator_job_ids
        ) != set(self.job_ids):
            raise ValueError("concentration denominator must equal sample jobs")
        if self.collection_cutoff is not None:
            _timestamp(self.collection_cutoff, "collection_cutoff")


@dataclass(frozen=True, slots=True)
class HybridProfileMentions:
    profile_skill_ids: tuple[str, ...]
    covered_skill_ids: tuple[str, ...]
    neutral_skill_ids: tuple[str, ...]
    uncovered_mentions: tuple[HybridSkill, ...]

    def __post_init__(self) -> None:
        for field in ("profile_skill_ids", "covered_skill_ids", "neutral_skill_ids"):
            _strings(getattr(self, field), field)
        uncovered_ids = tuple(item.skill_id for item in self.uncovered_mentions)
        if uncovered_ids != tuple(sorted(set(uncovered_ids))):
            raise ValueError("uncovered_mentions must be unique and stable by skill ID")
        if set(self.covered_skill_ids) & set(self.neutral_skill_ids):
            raise ValueError("covered and neutral profile skills must be disjoint")
        if set(self.covered_skill_ids) | set(self.neutral_skill_ids) != set(self.profile_skill_ids):
            raise ValueError("covered and neutral profile skills must partition profile skills")
        if set(uncovered_ids) & set(self.profile_skill_ids):
            raise ValueError("uncovered mentions must not include profile skills")


@dataclass(frozen=True, slots=True)
class HybridObservations:
    sample: HybridSample
    companies: tuple[HybridBucket, ...]
    skills: tuple[HybridSkill, ...]
    seniorities: tuple[HybridBucket, ...]
    workplace_types: tuple[HybridBucket, ...]
    countries: tuple[HybridBucket, ...]
    compensation: tuple[HybridCompensation, ...]
    profile_mentions: HybridProfileMentions

    @property
    def scope(self) -> str:
        return "configured_source_panel"

    def __post_init__(self) -> None:
        matching = self.sample.job_ids
        for field in ("companies", "seniorities", "workplace_types"):
            if any(item.denominator_job_ids != matching for item in getattr(self, field)):
                raise ValueError(f"{field} must use sample job IDs as denominator")
        if any(
            item.denominator_job_ids != self.sample.skill_denominator_job_ids
            for item in self.skills
        ):
            raise ValueError("skills must use skill denominator job IDs")
        if any(item.denominator_job_ids != matching for item in self.compensation):
            raise ValueError("compensation must use sample job IDs as denominator")
        _bucket_ids(self.companies, "companies")
        _bucket_ids(self.skills, "skills", skill=True)
        _bucket_ids(self.seniorities, "seniorities")
        _bucket_ids(self.workplace_types, "workplace_types")
        _bucket_ids(self.countries, "countries")
        if any(not set(item.denominator_job_ids) <= set(matching) for item in self.countries):
            raise ValueError("country denominators must be subsets of sample job IDs")


def _bucket_ids(
    values: tuple[HybridBucket | HybridSkill, ...], field: str, *, skill: bool = False
) -> None:
    if skill:
        keys = tuple(item.skill_id for item in values if isinstance(item, HybridSkill))
    else:
        keys = tuple(item.value for item in values if isinstance(item, HybridBucket))
    if keys != tuple(sorted(set(keys))):
        raise ValueError(f"{field} must be unique and stable")


@dataclass(frozen=True, slots=True)
class HybridProvenance:
    kind: str
    ref: str
    reviewed_job_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if self.kind not in {"result_derived", "contract_rule", "review_artifact"}:
            raise ValueError("invalid provenance kind")
        _nonblank(self.ref, "provenance.ref")
        _ids(self.reviewed_job_ids, "provenance.reviewed_job_ids")


@dataclass(frozen=True, slots=True)
class HybridEvaluation:
    basis: str
    provenance: HybridProvenance

    def __post_init__(self) -> None:
        if self.basis not in {"computed", "contract_invariant", "manual_review"}:
            raise ValueError("invalid evaluation basis")
        expected = {
            "computed": "result_derived",
            "contract_invariant": "contract_rule",
            "manual_review": "review_artifact",
        }[self.basis]
        if self.provenance.kind != expected:
            raise ValueError("evaluation basis and provenance kind must agree")


@dataclass(frozen=True, slots=True)
class HybridGate:
    gate_id: str
    status: str
    operator: str
    threshold: object
    observed: object
    unit: str
    evidence_job_ids: tuple[UUID, ...]
    evidence_company_names: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonblank(self.gate_id, "gate_id")
        if self.status not in {"passed", "failed", "not_evaluable"}:
            raise ValueError("invalid gate status")
        _nonblank(self.operator, "operator")
        _nonblank(self.unit, "unit")
        _ids(self.evidence_job_ids, "gate.evidence_job_ids")
        _strings(self.evidence_company_names, "gate.evidence_company_names")


@dataclass(frozen=True, slots=True)
class HybridQuality:
    state: str
    gates: tuple[HybridGate, ...]
    failed_gates: tuple[str, ...]
    evaluation_provenance: tuple[tuple[str, HybridEvaluation], ...]
    observed_values: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_values", MappingProxyType(dict(self.observed_values)))
        if set(self.observed_values) != _OBSERVED_VALUE_KEYS:
            raise ValueError("observed_values must contain the fixed H1 diagnostic keys")
        for key in (
            "matching_job_count",
            "company_count",
            "reviewed_eligible_job_count",
            "represented_company_count",
            "demonstrated_eligibility_false_positive_count",
        ):
            value = self.observed_values[key]
            if type(value) is not int or value < 0:
                raise ValueError(f"observed_values.{key} must be a non-negative integer")
        for key in (
            "max_company_concentration",
            "source_coverage",
            "compensation_coverage",
        ):
            value = self.observed_values[key]
            if value is not None:
                if not isinstance(value, Decimal):
                    raise ValueError(f"observed_values.{key} must be a Decimal or null")
                _decimal(value, f"observed_values.{key}")
                if value > Decimal(1):
                    raise ValueError(f"observed_values.{key} must be at most one")
        if self.observed_values["skill_semantics"] != "mentions_only":
            raise ValueError("observed_values.skill_semantics must be mentions_only")
        if type(self.observed_values["compensation_reconciled"]) is not bool:
            raise ValueError("observed_values.compensation_reconciled must be a bool")
        collection_cutoff = self.observed_values["collection_cutoff"]
        if collection_cutoff is not None:
            if not isinstance(collection_cutoff, datetime):
                raise ValueError("observed_values.collection_cutoff must be a timestamp or null")
            _timestamp(collection_cutoff, "observed_values.collection_cutoff")
        if self.state not in {"accepted", "evidence_limited"}:
            raise ValueError("invalid quality state")
        if tuple(gate.gate_id for gate in self.gates) != GATE_IDS:
            raise ValueError("quality gates must use the fixed H1 order")
        if tuple(key for key, _ in self.evaluation_provenance) != GATE_IDS:
            raise ValueError("quality provenance must use the fixed H1 order")
        provenance = dict(self.evaluation_provenance)
        for gate in self.gates:
            evaluation = provenance[gate.gate_id]
            if gate.status == "not_evaluable" and evaluation.provenance.reviewed_job_ids:
                raise ValueError("not_evaluable gates cannot claim reviewed job evidence")
            if (
                gate.gate_id == "demonstrated_eligibility_false_positives"
                and gate.status == "passed"
                and gate.observed == 0
                and not evaluation.provenance.reviewed_job_ids
            ):
                raise ValueError("a passed false-positive gate requires reviewed job provenance")
        expected_failed = tuple(gate.gate_id for gate in self.gates if gate.status != "passed")
        if self.failed_gates != expected_failed:
            raise ValueError("failed_gates must mirror non-passed gates")
        expected_state = "accepted" if not expected_failed else "evidence_limited"
        if self.state != expected_state:
            raise ValueError("quality state must follow gate statuses")
        if any(not isinstance(value, str) or not value.strip() for value in self.failed_gates):
            raise ValueError("failed_gates must be non-blank IDs")

    @property
    def claim_policy(self) -> Mapping[str, str]:
        """The fixed H1 claim policy as an immutable mapping."""
        return MappingProxyType(
            {
                "opportunities": "available",
                "panel_observations": "panel_bounded" if self.state == "accepted" else "qualified",
                "broad_market_claims": "disabled",
                "skill_semantics": "mentions_only",
                "fit_probability": "not_provided",
            }
        )

    @property
    def compensation_coverage(self) -> Decimal | None:
        value = self.observed_values["compensation_coverage"]
        return value if isinstance(value, Decimal) else None


@dataclass(frozen=True, slots=True)
class HybridOpportunities:
    items: tuple[HybridOpportunity, ...]

    def __post_init__(self) -> None:
        ids = tuple(item.job_id for item in self.items)
        _ids(ids, "opportunities.job_ids")

    @property
    def job_ids(self) -> tuple[UUID, ...]:
        return tuple(item.job_id for item in self.items)

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def company_count(self) -> int:
        return len({item.company_name for item in self.items})


@dataclass(frozen=True, slots=True)
class HybridResult:
    filters: MarketFilters
    profile: Profile
    quality: HybridQuality
    opportunities: HybridOpportunities
    observations: HybridObservations

    def __post_init__(self) -> None:
        ids = self.opportunities.job_ids
        sample = self.observations.sample
        if sample.job_ids != ids:
            raise ValueError("opportunities and sample job IDs must exactly match")
        if (
            tuple(sorted({item.company_name for item in self.opportunities.items}))
            != sample.company_names
        ):
            raise ValueError("sample company names must derive from opportunities")
        if len(sample.company_names) != self.opportunities.company_count:
            raise ValueError("sample company count must derive from opportunities")
        if ids:
            expected_cutoff = max(item.freshness.last_seen_at for item in self.opportunities.items)
            if sample.collection_cutoff != expected_cutoff:
                raise ValueError("collection_cutoff must equal max opportunity last_seen_at")
        elif sample.collection_cutoff is not None:
            raise ValueError("empty sample must have null collection_cutoff")
        self._validate_quality_values()
        self._validate_evidence_unions()

    @property
    def contract_version(self) -> str:
        return CONTRACT_VERSION

    @property
    def scope(self) -> HybridScope:
        return HybridScope(name=self.filters.name)

    def _validate_evidence_unions(self) -> None:
        items = self.opportunities.items
        by_skill: dict[str, set[UUID]] = {}
        skill_companies: dict[str, set[str]] = {}
        for item in items:
            for mention in item.skill_mentions:
                by_skill.setdefault(mention.skill_id, set()).add(item.job_id)
                skill_companies.setdefault(mention.skill_id, set()).add(item.company_name)
        described_companies = {
            item.company_name
            for item in items
            if item.job_id in set(self.observations.sample.skill_denominator_job_ids)
        }
        for skill in self.observations.skills:
            if set(skill.job_ids) != by_skill.get(skill.skill_id, set()):
                raise ValueError("skill aggregate does not reconcile to per-job mention evidence")
            if set(skill.company_names) != skill_companies.get(skill.skill_id, set()):
                raise ValueError("skill company breadth does not reconcile to opportunity evidence")
            if set(skill.denominator_company_names) != described_companies:
                raise ValueError("skill company denominator does not reconcile")
        if set(by_skill) != {skill.skill_id for skill in self.observations.skills}:
            raise ValueError("per-job skill evidence has no aggregate row")
        by_component: dict[UUID, UUID] = {}
        components: dict[UUID, HybridJobCompensation] = {}
        for item in items:
            for component in item.compensation:
                if component.component_id in components:
                    raise ValueError("compensation component IDs must be globally unique")
                by_component[component.component_id] = item.job_id
                components[component.component_id] = component
        grouped_components = {
            component_id
            for group in self.observations.compensation
            for component_id in group.component_ids
        }
        if grouped_components != set(components):
            raise ValueError("compensation aggregate does not reconcile to per-job components")
        for group in self.observations.compensation:
            expected_jobs = {by_component[component_id] for component_id in group.component_ids}
            if expected_jobs != set(group.job_ids):
                raise ValueError("compensation group job IDs do not reconcile")
            group_components = tuple(components[value] for value in group.component_ids)
            if any(
                component.component_type != group.component_type
                or component.currency != group.currency
                or (component.interval or "unknown") != group.interval
                or component.location_tier != group.location_tier
                for component in group_components
            ):
                raise ValueError("compensation group identity does not reconcile")
            minimums = tuple(
                value.min_value for value in group_components if value.min_value is not None
            )
            maximums = tuple(
                value.max_value for value in group_components if value.max_value is not None
            )
            if group.min_value != (min(minimums) if minimums else None) or group.max_value != (
                max(maximums) if maximums else None
            ):
                raise ValueError("compensation group range does not reconcile")
            if group.interval == "unknown" and len(group.job_ids) != 1:
                raise ValueError("unknown-interval compensation must remain grouped per job")
        if set(self.observations.sample.compensation_job_ids) != set(by_component.values()):
            raise ValueError("compensation job IDs must reconcile to component evidence")

        company_jobs = {
            company_name: {item.job_id for item in items if item.company_name == company_name}
            for company_name in self.observations.sample.company_names
        }
        if {
            bucket.value: set(bucket.job_ids) for bucket in self.observations.companies
        } != company_jobs:
            raise ValueError("company buckets must exactly partition opportunity evidence")
        concentration = self.observations.sample.max_company_concentration
        if company_jobs:
            expected_name, expected_ids = min(
                company_jobs.items(),
                key=lambda value: (-len(value[1]), value[0]),
            )
            if (
                concentration is None
                or concentration.company_name != expected_name
                or set(concentration.job_ids) != expected_ids
            ):
                raise ValueError("maximum company concentration does not reconcile")
        elif concentration is not None:
            raise ValueError("empty opportunities cannot have a concentration record")

    def _validate_quality_values(self) -> None:
        values = self.quality.observed_values
        sample = self.observations.sample
        matching_ids = set(sample.job_ids)
        matching_companies = set(sample.company_names)
        evaluations = dict(self.quality.evaluation_provenance)
        for gate in self.quality.gates:
            if not set(gate.evidence_job_ids) <= matching_ids:
                raise ValueError(f"quality gate evidence must use matching job IDs: {gate.gate_id}")
            if not set(gate.evidence_company_names) <= matching_companies:
                raise ValueError(
                    f"quality gate evidence must use matching company names: {gate.gate_id}"
                )
            reviewed_ids = evaluations[gate.gate_id].provenance.reviewed_job_ids
            if not set(reviewed_ids) <= matching_ids:
                raise ValueError(f"quality provenance must use matching job IDs: {gate.gate_id}")
        expected = {
            "matching_job_count": sample.job_count,
            "company_count": sample.company_count,
            "represented_company_count": sample.company_count,
            "collection_cutoff": sample.collection_cutoff,
            "source_coverage": sample.source_coverage.source_share,
            "compensation_coverage": (_share(sample.compensation_job_count, sample.job_count)),
            "max_company_concentration": (
                sample.max_company_concentration.job_share
                if sample.max_company_concentration is not None
                else None
            ),
        }
        for key, expected_value in expected.items():
            if values[key] != expected_value:
                raise ValueError(f"quality observed value does not reconcile for {key}")
        gates = {gate.gate_id: gate for gate in self.quality.gates}
        expected_gate_values: dict[str, object] = {
            "reviewed_eligible_jobs": values["reviewed_eligible_job_count"],
            "represented_companies": values["represented_company_count"],
            "max_company_concentration": values["max_company_concentration"],
            "demonstrated_eligibility_false_positives": values[
                "demonstrated_eligibility_false_positive_count"
            ],
            "skill_semantics": values["skill_semantics"],
            "compensation_reconciled": (
                "reconciled" if values["compensation_reconciled"] else "not_reconciled"
            ),
        }
        for gate_id, expected_gate_value in expected_gate_values.items():
            if gates[gate_id].observed != expected_gate_value:
                raise ValueError(f"quality gate observed value does not reconcile for {gate_id}")

        fixed_policy: dict[str, tuple[str, object, str]] = {
            "reviewed_eligible_jobs": (">=", 20, "reviewed_jobs"),
            "represented_companies": (">=", 5, "companies"),
            "max_company_concentration": ("<=", Decimal("0.40"), "share"),
            "demonstrated_eligibility_false_positives": (
                "==",
                0,
                "false_positive_jobs",
            ),
            "skill_semantics": ("==", "mentions_only", "semantics"),
            "compensation_reconciled": ("==", "reconciled", "evidence"),
        }
        for gate_id, (operator, threshold, unit) in fixed_policy.items():
            gate = gates[gate_id]
            if (gate.operator, gate.threshold, gate.unit) != (operator, threshold, unit):
                raise ValueError(f"quality gate policy changed for {gate_id}")

        reviewed = evaluations["reviewed_eligible_jobs"].provenance.reviewed_job_ids
        false_positive_reviewed = evaluations[
            "demonstrated_eligibility_false_positives"
        ].provenance.reviewed_job_ids
        if reviewed != false_positive_reviewed:
            raise ValueError("manual gates must use the same reviewed evidence set")
        if values["reviewed_eligible_job_count"] != len(reviewed):
            raise ValueError("reviewed eligible count must reconcile to review provenance")
        false_positive_ids = gates["demonstrated_eligibility_false_positives"].evidence_job_ids
        if values["demonstrated_eligibility_false_positive_count"] != len(
            false_positive_ids
        ) or not set(false_positive_ids) <= set(reviewed):
            raise ValueError("false-positive evidence must reconcile to reviewed evidence")

        concentration_share = (
            sample.max_company_concentration.job_share
            if sample.max_company_concentration is not None
            else None
        )
        expected_statuses = {
            "reviewed_eligible_jobs": (
                "not_evaluable" if not reviewed else "passed" if len(reviewed) >= 20 else "failed"
            ),
            "represented_companies": (
                "not_evaluable"
                if not sample.job_ids
                else "passed"
                if sample.company_count >= 5
                else "failed"
            ),
            "max_company_concentration": (
                "not_evaluable"
                if concentration_share is None
                else "passed"
                if concentration_share <= Decimal("0.40")
                else "failed"
            ),
            "demonstrated_eligibility_false_positives": (
                "not_evaluable"
                if not reviewed
                else "passed"
                if not false_positive_ids
                else "failed"
            ),
            "skill_semantics": "passed",
            "compensation_reconciled": "passed",
        }
        for gate_id, expected_status in expected_statuses.items():
            if gates[gate_id].status != expected_status:
                raise ValueError(f"quality gate status does not reconcile for {gate_id}")

    @property
    def scope_name(self) -> str:
        return self.filters.name

    def to_payload(self) -> dict[str, object]:
        """Serialize the immutable result into schema-shaped Python values."""
        return {
            "contract_version": CONTRACT_VERSION,
            "scope": {
                "kind": "configured_source_panel",
                "name": self.scope_name,
                "target_universe": None,
            },
            "filters": self.filters.model_dump(mode="json"),
            "profile": self.profile.model_dump(mode="json"),
            "quality": _quality_payload(self.quality),
            "opportunities": {
                "job_ids": [str(value) for value in self.opportunities.job_ids],
                "count": self.opportunities.count,
                "company_count": self.opportunities.company_count,
                "items": [_opportunity_payload(item) for item in self.opportunities.items],
            },
            "observations": _observations_payload(self.observations),
        }


def _timestamp_payload(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def _opportunity_payload(item: HybridOpportunity) -> dict[str, object]:
    return {
        "job_id": str(item.job_id),
        "company_name": item.company_name,
        "title": item.title,
        "source": {"key": item.source.key, "kind": item.source.kind, "url": item.source.url},
        "freshness": {
            "first_seen_at": _timestamp_payload(item.freshness.first_seen_at),
            "last_seen_at": _timestamp_payload(item.freshness.last_seen_at),
        },
        "locations": [
            {
                "ordinal": value.ordinal,
                "city": value.city,
                "region": value.region,
                "country_code": value.country_code,
                "is_primary": value.is_primary,
                "confidence": value.confidence,
            }
            for value in item.locations
        ],
        "remote_scope": item.remote_scope,
        "skill_mentions": [
            {
                "id": str(value.id),
                "skill_id": value.skill_id,
                "matched_text": value.matched_text,
                "section": value.section,
                "method": value.method,
                "confidence": value.confidence,
                "taxonomy_version": value.taxonomy_version,
            }
            for value in item.skill_mentions
        ],
        "compensation": [
            {
                "component_id": str(value.component_id),
                "component_type": value.component_type,
                "min_value": value.min_value,
                "max_value": value.max_value,
                "currency": value.currency,
                "interval": value.interval or "unknown",
                "location_tier": value.location_tier,
                "source_method": value.source_method,
            }
            for value in item.compensation
        ],
        "status": item.status,
        "role_family": item.role_family,
        "seniority": item.seniority,
        "workplace_type": item.workplace_type,
    }


def _bucket_payload(item: HybridBucket) -> dict[str, object]:
    return {
        "value": item.value,
        "job_ids": [str(value) for value in item.job_ids],
        "denominator_job_ids": [str(value) for value in item.denominator_job_ids],
        "job_count": item.job_count,
        "job_share": item.job_share,
    }


def _concentration_payload(item: HybridConcentration | None) -> dict[str, object] | None:
    if item is None:
        return None
    return {
        "company_name": item.company_name,
        "job_ids": [str(value) for value in item.job_ids],
        "denominator_job_ids": [str(value) for value in item.denominator_job_ids],
        "job_count": item.job_count,
        "job_share": item.job_share,
    }


def _skill_payload(item: HybridSkill) -> dict[str, object]:
    return {
        "skill_id": item.skill_id,
        "label": item.label,
        "semantics": item.semantics,
        "job_ids": [str(value) for value in item.job_ids],
        "job_count": item.job_count,
        "job_share": item.job_share,
        "denominator_job_ids": [str(value) for value in item.denominator_job_ids],
        "company_names": list(item.company_names),
        "company_count": item.company_count,
        "company_share": item.company_share,
        "denominator_company_names": list(item.denominator_company_names),
    }


def _compensation_payload(item: HybridCompensation) -> dict[str, object]:
    return {
        "component_type": item.component_type,
        "currency": item.currency,
        "interval": item.interval,
        "location_tier": item.location_tier,
        "component_ids": [str(value) for value in item.component_ids],
        "job_ids": [str(value) for value in item.job_ids],
        "denominator_job_ids": [str(value) for value in item.denominator_job_ids],
        "component_count": item.component_count,
        "job_count": item.job_count,
        "job_share": item.job_share,
        "min_value": item.min_value,
        "max_value": item.max_value,
    }


def _observations_payload(value: HybridObservations) -> dict[str, object]:
    sample = value.sample
    coverage = sample.source_coverage
    return {
        "scope": "configured_source_panel",
        "sample": {
            "job_ids": [str(item) for item in sample.job_ids],
            "job_count": sample.job_count,
            "company_names": list(sample.company_names),
            "company_count": sample.company_count,
            "max_company_concentration": _concentration_payload(sample.max_company_concentration),
            "source_coverage": {
                "configured_source_count": coverage.configured_source_count,
                "configured_source_keys": list(coverage.configured_source_keys),
                "matching_source_count": coverage.matching_source_count,
                "matching_source_keys": list(coverage.matching_source_keys),
                "source_share": coverage.source_share,
                "job_ids_by_source": [
                    {"source_key": key, "job_ids": [str(item) for item in job_ids]}
                    for key, job_ids in coverage.job_ids_by_source
                ],
            },
            "collection_cutoff": _timestamp_payload(sample.collection_cutoff),
            "skill_denominator_job_ids": [str(item) for item in sample.skill_denominator_job_ids],
            "compensation_job_ids": [str(item) for item in sample.compensation_job_ids],
        },
        "companies": [_bucket_payload(item) for item in value.companies],
        "skills": [_skill_payload(item) for item in value.skills],
        "seniorities": [_bucket_payload(item) for item in value.seniorities],
        "workplace_types": [_bucket_payload(item) for item in value.workplace_types],
        "countries": [_bucket_payload(item) for item in value.countries],
        "compensation": [_compensation_payload(item) for item in value.compensation],
        "profile_mentions": {
            "semantics": "mention_only",
            "profile_skill_ids": list(value.profile_mentions.profile_skill_ids),
            "covered_skill_ids": list(value.profile_mentions.covered_skill_ids),
            "neutral_skill_ids": list(value.profile_mentions.neutral_skill_ids),
            "uncovered_mentions": [
                _skill_payload(item) for item in value.profile_mentions.uncovered_mentions
            ],
        },
    }


def _quality_payload(value: HybridQuality) -> dict[str, object]:
    return {
        "state": value.state,
        "claim_policy": {
            "opportunities": "available",
            "panel_observations": "panel_bounded" if value.state == "accepted" else "qualified",
            "broad_market_claims": "disabled",
            "skill_semantics": "mentions_only",
            "fit_probability": "not_provided",
        },
        "gates": [
            {
                "gate_id": item.gate_id,
                "status": item.status,
                "operator": item.operator,
                "threshold": item.threshold,
                "observed": item.observed,
                "unit": item.unit,
                "evidence_job_ids": [str(value) for value in item.evidence_job_ids],
                "evidence_company_names": list(item.evidence_company_names),
            }
            for item in value.gates
        ],
        "failed_gates": list(value.failed_gates),
        "evaluation_provenance": {
            key: {
                "basis": evaluation.basis,
                "provenance": {
                    "kind": evaluation.provenance.kind,
                    "ref": evaluation.provenance.ref,
                    "reviewed_job_ids": [
                        str(value) for value in evaluation.provenance.reviewed_job_ids
                    ],
                },
            }
            for key, evaluation in value.evaluation_provenance
        },
        "observed_values": {
            key: _timestamp_payload(item) if isinstance(item, datetime) else item
            for key, item in value.observed_values.items()
        },
    }


# Descriptive aliases make the public domain boundary discoverable without
# duplicating mutable API models.
HybridResultContract = HybridResult
H1HybridResult = HybridResult
QualityReview = QualityReviewInput
