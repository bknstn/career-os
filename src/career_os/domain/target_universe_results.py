"""Transport-neutral H2 Target Universe result and quality contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import cast

from career_os.config.target_universes import TargetUniverse
from career_os.domain.hybrid_results import (
    HybridEvaluation,
    HybridGate,
    HybridObservations,
    HybridOpportunities,
    HybridProvenance,
    HybridResult,
)

CONTRACT_VERSION = "h2.target-universe.v1"
OBSERVATION_SCOPE = "target_universe_configured_source_panel"
GATE_IDS = (
    "target_universe_boundary",
    "opportunity_evidence_reconciled",
    "reviewed_eligible_opportunities",
    "demonstrated_eligibility_false_positives",
    "skill_semantics",
    "compensation_reconciled",
)
_OBSERVED_VALUE_KEYS = frozenset(
    {
        "matching_opportunity_count",
        "source_panel_source_count",
        "reviewed_eligible_opportunity_count",
        "demonstrated_eligibility_false_positive_count",
        "target_universe_boundary_resolved",
        "opportunity_evidence_reconciled",
        "skill_semantics",
        "compensation_reconciled",
        "source_coverage",
        "compensation_coverage",
        "collection_cutoff",
    }
)


def _share(numerator: int, denominator: int) -> Decimal | None:
    return Decimal(numerator) / Decimal(denominator) if denominator else None


@dataclass(frozen=True, slots=True)
class TargetUniverseQuality:
    """H2 quality for an explicit boundary, not a representative market sample.

    A narrow, deliberately complete universe is not a weak sample. H2 verifies
    the declared boundary and reconciliation integrity, while retaining an
    exhaustive manual eligibility review and the mention/compensation evidence
    invariants. It never enables broad-market claims.
    """

    state: str
    gates: tuple[HybridGate, ...]
    failed_gates: tuple[str, ...]
    evaluation_provenance: tuple[tuple[str, HybridEvaluation], ...]
    observed_values: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_values", MappingProxyType(dict(self.observed_values)))
        if set(self.observed_values) != _OBSERVED_VALUE_KEYS:
            raise ValueError("observed_values must contain the fixed H2 diagnostic keys")
        for key in (
            "matching_opportunity_count",
            "source_panel_source_count",
            "reviewed_eligible_opportunity_count",
            "demonstrated_eligibility_false_positive_count",
        ):
            value = self.observed_values[key]
            if type(value) is not int or value < 0:
                raise ValueError(f"observed_values.{key} must be a non-negative integer")
        for key in ("target_universe_boundary_resolved", "opportunity_evidence_reconciled"):
            if type(self.observed_values[key]) is not bool:
                raise ValueError(f"observed_values.{key} must be a bool")
        if self.observed_values["skill_semantics"] != "mentions_only":
            raise ValueError("observed_values.skill_semantics must be mentions_only")
        if type(self.observed_values["compensation_reconciled"]) is not bool:
            raise ValueError("observed_values.compensation_reconciled must be a bool")
        for key in ("source_coverage", "compensation_coverage"):
            value = self.observed_values[key]
            if value is not None and (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value < Decimal(0)
                or value > Decimal(1)
            ):
                raise ValueError(f"observed_values.{key} must be a share or null")
        cutoff = self.observed_values["collection_cutoff"]
        if cutoff is not None and (
            not isinstance(cutoff, datetime) or cutoff.tzinfo is None or cutoff.utcoffset() is None
        ):
            raise ValueError("observed_values.collection_cutoff must be an aware timestamp or null")
        if self.state not in {"accepted", "evidence_limited"}:
            raise ValueError("invalid H2 quality state")
        if tuple(gate.gate_id for gate in self.gates) != GATE_IDS:
            raise ValueError("quality gates must use the fixed H2 order")
        if tuple(key for key, _ in self.evaluation_provenance) != GATE_IDS:
            raise ValueError("quality provenance must use the fixed H2 order")
        gates = {gate.gate_id: gate for gate in self.gates}
        evaluations = dict(self.evaluation_provenance)
        fixed_policy: dict[str, tuple[str, object, object, str]] = {
            "target_universe_boundary": ("passed", "==", "resolved", "boundary"),
            "opportunity_evidence_reconciled": ("passed", "==", "reconciled", "evidence"),
            "skill_semantics": ("passed", "==", "mentions_only", "semantics"),
            "compensation_reconciled": ("passed", "==", "reconciled", "evidence"),
        }
        for gate_id, (status, operator, threshold, unit) in fixed_policy.items():
            gate = gates[gate_id]
            if (gate.status, gate.operator, gate.threshold, gate.unit) != (
                status,
                operator,
                threshold,
                unit,
            ):
                raise ValueError(f"H2 quality gate policy changed for {gate_id}")
        review = gates["reviewed_eligible_opportunities"]
        false_positives = gates["demonstrated_eligibility_false_positives"]
        matching_count = self.observed_values["matching_opportunity_count"]
        reviewed_count = self.observed_values["reviewed_eligible_opportunity_count"]
        false_positive_count = self.observed_values["demonstrated_eligibility_false_positive_count"]
        if (
            review.operator,
            review.threshold,
            review.observed,
            review.unit,
        ) != ("==", matching_count, reviewed_count, "reviewed_jobs"):
            raise ValueError("reviewed eligibility gate does not reconcile to H2 observed values")
        if (
            false_positives.operator,
            false_positives.threshold,
            false_positives.observed,
            false_positives.unit,
        ) != ("==", 0, false_positive_count, "false_positive_jobs"):
            raise ValueError("false-positive gate does not reconcile to H2 observed values")
        expected_review_status = (
            "passed" if matching_count and reviewed_count == matching_count else "not_evaluable"
        )
        if review.status != expected_review_status:
            raise ValueError("reviewed eligibility gate status does not reconcile")
        expected_false_positive_status = (
            "passed"
            if expected_review_status == "passed" and false_positive_count == 0
            else "failed"
            if expected_review_status == "passed"
            else "not_evaluable"
        )
        if false_positives.status != expected_false_positive_status:
            raise ValueError("false-positive gate status does not reconcile")
        review_provenance = evaluations["reviewed_eligible_opportunities"].provenance
        false_positive_provenance = evaluations[
            "demonstrated_eligibility_false_positives"
        ].provenance
        if (
            review_provenance.reviewed_job_ids != review.evidence_job_ids
            or false_positive_provenance.reviewed_job_ids != review.evidence_job_ids
            or not set(false_positives.evidence_job_ids) <= set(review.evidence_job_ids)
        ):
            raise ValueError("manual review provenance does not reconcile to H2 review gates")
        for gate_id in ("target_universe_boundary", "opportunity_evidence_reconciled"):
            if evaluations[gate_id].basis != "contract_invariant":
                raise ValueError(f"{gate_id} requires contract-invariant provenance")
        for gate_id in (
            "reviewed_eligible_opportunities",
            "demonstrated_eligibility_false_positives",
        ):
            if evaluations[gate_id].basis != "manual_review":
                raise ValueError(f"{gate_id} requires manual-review provenance")
        expected_failed = tuple(gate.gate_id for gate in self.gates if gate.status != "passed")
        if self.failed_gates != expected_failed:
            raise ValueError("failed_gates must mirror non-passed H2 gates")
        expected_state = "accepted" if not expected_failed else "evidence_limited"
        if self.state != expected_state:
            raise ValueError("quality state must follow H2 gate statuses")

    @property
    def claim_policy(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                "opportunities": "available",
                "target_universe_observations": (
                    "bounded" if self.state == "accepted" else "qualified"
                ),
                "broad_market_claims": "disabled",
                "skill_semantics": "mentions_only",
                "fit_probability": "not_provided",
            }
        )

    @property
    def compensation_coverage(self) -> Decimal | None:
        value = self.observed_values["compensation_coverage"]
        return value if isinstance(value, Decimal) else None

    @classmethod
    def from_evidence_result(cls, result: HybridResult) -> TargetUniverseQuality:
        """Derive H2 quality from already-reconciled H1 evidence mechanics."""
        sample = result.observations.sample
        matching_ids = sample.job_ids
        matching_companies = sample.company_names
        internal_gates = {gate.gate_id: gate for gate in result.quality.gates}
        internal_evaluations = dict(result.quality.evaluation_provenance)
        review_evaluation = internal_evaluations["reviewed_eligible_jobs"]
        review_ref = review_evaluation.provenance.ref
        reviewed_ids = review_evaluation.provenance.reviewed_job_ids
        false_positive_gate = internal_gates["demonstrated_eligibility_false_positives"]
        false_positive_ids = false_positive_gate.evidence_job_ids
        complete_review = bool(matching_ids) and reviewed_ids == matching_ids
        reviewed_status = "passed" if complete_review else "not_evaluable"
        false_positive_status = (
            "passed"
            if complete_review and not false_positive_ids
            else "failed"
            if complete_review
            else "not_evaluable"
        )
        reviewed_companies = tuple(
            sorted(
                {
                    opportunity.company_name
                    for opportunity in result.opportunities.items
                    if opportunity.job_id in set(reviewed_ids)
                }
            )
        )
        false_positive_companies = tuple(
            sorted(
                {
                    opportunity.company_name
                    for opportunity in result.opportunities.items
                    if opportunity.job_id in set(false_positive_ids)
                }
            )
        )
        reconciliation_ref = "h2.target-universe.v1#opportunity-evidence-reconciled"
        boundary_ref = "h2.target-universe.v1#target-universe-boundary"
        gates = (
            HybridGate(
                "target_universe_boundary",
                "passed",
                "==",
                "resolved",
                "resolved",
                "boundary",
                matching_ids,
                matching_companies,
            ),
            HybridGate(
                "opportunity_evidence_reconciled",
                "passed",
                "==",
                "reconciled",
                "reconciled",
                "evidence",
                matching_ids,
                matching_companies,
            ),
            HybridGate(
                "reviewed_eligible_opportunities",
                reviewed_status,
                "==",
                len(matching_ids),
                len(reviewed_ids),
                "reviewed_jobs",
                reviewed_ids,
                reviewed_companies,
            ),
            HybridGate(
                "demonstrated_eligibility_false_positives",
                false_positive_status,
                "==",
                0,
                len(false_positive_ids),
                "false_positive_jobs",
                false_positive_ids,
                false_positive_companies,
            ),
            HybridGate(
                "skill_semantics",
                "passed",
                "==",
                "mentions_only",
                "mentions_only",
                "semantics",
                internal_gates["skill_semantics"].evidence_job_ids,
                internal_gates["skill_semantics"].evidence_company_names,
            ),
            HybridGate(
                "compensation_reconciled",
                "passed",
                "==",
                "reconciled",
                "reconciled",
                "evidence",
                internal_gates["compensation_reconciled"].evidence_job_ids,
                internal_gates["compensation_reconciled"].evidence_company_names,
            ),
        )
        evaluations = (
            (
                "target_universe_boundary",
                HybridEvaluation(
                    "contract_invariant", HybridProvenance("contract_rule", boundary_ref, ())
                ),
            ),
            (
                "opportunity_evidence_reconciled",
                HybridEvaluation(
                    "contract_invariant",
                    HybridProvenance("contract_rule", reconciliation_ref, ()),
                ),
            ),
            (
                "reviewed_eligible_opportunities",
                HybridEvaluation(
                    "manual_review", HybridProvenance("review_artifact", review_ref, reviewed_ids)
                ),
            ),
            (
                "demonstrated_eligibility_false_positives",
                HybridEvaluation(
                    "manual_review", HybridProvenance("review_artifact", review_ref, reviewed_ids)
                ),
            ),
            ("skill_semantics", internal_evaluations["skill_semantics"]),
            ("compensation_reconciled", internal_evaluations["compensation_reconciled"]),
        )
        values: Mapping[str, object] = {
            "matching_opportunity_count": len(matching_ids),
            "source_panel_source_count": sample.source_coverage.configured_source_count,
            "reviewed_eligible_opportunity_count": len(reviewed_ids),
            "demonstrated_eligibility_false_positive_count": len(false_positive_ids),
            "target_universe_boundary_resolved": True,
            "opportunity_evidence_reconciled": True,
            "skill_semantics": "mentions_only",
            "compensation_reconciled": True,
            "source_coverage": sample.source_coverage.source_share,
            "compensation_coverage": _share(sample.compensation_job_count, sample.job_count),
            "collection_cutoff": sample.collection_cutoff,
        }
        failed = tuple(gate.gate_id for gate in gates if gate.status != "passed")
        return cls(
            state="accepted" if not failed else "evidence_limited",
            gates=gates,
            failed_gates=failed,
            evaluation_provenance=evaluations,
            observed_values=values,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "state": self.state,
            "claim_policy": dict(self.claim_policy),
            "gates": [
                {
                    "gate_id": gate.gate_id,
                    "status": gate.status,
                    "operator": gate.operator,
                    "threshold": gate.threshold,
                    "observed": gate.observed,
                    "unit": gate.unit,
                    "evidence_job_ids": [str(value) for value in gate.evidence_job_ids],
                    "evidence_company_names": list(gate.evidence_company_names),
                }
                for gate in self.gates
            ],
            "failed_gates": list(self.failed_gates),
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
                for key, evaluation in self.evaluation_provenance
            },
            "observed_values": {
                key: value.isoformat().replace("+00:00", "Z")
                if isinstance(value, datetime)
                else value
                for key, value in self.observed_values.items()
            },
        }


@dataclass(frozen=True, slots=True)
class TargetUniverseResult:
    """An H2 envelope around reconciled opportunity and panel evidence."""

    target_universe: TargetUniverse
    evidence_result: HybridResult
    quality: TargetUniverseQuality = field(init=False)

    def __post_init__(self) -> None:
        if self.evidence_result.filters != self.target_universe.as_market_filters():
            raise ValueError("target universe does not match compiled evidence selection")
        configured_sources = self.evidence_result.observations.sample.source_coverage
        if configured_sources.configured_source_keys != self.target_universe.source_keys:
            raise ValueError("configured source panel does not match target universe")
        expected_companies = self.target_universe.normalized_source_companies
        for opportunity in self.evidence_result.opportunities.items:
            expected_company = expected_companies.get(opportunity.source.key)
            if expected_company is None or opportunity.company_name.lower() != expected_company:
                raise ValueError("opportunity source/company identity is outside target universe")
        object.__setattr__(
            self, "quality", TargetUniverseQuality.from_evidence_result(self.evidence_result)
        )

    @property
    def contract_version(self) -> str:
        return CONTRACT_VERSION

    @property
    def opportunities(self) -> HybridOpportunities:
        return self.evidence_result.opportunities

    @property
    def observations(self) -> HybridObservations:
        return self.evidence_result.observations

    def to_payload(self) -> dict[str, object]:
        """Serialize the H2 boundary without leaking derived MarketFilters."""
        evidence = self.evidence_result.to_payload()
        observations = cast(dict[str, object], evidence["observations"])
        return {
            "contract_version": CONTRACT_VERSION,
            "scope": {
                "kind": "target_universe",
                "name": self.target_universe.name,
                "target_universe": self.target_universe.model_dump(mode="json"),
            },
            "profile": evidence["profile"],
            "quality": self.quality.to_payload(),
            "opportunities": evidence["opportunities"],
            "observations": {**observations, "scope": OBSERVATION_SCOPE},
        }
