"""Offline prototype for the versioned two-axis Career Radar role contract.

This module is deliberately separate from the accepted R1 classifier and
aggregation runtime. It adapts pinned legacy 1.1.0 evidence, and offers a pure
matched-window aggregator for future review; it does not persist runs or read a
database.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Final, cast

import career_os.radar.roles as legacy_roles
from career_os.domain.canonical import content_hash, stable_json_dumps
from career_os.radar.aggregate import (
    RadarObservationInput,
    RadarPanelInput,
    RadarScanInput,
    build_radar_result,
)
from career_os.radar.roles import (
    CLASSIFIER_VERSION as LEGACY_CLASSIFIER_VERSION,
)
from career_os.radar.roles import (
    ROLE_TAXONOMY,
    ClassificationStatus,
    EvidenceAxis,
    RoleClassification,
    RoleFamily,
    classify_role,
)

PROTOTYPE_CLASSIFIER_VERSION: Final = "2.0.0"
PROTOTYPE_TAXONOMY_VERSION: Final = "2.0.0"
PROTOTYPE_METRIC_VERSION: Final = "2.0.0"
PROTOTYPE_SCHEMA_VERSION: Final = "2.0.0"
PINNED_LEGACY_CLASSIFIER_VERSION: Final = "1.1.0"
PINNED_LEGACY_TAXONOMY_VERSION: Final = "1.0.0"
PINNED_LEGACY_SOURCE_SHA256: Final = (
    "3f1ddb0e76dcb243165790003fa0630fb5b737f14d29bb54823fc4bc5874db74"
)
PINNED_R1_METRIC_VERSION: Final = "1.0.0"
PINNED_R1_AGGREGATION_VERSION: Final = "1.0.0"
PANEL_OVERALL_COVERAGE_MINIMUM: Final = 0.90
PANEL_BOARD_COVERAGE_MINIMUM: Final = 0.80
GROUP_POSTING_MINIMUM: Final = 20
GROUP_COMPANY_MINIMUM: Final = 5
GROUP_COMPANY_CONCENTRATION_MAXIMUM: Final = 0.40
OCCUPATION_UNKNOWN_MAXIMUM: Final = 0.20
SPECIALTY_UNRESOLVED_MAXIMUM: Final = 0.20

OCCUPATIONS: Final = (
    "technical_engineering",
    "engineering_management",
    "solutions_architecture",
    "nontechnical",
    "unknown",
)
SPECIALTIES: Final = (
    "backend_engineering",
    "frontend_engineering",
    "fullstack_engineering",
    "data_engineering",
    "product_engineering",
    "platform_infrastructure_engineering",
    "machine_learning_engineering",
    "ai_engineering",
    "ai_product_engineering",
)

_LEGACY_FAMILY_TO_BROAD: Final = {
    **dict.fromkeys(SPECIALTIES, "technical_engineering"),
    RoleFamily.ENGINEERING_MANAGEMENT.value: "engineering_management",
    RoleFamily.SOLUTIONS_ARCHITECTURE.value: "solutions_architecture",
}
_SENIORITY_PREFIX: Final = r"(?:staff|principal|senior|sr\.?|lead|junior|jr\.?|mid)"
_TITLE_DELIMITER: Final = r"(?:$|\s*(?:,|\||[-\u2010-\u2011\u2012\u2013\u2014\u2015\u2212]|\())"
_B_ONLY_KNOWN_HEADS: Final = (
    "Android Mobile Engineer",
    "Mobile Engineer",
    "Systems Engineer",
    "Hardware Engineer",
)
_B_ONLY_UNKNOWN_HEADS: Final = (
    "Technical Program Manager",
    "Developer Relations Engineer",
    "Developer Relations Specialist",
    "Developer Advocate",
    "Developer Evangelist",
    "Developer Success Engineer",
    "AI Technical Support Specialist",
    "Technical Support Engineer",
    "Technical Support Specialist",
    "Support Engineer",
    "Support Specialist",
)
_GENERIC_HEADS: Final = (
    "Software Engineer",
    "Software Developer",
    "Engineer",
    "Developer",
)
_ROMAN_SUFFIX: Final = r"(?:\s+(?:I|II|III|IV))?"


class ContractInputError(ValueError):
    """An adapter or comparison input violates the pinned B contract."""


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    axis: str
    source: str
    rule_id: str
    matched_text: str

    def __post_init__(self) -> None:
        if not all(
            value.strip() for value in (self.axis, self.source, self.rule_id, self.matched_text)
        ):
            raise ValueError("evidence fields must be non-blank")


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    occupation: str
    specialty: str | None
    specialty_state: str
    reason: str | None
    candidate_families: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_families", tuple(self.candidate_families))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if self.occupation not in OCCUPATIONS:
            raise ValueError("unknown occupation")
        if self.occupation == "technical_engineering":
            if self.specialty_state == "resolved":
                if self.specialty not in SPECIALTIES or self.reason is not None:
                    raise ValueError("resolved technical specialty must name one family")
            elif self.specialty_state not in {"missing", "unsupported", "ambiguous"}:
                raise ValueError("technical engineering requires a specialty state")
            elif self.specialty is not None or not self.reason:
                raise ValueError("unresolved specialty requires null value and reason")
        elif self.occupation == "unknown":
            if self.specialty is not None or self.specialty_state != "not_assessed":
                raise ValueError("unknown occupation must have not_assessed specialty")
            if not self.reason:
                raise ValueError("unknown occupation requires a reason")
        elif self.specialty is not None or self.specialty_state != "not_applicable":
            raise ValueError("non-engineering occupations require not_applicable specialty")
        if len(self.candidate_families) != len(set(self.candidate_families)):
            raise ValueError("candidate families must be unique")
        if any(family not in _LEGACY_FAMILY_TO_BROAD for family in self.candidate_families):
            raise ValueError("candidate families must be known legacy family IDs")
        if not all(isinstance(item, EvidenceRef) for item in self.evidence):
            raise ValueError("evidence must contain immutable EvidenceRef values")


type JsonScalar = bool | int | float | str | None
type FrozenJson = JsonScalar | tuple[FrozenJson, ...] | Mapping[str, FrozenJson]


@dataclass(frozen=True, slots=True)
class RoleContractBuild:
    """Deep-immutable JSON payload and canonical digest for one offline build."""

    payload: Mapping[str, FrozenJson]
    digest: str

    def __post_init__(self) -> None:
        frozen = _frozen_mapping(_freeze_json(self.payload))
        payload = _thaw_json(frozen)
        if not isinstance(payload, dict):
            raise TypeError("build payload must be an object")
        recorded = payload.pop("build_digest", None)
        if recorded != self.digest or content_hash(payload) != self.digest:
            raise ValueError("build digest does not match immutable payload")
        object.__setattr__(self, "payload", frozen)

    def to_json(self) -> dict[str, object]:
        return _thaw_json(self.payload)  # type: ignore[return-value]


def _freeze_json(value: object) -> FrozenJson:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError(f"unsupported JSON payload value: {type(value).__name__}")


def _thaw_json(value: FrozenJson) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContractInputError(f"{field} must be an object")
    return cast(Mapping[str, object], value)


def _frozen_mapping(value: object) -> Mapping[str, FrozenJson]:
    if not isinstance(value, Mapping):
        raise TypeError("build payload must be a JSON object")
    return cast(Mapping[str, FrozenJson], value)


def _require_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractInputError(f"{field} must be an integer")
    return value


def _require_float(value: object, *, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ContractInputError(f"{field} must be numeric")
    return float(value)


@cache
def _legacy_source_sha256() -> str:
    source_path = Path(legacy_roles.__file__ or "")
    return hashlib.sha256(source_path.read_bytes()).hexdigest()


def _require_legacy_runtime() -> None:
    if LEGACY_CLASSIFIER_VERSION != PINNED_LEGACY_CLASSIFIER_VERSION:
        raise ContractInputError("B requires the pinned legacy classifier 1.1.0 runtime")
    if ROLE_TAXONOMY.version != PINNED_LEGACY_TAXONOMY_VERSION:
        raise ContractInputError("B requires the pinned legacy taxonomy 1.0.0 runtime")
    if _legacy_source_sha256() != PINNED_LEGACY_SOURCE_SHA256:
        raise ContractInputError("B requires the reviewed immutable legacy roles.py source")


def _evidence_from_payload(value: object) -> tuple[EvidenceRef, ...]:
    if not isinstance(value, (list, tuple)):
        raise ContractInputError("legacy evidence must be a list")
    result: list[EvidenceRef] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ContractInputError("legacy evidence entries must be objects")
        try:
            result.append(
                EvidenceRef(
                    axis=str(item["axis"]),
                    source=str(item["source"]),
                    rule_id=str(item["rule_id"]),
                    matched_text=str(item["matched_text"]),
                )
            )
        except KeyError as error:
            raise ContractInputError("legacy evidence entry is incomplete") from error
    return tuple(result)


def _legacy_payload(value: RoleClassification | Mapping[str, object]) -> dict[str, object]:
    _require_legacy_runtime()
    if isinstance(value, RoleClassification):
        payload: dict[str, object] = {
            "classifier_version": value.classifier_version,
            "taxonomy_version": value.taxonomy_version,
            "role_family": value.role_family.value if value.role_family else None,
            "status": value.status.value,
            "scope": value.scope.value,
            "evidence": [
                {
                    "axis": item.axis.value,
                    "source": item.source.value,
                    "rule_id": item.rule_id,
                    "matched_text": item.matched_text,
                }
                for item in value.evidence
            ],
        }
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        raise TypeError("legacy classification must be a role result or mapping")
    if payload.get("classifier_version") != PINNED_LEGACY_CLASSIFIER_VERSION:
        raise ContractInputError("legacy classifier version must be pinned to 1.1.0")
    if payload.get("taxonomy_version") != PINNED_LEGACY_TAXONOMY_VERSION:
        raise ContractInputError("legacy taxonomy version must be pinned to 1.0.0")
    if payload.get("status") not in {status.value for status in ClassificationStatus}:
        raise ContractInputError("legacy status is unsupported")
    if payload.get("scope") not in {"in_scope", "out_of_scope", "unknown_scope"}:
        raise ContractInputError("legacy scope is unsupported")
    family = payload.get("role_family")
    if family is not None and family not in {item.value for item in RoleFamily}:
        raise ContractInputError("legacy role family is unsupported")
    if (payload["status"] == ClassificationStatus.CLASSIFIED.value) != (family is not None):
        raise ContractInputError("legacy classified status and family do not agree")
    _evidence_from_payload(payload.get("evidence"))
    # Copy nested JSON before retaining it in the immutable result.
    return cast(dict[str, object], json.loads(stable_json_dumps(payload)))


def _title_match(title: str, head: str, *, roman: bool = False) -> re.Match[str] | None:
    body = r"\s+".join(re.escape(part) for part in head.split())
    suffix = _ROMAN_SUFFIX if roman else ""
    pattern = re.compile(
        rf"^\s*(?:(?P<seniority>{_SENIORITY_PREFIX})\s+)?"
        rf"(?P<head>{body}){suffix}(?={_TITLE_DELIMITER})",
        re.I,
    )
    return pattern.match(title.strip())


def _legacy_family_candidates(evidence: tuple[EvidenceRef, ...]) -> tuple[str, ...]:
    candidates: set[str] = set()
    for item in evidence:
        if item.axis != EvidenceAxis.ROLE.value:
            continue
        rule = item.rule_id
        for family, patterns in _role_patterns().items():
            if any(rule == rule_id for rule_id, _ in patterns):
                candidates.add(family.value)
        for family, (rule_id, _) in _duty_patterns().items():
            if rule == rule_id:
                candidates.add(family.value)
        if rule == "product_engineer_backend_specialty":
            candidates.add(RoleFamily.BACKEND_ENGINEERING.value)
        if rule == "platform_context_specialty":
            candidates.add(RoleFamily.PLATFORM_INFRASTRUCTURE_ENGINEERING.value)
        if rule == "cross_family_disjunction":
            from career_os.radar.roles import _DISJUNCTION, _disjunction_family

            matched = _DISJUNCTION.search(item.matched_text)
            if matched is not None:
                for side in (matched.group("left"), matched.group("right")):
                    disjunction_family = _disjunction_family(side)
                    if disjunction_family is not None:
                        candidates.add(disjunction_family.value)
    return tuple(sorted(candidates))


def _b_title_evidence(
    match: re.Match[str] | None, rule_id: str, *, axis: str
) -> tuple[EvidenceRef, ...]:
    if match is None:
        raise ContractInputError("B title evidence requires a successful head match")
    return (
        EvidenceRef(
            axis=axis,
            source="title",
            rule_id=rule_id,
            matched_text=match.group("head"),
        ),
    )


def _role_patterns() -> Mapping[RoleFamily, tuple[tuple[str, re.Pattern[str]], ...]]:
    # The pinned legacy pattern registry is the authoritative rule-ID set.
    from career_os.radar.roles import _ROLE_PATTERNS

    return _ROLE_PATTERNS


def _duty_patterns() -> Mapping[RoleFamily, tuple[str, re.Pattern[str]]]:
    from career_os.radar.roles import _DUTY_PATTERNS

    return _DUTY_PATTERNS


def _assignment(
    occupation: str,
    specialty: str | None,
    specialty_state: str,
    reason: str | None,
    candidates: Sequence[str],
    evidence: tuple[EvidenceRef, ...],
) -> RoleAssignment:
    return RoleAssignment(
        occupation=occupation,
        specialty=specialty,
        specialty_state=specialty_state,
        reason=reason,
        candidate_families=tuple(sorted(set(candidates))),
        evidence=evidence,
    )


def adapt_legacy_output(
    title: str,
    legacy: RoleClassification | Mapping[str, object],
) -> RoleAssignment:
    """Map one pinned legacy result to the finite B occupation/specialty axes."""
    payload = _legacy_payload(legacy)
    evidence = _evidence_from_payload(payload["evidence"])
    scope_rules = {item.rule_id for item in evidence if item.axis == EvidenceAxis.SCOPE.value}
    # A scope conflict wins over any specialty evidence.
    if {"nontechnical_role_head", "technical_role_head"} <= scope_rules:
        candidates = _legacy_family_candidates(evidence)
        return _assignment("unknown", None, "not_assessed", "scope_conflict", candidates, evidence)

    status = str(payload["status"])
    legacy_family = payload.get("role_family")
    if status == ClassificationStatus.CLASSIFIED.value and legacy_family is not None:
        broad = _LEGACY_FAMILY_TO_BROAD[str(legacy_family)]
        if broad == "technical_engineering":
            return _assignment(
                broad,
                str(legacy_family),
                "resolved",
                None,
                (str(legacy_family),),
                evidence,
            )
        return _assignment(broad, None, "not_applicable", "not_applicable", (), evidence)

    if status == ClassificationStatus.OUT_OF_SCOPE.value:
        return _assignment("nontechnical", None, "not_applicable", "not_applicable", (), evidence)

    stripped = title.strip()

    # Preserve unresolved legacy candidates before applying B's title-only
    # fallback. In particular, recognized competing duties beat a generic head.
    if status in {ClassificationStatus.AMBIGUOUS.value, ClassificationStatus.CONFLICT.value}:
        candidates = _legacy_family_candidates(evidence)
        if candidates and all(
            _LEGACY_FAMILY_TO_BROAD[item] == "technical_engineering" for item in candidates
        ):
            reason = (
                "title_duty_conflict"
                if status == ClassificationStatus.CONFLICT.value
                else "multiple_family_candidates"
            )
            technical_candidates = tuple(item for item in candidates if item in SPECIALTIES)
            return _assignment(
                "technical_engineering",
                None,
                "ambiguous",
                reason,
                technical_candidates,
                evidence,
            )
        return _assignment(
            "unknown",
            None,
            "not_assessed",
            "unresolved_competing_evidence",
            candidates,
            evidence,
        )

    # Explicit B-only role-head boundaries are unknown occupation, even when
    # legacy broad lexical cues would otherwise call the posting technical.
    unknown_head = next(
        (
            (head, _title_match(stripped, head))
            for head in _B_ONLY_UNKNOWN_HEADS
            if _title_match(stripped, head)
        ),
        None,
    )
    if unknown_head is not None:
        head, match = unknown_head
        assert match is not None
        return _assignment(
            "unknown",
            None,
            "not_assessed",
            "b_only_scope_boundary",
            (),
            (*evidence, *_b_title_evidence(match, "b_unknown_occupation_head", axis="occupation")),
        )

    if status != ClassificationStatus.UNCLASSIFIED.value or payload.get("scope") != "in_scope":
        return _assignment(
            "unknown", None, "not_assessed", "occupation_not_established", (), evidence
        )

    explicit_known = next(
        (
            (head, _title_match(stripped, head, roman=True))
            for head in _B_ONLY_KNOWN_HEADS
            if _title_match(stripped, head, roman=True)
        ),
        None,
    )
    if explicit_known is not None:
        head, match = explicit_known
        assert match is not None
    else:
        head, match = "", None
    if head in {"Android Mobile Engineer", "Mobile Engineer"}:
        return _assignment(
            "technical_engineering",
            None,
            "unsupported",
            "unsupported_mobile_specialty",
            (),
            (*evidence, *_b_title_evidence(match, "b_unsupported_mobile_head", axis="specialty")),
        )
    if head == "Hardware Engineer":
        return _assignment(
            "technical_engineering",
            None,
            "unsupported",
            "unsupported_hardware_specialty",
            (),
            (*evidence, *_b_title_evidence(match, "b_unsupported_hardware_head", axis="specialty")),
        )
    if head == "Systems Engineer":
        return _assignment(
            "technical_engineering",
            None,
            "missing",
            "no_family_evidence",
            (),
            (*evidence, *_b_title_evidence(match, "b_systems_engineering_head", axis="occupation")),
        )

    # Generic title recognition is a B-only closed boundary. It adds the
    # missing end/delimiter check absent from legacy's word-boundary regex.
    generic_match = next(
        (match for head in _GENERIC_HEADS if (match := _title_match(stripped, head, roman=True))),
        None,
    )
    if generic_match is not None:
        return _assignment(
            "technical_engineering",
            None,
            "missing",
            "no_family_evidence",
            (),
            (
                *evidence,
                *_b_title_evidence(generic_match, "b_generic_engineering_head", axis="occupation"),
            ),
        )

    return _assignment("unknown", None, "not_assessed", "unrecognized_title", (), evidence)


def adapt_classification(title: str, classification: RoleClassification) -> RoleAssignment:
    return adapt_legacy_output(title, classification)


def _assignment_payload(value: RoleAssignment) -> dict[str, object]:
    return {
        "occupation": value.occupation,
        "specialty": value.specialty,
        "specialty_state": value.specialty_state,
        "reason": value.reason,
        "candidate_families": list(value.candidate_families),
        "evidence": [
            {
                "axis": item.axis,
                "source": item.source,
                "rule_id": item.rule_id,
                "matched_text": item.matched_text,
            }
            for item in value.evidence
        ],
    }


def _metric_versions() -> dict[str, str]:
    _require_legacy_runtime()
    return {
        "prototype_classifier": PROTOTYPE_CLASSIFIER_VERSION,
        "prototype_taxonomy": PROTOTYPE_TAXONOMY_VERSION,
        "metric_contract": PROTOTYPE_METRIC_VERSION,
        "output_schema": PROTOTYPE_SCHEMA_VERSION,
        "legacy_classifier": PINNED_LEGACY_CLASSIFIER_VERSION,
        "legacy_taxonomy": PINNED_LEGACY_TAXONOMY_VERSION,
        "legacy_source_sha256": PINNED_LEGACY_SOURCE_SHA256,
        "selector_metric": PINNED_R1_METRIC_VERSION,
        "selector_aggregation": PINNED_R1_AGGREGATION_VERSION,
    }


def build_purposive_snapshot(
    frozen_artifact: Mapping[str, object], *, source_sha256: str
) -> RoleContractBuild:
    """Adapt the accepted frozen127 inputs without claiming history/population."""
    if frozen_artifact.get("schema") != "career-radar.p4-residual-cause.frozen-corpus.v1":
        raise ContractInputError("unexpected frozen corpus schema")
    versions = frozen_artifact.get("versions")
    rows = frozen_artifact.get("rows")
    if (
        not isinstance(versions, Mapping)
        or versions.get("classifier") != "1.1.0"
        or versions.get("taxonomy") != "1.0.0"
    ):
        raise ContractInputError("frozen input versions must be classifier 1.1.0/taxonomy 1.0.0")
    if not isinstance(rows, list):
        raise ContractInputError("frozen corpus rows must be a list")
    if len({str(row.get("job_id")) for row in rows if isinstance(row, Mapping)}) != len(rows):
        raise ContractInputError("frozen corpus rows must have unique job IDs")
    adapted: list[dict[str, object]] = []
    assignments: list[RoleAssignment] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ContractInputError("frozen corpus row must be an object")
        title = row.get("exact_title")
        legacy = row.get("classifier_1_1_0_full_input_output")
        if not isinstance(title, str) or not isinstance(legacy, Mapping):
            raise ContractInputError("frozen row lacks exact title/full legacy result")
        assignment = adapt_legacy_output(title, legacy)
        assignments.append(assignment)
        adapted.append(
            {
                "snapshot_row_id": str(row["job_id"]),
                "source_key": str(row["source_key"]),
                "exact_title": title,
                "description_sha256": str(row["description_sha256"]),
                "legacy_classifier_output": _legacy_payload(legacy),
                "assignment": _assignment_payload(assignment),
            }
        )
    adapted.sort(key=lambda item: (item["source_key"], item["snapshot_row_id"]))
    input_digest = content_hash(
        {
            "frozen_artifact_sha256": source_sha256,
            "versions": _metric_versions(),
            "rows": [
                {
                    key: item[key]
                    for key in (
                        "snapshot_row_id",
                        "source_key",
                        "exact_title",
                        "description_sha256",
                        "legacy_classifier_output",
                    )
                }
                for item in adapted
            ],
        }
    )
    occupation_counts = Counter(item.occupation for item in assignments)
    specialty_counts = Counter(item.specialty for item in assignments if item.specialty is not None)
    unresolved_counts = Counter(
        item.specialty_state for item in assignments if item.occupation == "technical_engineering"
    )
    payload: dict[str, object] = {
        "schema": "career-radar.p4-role-contract.diagnostic.v2",
        "purpose": "purposive_diagnostic_snapshot",
        "population_estimate": None,
        "accuracy_claim": None,
        "history_windows": {
            "status": "insufficient",
            "reason": "missing_history",
            "previous": {"counts": None, "shares": None},
            "current": {"counts": None, "shares": None},
            "delta_pp": None,
        },
        "versions": _metric_versions(),
        "provenance": {
            "source_artifact_sha256": source_sha256,
            "input_digest": input_digest,
            "frozen_row_count": len(adapted),
        },
        "diagnostic_counts_only": {
            "corpus_rows": len(adapted),
            "occupation_counts": {key: occupation_counts.get(key, 0) for key in OCCUPATIONS},
            "resolved_specialty_counts": {key: specialty_counts.get(key, 0) for key in SPECIALTIES},
            "technical_specialty_state_counts": {
                key: unresolved_counts.get(key, 0)
                for key in ("resolved", "missing", "unsupported", "ambiguous")
            },
        },
        "rows": adapted,
    }
    digest = content_hash(payload)
    payload["build_digest"] = digest
    return RoleContractBuild(_frozen_mapping(_freeze_json(payload)), digest)


def write_immutable_json(path: Path, build: RoleContractBuild) -> bool:
    """Create a deterministic artifact; identical reruns are no-ops, conflicts fail."""
    rendered = stable_json_dumps(build.to_json()).encode("utf-8") + b"\n"
    try:
        with path.open("xb") as output:
            output.write(rendered)
        return True
    except FileExistsError:
        if path.read_bytes() == rendered:
            return False
        raise ContractInputError(
            f"refusing to overwrite different immutable build: {path}"
        ) from None


@dataclass(frozen=True, slots=True)
class _SelectedPosting:
    source_key: str
    external_id: str
    observed_at: datetime
    title: str
    assignment: RoleAssignment


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 9) if denominator else None


def _gate(code: str, passed: bool, **details: object) -> dict[str, object]:
    return {"code": code, "passed": passed, **details}


def _latest_postings(
    *,
    window: str,
    matched_pairs: Sequence[Mapping[str, object]],
    scans_by_id: Mapping[str, RadarScanInput],
) -> tuple[_SelectedPosting, ...] | None:
    """Return None with no matched history; otherwise collapse latest observations."""
    if not matched_pairs:
        return None
    run_ids = {str(pair[f"{window}_run_id"]) for pair in matched_pairs}
    latest: dict[tuple[str, str], tuple[RadarObservationInput, str]] = {}
    for run_id in run_ids:
        scan = scans_by_id[run_id]
        for observation in scan.observations:
            if (
                observation.observed_at.tzinfo is None
                or observation.observed_at.utcoffset() is None
            ):
                raise ContractInputError("observation timestamp must be timezone-aware")
            identity = (scan.source_key, observation.external_id)
            previous = latest.get(identity)
            key = (observation.observed_at.astimezone(UTC), run_id)
            if previous is None or key > (previous[0].observed_at.astimezone(UTC), previous[1]):
                latest[identity] = (observation, run_id)
    return tuple(
        _SelectedPosting(
            source_key=source_key,
            external_id=external_id,
            observed_at=observation.observed_at.astimezone(UTC),
            title=observation.title,
            assignment=adapt_classification(
                observation.title,
                classify_role(observation.title, observation.description),
            ),
        )
        for (source_key, external_id), (observation, _) in sorted(latest.items())
    )


def _sample_summary(
    postings: Sequence[_SelectedPosting],
    predicate: Callable[[_SelectedPosting], bool],
    company_by_source: Mapping[str, str],
    *,
    history: bool,
) -> dict[str, object]:
    if not history:
        return {"count": None, "company_count": None, "largest_company_share": None}
    selected = [posting for posting in postings if predicate(posting)]
    companies = Counter(company_by_source[item.source_key] for item in selected)
    return {
        "count": len(selected),
        "company_count": len(companies),
        "largest_company_share": _ratio(max(companies.values(), default=0), len(selected)),
    }


def _sample_pass(summary: Mapping[str, object]) -> bool:
    count = summary["count"]
    companies = summary["company_count"]
    concentration = summary["largest_company_share"]
    return (
        isinstance(count, int)
        and count >= GROUP_POSTING_MINIMUM
        and isinstance(companies, int)
        and companies >= GROUP_COMPANY_MINIMUM
        and isinstance(concentration, (int, float))
        and concentration <= GROUP_COMPANY_CONCENTRATION_MAXIMUM
    )


def _occupation_predicate(value: str) -> Callable[[_SelectedPosting], bool]:
    return lambda posting: posting.assignment.occupation == value


def _specialty_predicate(value: str) -> Callable[[_SelectedPosting], bool]:
    return lambda posting: posting.assignment.specialty == value


def _window_payload(
    postings: tuple[_SelectedPosting, ...] | None,
    *,
    company_by_source: Mapping[str, str],
    window_name: str,
) -> dict[str, object]:
    history = postings is not None
    if postings is None:
        postings = ()
    occupation_counts = Counter(item.assignment.occupation for item in postings)
    specialty_counts = Counter(
        item.assignment.specialty
        for item in postings
        if item.assignment.occupation == "technical_engineering" and item.assignment.specialty
    )
    specialty_states = Counter(
        item.assignment.specialty_state
        for item in postings
        if item.assignment.occupation == "technical_engineering"
    )
    n_panel = len(postings) if history else None
    n_specialty = specialty_states.total() if history else None
    occupations: dict[str, object] = {}
    for occupation in OCCUPATIONS:
        share = (
            _ratio(occupation_counts.get(occupation, 0), n_panel) if history and n_panel else None
        )

        summary = _sample_summary(
            postings,
            _occupation_predicate(occupation),
            company_by_source,
            history=history,
        )
        occupations[occupation] = {**summary, "share": share}
    specialties: dict[str, object] = {}
    for specialty in SPECIALTIES:
        share = (
            _ratio(specialty_counts.get(specialty, 0), n_specialty)
            if history and n_specialty
            else None
        )

        summary = _sample_summary(
            postings,
            _specialty_predicate(specialty),
            company_by_source,
            history=history,
        )
        specialties[specialty] = {**summary, "share": share}
    technical_union = (
        sum(
            occupation_counts.get(value, 0)
            for value in (
                "technical_engineering",
                "engineering_management",
                "solutions_architecture",
            )
        )
        if history
        else None
    )
    unresolved = sum(
        specialty_states.get(value, 0) for value in ("missing", "unsupported", "ambiguous")
    )
    technical_rows = [
        item for item in postings if item.assignment.occupation == "technical_engineering"
    ]
    union_summary = _sample_summary(
        postings,
        lambda posting: (
            posting.assignment.occupation
            in {"technical_engineering", "engineering_management", "solutions_architecture"}
        ),
        company_by_source,
        history=history,
    )
    return {
        "name": window_name,
        "history_observed": history,
        "N_panel": n_panel,
        "occupation_counts": {key: occupation_counts.get(key, 0) for key in OCCUPATIONS}
        if history
        else None,
        "occupation_shares": {
            key: _mapping(occupations[key], field="occupation summary")["share"]
            for key in OCCUPATIONS
        }
        if history
        else None,
        "unknown_occupation_share": _ratio(occupation_counts.get("unknown", 0), n_panel)
        if history and n_panel
        else None,
        "N_specialty": n_specialty,
        "specialty_counts": {key: specialty_counts.get(key, 0) for key in SPECIALTIES}
        if history
        else None,
        "specialty_shares": {
            key: _mapping(specialties[key], field="specialty summary")["share"]
            for key in SPECIALTIES
        }
        if history
        else None,
        "specialty_state_counts": {
            key: specialty_states.get(key, 0)
            for key in ("resolved", "missing", "unsupported", "ambiguous")
        }
        if history
        else None,
        "unresolved_specialty_share": _ratio(unresolved, n_specialty)
        if history and n_specialty
        else None,
        "technical_occupation_count": technical_union,
        "technical_occupation_share": _ratio(technical_union, n_panel)
        if isinstance(technical_union, int) and isinstance(n_panel, int) and n_panel > 0
        else None,
        "occupation_groups": occupations,
        "specialty_groups": specialties,
        "technical_union_sample": union_summary,
        "technical_engineering_count": len(technical_rows) if history else None,
    }


def _make_comparisons(
    previous: Mapping[str, object],
    current: Mapping[str, object],
    *,
    common_gates: Sequence[Mapping[str, object]],
    occupation_gates: Sequence[Mapping[str, object]],
    specialty_gates: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    shared_pass = all(bool(item["passed"]) for item in common_gates)
    occupation_pass = shared_pass and all(bool(item["passed"]) for item in occupation_gates)
    specialty_pass = occupation_pass and all(bool(item["passed"]) for item in specialty_gates)

    broad: dict[str, object] = {}
    for occupation in OCCUPATIONS:
        left_groups = _mapping(previous["occupation_groups"], field="previous occupation groups")
        right_groups = _mapping(current["occupation_groups"], field="current occupation groups")
        left_summary = _mapping(left_groups[occupation], field="previous occupation summary")
        right_summary = _mapping(right_groups[occupation], field="current occupation summary")
        gate = _sample_pass(left_summary) and _sample_pass(right_summary)
        broad[occupation] = {
            "delta_pp": delta_from(
                left_summary["share"], right_summary["share"], occupation_pass and gate
            ),
            "status": "provisional" if occupation_pass and gate else "insufficient",
            "sample_gate_passed": gate,
        }
    union_gate = _sample_pass(
        _mapping(previous["technical_union_sample"], field="previous union summary")
    ) and _sample_pass(_mapping(current["technical_union_sample"], field="current union summary"))
    broad["technical_occupation_share"] = {
        "delta_pp": delta_from(
            previous["technical_occupation_share"],
            current["technical_occupation_share"],
            occupation_pass and union_gate,
        ),
        "status": "provisional" if occupation_pass and union_gate else "insufficient",
        "sample_gate_passed": union_gate,
    }
    specialty: dict[str, object] = {}
    for family in SPECIALTIES:
        left_groups = _mapping(previous["specialty_groups"], field="previous specialty groups")
        right_groups = _mapping(current["specialty_groups"], field="current specialty groups")
        left_summary = _mapping(left_groups[family], field="previous specialty summary")
        right_summary = _mapping(right_groups[family], field="current specialty summary")
        sample_gate = _sample_pass(left_summary) and _sample_pass(right_summary)
        specialty[family] = {
            "delta_pp": delta_from(
                left_summary["share"], right_summary["share"], specialty_pass and sample_gate
            ),
            "status": "provisional" if specialty_pass and sample_gate else "insufficient",
            "sample_gate_passed": sample_gate,
        }
    return {
        "broad_occupation": broad,
        "specialty_within_technical_engineering": specialty,
        "broad_run_status": "provisional" if occupation_pass else "insufficient",
        "specialty_run_status": "provisional" if specialty_pass else "insufficient",
    }


def delta_from(left: object, right: object, allowed: bool) -> float | None:
    if not allowed or not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return None
    return round((right - left) * 100, 9)


def aggregate_role_contract_windows(
    panel: RadarPanelInput,
    cutoff: datetime,
    scans: tuple[RadarScanInput, ...],
) -> RoleContractBuild:
    """Aggregate B over R1's matched slots, with B-specific denominators/gates."""
    selected = build_radar_result(panel, cutoff, scans)
    selected_versions = _mapping(selected.get("versions"), field="selector versions")
    expected_selector_versions = {
        "metric_contract": PINNED_R1_METRIC_VERSION,
        "aggregation_algorithm": PINNED_R1_AGGREGATION_VERSION,
        "classifier": PINNED_LEGACY_CLASSIFIER_VERSION,
        "taxonomy": PINNED_LEGACY_TAXONOMY_VERSION,
    }
    if dict(selected_versions) != expected_selector_versions:
        raise ContractInputError("R1 selector changed from the pinned 1.0.0/1.1.0 contract")
    matched = selected["matched_pairs"]
    if not isinstance(matched, list) or not all(isinstance(item, Mapping) for item in matched):
        raise ContractInputError("R1 selector returned an unsupported matched-pair payload")
    scans_by_id = {str(scan.run_id): scan for scan in scans}
    if len(scans_by_id) != len(scans):
        raise ContractInputError("scan run IDs must be unique")
    panel_sources = {source.source_key: source for source in panel.sources}
    previous_rows = _latest_postings(
        window="previous", matched_pairs=matched, scans_by_id=scans_by_id
    )
    current_rows = _latest_postings(
        window="current", matched_pairs=matched, scans_by_id=scans_by_id
    )
    company_by_source = {key: source.company_key for key, source in panel_sources.items()}
    previous = _window_payload(
        previous_rows, company_by_source=company_by_source, window_name="previous"
    )
    current = _window_payload(
        current_rows, company_by_source=company_by_source, window_name="current"
    )

    matched_count = _require_int(selected.get("matched_pair_count"), field="matched pair count")
    expected_count = _require_int(selected.get("expected_pair_count"), field="expected pair count")
    coverage = _require_float(selected.get("matched_pair_coverage"), field="matched pair coverage")
    board_coverage = selected["board_coverage"]
    if not isinstance(board_coverage, Mapping):
        raise ContractInputError("R1 selector omitted board coverage")
    common_gates: list[dict[str, object]] = [
        _gate(
            "history_present",
            matched_count > 0,
            reason=None if matched_count > 0 else "missing_history",
            matched_pairs=matched_count,
            expected_pairs=expected_count,
        ),
        _gate(
            "overall_coverage",
            coverage >= PANEL_OVERALL_COVERAGE_MINIMUM,
            coverage=coverage,
            threshold=PANEL_OVERALL_COVERAGE_MINIMUM,
        ),
    ]
    for source_key, item in sorted(board_coverage.items()):
        if not isinstance(item, Mapping):
            raise ContractInputError("board coverage item is malformed")
        board_value = _require_float(item.get("coverage"), field="board coverage")
        common_gates.append(
            _gate(
                "board_coverage",
                board_value >= PANEL_BOARD_COVERAGE_MINIMUM,
                source_key=source_key,
                coverage=board_value,
                threshold=PANEL_BOARD_COVERAGE_MINIMUM,
            )
        )
    versions = _metric_versions()
    common_gates.append(_gate("version_match", True, versions=versions))

    def unknown_gate(window: Mapping[str, object]) -> dict[str, object]:
        denominator = window["N_panel"]
        rate = window["unknown_occupation_share"]
        return _gate(
            "occupation_unknown_threshold",
            isinstance(denominator, int)
            and denominator > 0
            and isinstance(rate, (int, float))
            and rate <= OCCUPATION_UNKNOWN_MAXIMUM,
            window=window["name"],
            N_panel=denominator,
            unknown_share=rate,
            threshold=OCCUPATION_UNKNOWN_MAXIMUM,
        )

    occupation_gates = [unknown_gate(previous), unknown_gate(current)]

    def specialty_run_gate(window: Mapping[str, object]) -> dict[str, object]:
        denominator = window["N_specialty"]
        rate = window["unresolved_specialty_share"]
        return _gate(
            "specialty_unresolved_threshold",
            isinstance(denominator, int)
            and denominator > 0
            and isinstance(rate, (int, float))
            and rate <= SPECIALTY_UNRESOLVED_MAXIMUM,
            window=window["name"],
            N_specialty=denominator,
            unresolved_share=rate,
            threshold=SPECIALTY_UNRESOLVED_MAXIMUM,
        )

    specialty_gates = [specialty_run_gate(previous), specialty_run_gate(current)]
    comparisons = _make_comparisons(
        previous,
        current,
        common_gates=common_gates,
        occupation_gates=occupation_gates,
        specialty_gates=specialty_gates,
    )
    selector_digest = str(selected["input_digest"])
    selected_windows = _mapping(selected.get("windows"), field="selector windows")
    selector_window_provenance = {
        name: {
            "bounds": _mapping(selected_windows.get(name), field=f"{name} selector window")[
                "bounds"
            ],
            "input_digest": _mapping(selected_windows.get(name), field=f"{name} selector window")[
                "input_digest"
            ],
        }
        for name in ("previous", "current")
    }
    input_digest = content_hash(
        {
            "r1_selector_input_digest": selector_digest,
            "versions": versions,
            "matched_pairs": matched,
            "window_provenance": selector_window_provenance,
        }
    )
    payload = {
        "schema": "career-radar.p4-role-contract.windows.v2",
        "status": "provisional"
        if comparisons["broad_run_status"] == "provisional"
        else "insufficient",
        "purpose": "matched_panel_descriptive_comparison",
        "panel": {
            "panel_version": panel.panel_version,
            "manifest_digest": panel.manifest_digest,
            "panel_digest": panel.panel_digest,
            "source_count": len(panel.sources),
        },
        "cutoff_utc": selected["cutoff_utc"],
        "windows": {"previous": previous, "current": current},
        "matched_pair_count": matched_count,
        "expected_pair_count": expected_count,
        "matched_pair_coverage": coverage,
        "matched_slot_mapping": matched,
        "selector_window_provenance": selector_window_provenance,
        "board_coverage": board_coverage,
        "gates": {
            "shared": common_gates,
            "occupation_run": occupation_gates,
            "specialty_run": specialty_gates,
        },
        "comparisons": comparisons,
        "versions": versions,
        "input_digest": input_digest,
    }
    digest = content_hash(payload)
    payload["build_digest"] = digest
    return RoleContractBuild(_frozen_mapping(_freeze_json(payload)), digest)


def compare_build_versions(left: RoleContractBuild, right: RoleContractBuild) -> None:
    """Reject a cross-version delta before any metric comparison is attempted."""
    left_payload = left.payload
    right_payload = right.payload
    if left_payload.get("versions") != right_payload.get("versions"):
        raise ContractInputError("cross-version comparisons are not defined")
