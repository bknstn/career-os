"""Bounded, Decimal-only comparison of one current role and one opportunity."""

# ruff: noqa: RUF001

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field

from career_os.config.career_profiles import CareerProfile, Record, Text
from career_os.config.compensation_supplements import (
    CompensationLedger,
    CompensationObservation,
    CompensationSource,
)
from career_os.config.sources import ConfigurationError

ComparablePeriod = Literal["month", "year"]


class NormalizedCompensation(Record):
    minimum_monthly: Decimal = Field(gt=0)
    maximum_monthly: Decimal = Field(gt=0)
    minimum_annual: Decimal = Field(gt=0)
    maximum_annual: Decimal = Field(gt=0)


class FloorCheck(Record):
    status: Literal["meets", "partial", "not_met", "unknown"]
    rationale: Text
    monthly_floor: Decimal = Field(gt=0)


class CompensationTarget(Record):
    currency: str
    pay_basis: Literal["gross", "net"]
    employment_model: Text
    monthly_floor: Decimal = Field(gt=0)


class CompensationComparison(Record):
    schema_version: Literal["compensation-comparison.v1"] = "compensation-comparison.v1"
    sources: tuple[CompensationSource, ...]
    history: tuple[CompensationObservation, ...]
    target: CompensationTarget
    current_role: CompensationObservation
    opportunity: CompensationObservation
    current_role_normalized: NormalizedCompensation | None
    opportunity_normalized: NormalizedCompensation | None
    comparable: bool
    non_comparable_reasons: tuple[Text, ...]
    monthly_delta_minimum: Decimal | None
    monthly_delta_maximum: Decimal | None
    monthly_change_percent_minimum: Decimal | None
    monthly_change_percent_maximum: Decimal | None
    floor: FloorCheck


def build_compensation_comparison(
    ledger: CompensationLedger,
    profile: CareerProfile,
    *,
    current_role_id: str,
    opportunity_id: str,
) -> CompensationComparison:
    """Compare only explicit, matching UoP/base/currency/basis evidence.

    The H3 profile provides the floor but remains unmodified. Unknown or incompatible
    metadata makes the result visibly non-comparable; no employment or tax conversion
    is attempted.
    """
    if current_role_id not in {job.id for job in profile.employment if job.end is None}:
        raise ConfigurationError(
            "current role compensation subject is not an ongoing H3 employment"
        )
    current = _active_observation(ledger, kind="current_role", subject_id=current_role_id)
    opportunity = _active_observation(ledger, kind="opportunity", subject_id=opportunity_id)

    target = profile.target
    floor_monthly = _period_amount(target.minimum_base_pay, target.pay_period, "month")
    comparison_target = CompensationTarget(
        currency=target.currency,
        pay_basis=target.pay_basis,
        employment_model=target.employment_model,
        monthly_floor=floor_monthly,
    )
    current_reasons = _compatibility_reasons(current, profile, label="current role")
    opportunity_reasons = _compatibility_reasons(opportunity, profile, label="opportunity")
    reasons = tuple(dict.fromkeys((*current_reasons, *opportunity_reasons)))
    current_normalized = _normalize(current) if not current_reasons else None
    opportunity_normalized = _normalize(opportunity) if not opportunity_reasons else None
    floor = (
        _floor_check(opportunity_normalized, floor_monthly)
        if opportunity_normalized is not None
        else FloorCheck(
            status="unknown",
            rationale="Нельзя проверить минимум без сопоставимой подтверждённой базы возможности.",
            monthly_floor=floor_monthly,
        )
    )

    if current_normalized is None or opportunity_normalized is None:
        return CompensationComparison(
            sources=ledger.sources,
            history=ledger.observations,
            target=comparison_target,
            current_role=current,
            opportunity=opportunity,
            current_role_normalized=current_normalized,
            opportunity_normalized=opportunity_normalized,
            comparable=False,
            non_comparable_reasons=reasons,
            monthly_delta_minimum=None,
            monthly_delta_maximum=None,
            monthly_change_percent_minimum=None,
            monthly_change_percent_maximum=None,
            floor=floor,
        )

    delta_minimum = opportunity_normalized.minimum_monthly - current_normalized.maximum_monthly
    delta_maximum = opportunity_normalized.maximum_monthly - current_normalized.minimum_monthly
    percent_minimum = delta_minimum / current_normalized.maximum_monthly * Decimal("100")
    percent_maximum = delta_maximum / current_normalized.minimum_monthly * Decimal("100")
    return CompensationComparison(
        sources=ledger.sources,
        history=ledger.observations,
        target=comparison_target,
        current_role=current,
        opportunity=opportunity,
        current_role_normalized=current_normalized,
        opportunity_normalized=opportunity_normalized,
        comparable=True,
        non_comparable_reasons=(),
        monthly_delta_minimum=delta_minimum,
        monthly_delta_maximum=delta_maximum,
        monthly_change_percent_minimum=percent_minimum,
        monthly_change_percent_maximum=percent_maximum,
        floor=floor,
    )


def _active_observation(
    ledger: CompensationLedger, *, kind: str, subject_id: str
) -> CompensationObservation:
    superseded = {item.supersedes for item in ledger.observations if item.supersedes is not None}
    matches = [
        item
        for item in ledger.observations
        if item.id not in superseded and item.subject.kind == kind and item.subject.id == subject_id
    ]
    if not matches:
        raise ConfigurationError(f"no active compensation observation for {kind}:{subject_id}")
    # Ledger validation prevents more than one active match for a subject.
    return matches[0]


def _compatibility_reasons(
    observation: CompensationObservation, profile: CareerProfile, *, label: str
) -> tuple[str, ...]:
    amount = observation.amount
    target = profile.target
    reasons: list[str] = []
    if amount.minimum is None:
        reasons.append(f"{label}: сумма не раскрыта")
    if amount.currency is None:
        reasons.append(f"{label}: валюта не раскрыта")
    elif amount.currency != target.currency:
        reasons.append(f"{label}: валюта {amount.currency} не совпадает с {target.currency}")
    if amount.period == "unknown":
        reasons.append(f"{label}: период оплаты не раскрыт")
    elif amount.period not in ("month", "year"):
        reasons.append(f"{label}: период оплаты не поддержан")
    if amount.basis == "unknown":
        reasons.append(f"{label}: gross/net не раскрыт")
    elif amount.basis != target.pay_basis:
        reasons.append(f"{label}: basis {amount.basis} не совпадает с {target.pay_basis}")
    if amount.component == "unknown":
        reasons.append(f"{label}: base/total не раскрыт")
    elif amount.component != "base":
        reasons.append(f"{label}: указано {amount.component}, а не base")
    if amount.employment_model == "unknown":
        reasons.append(f"{label}: тип договора не раскрыт")
    elif amount.employment_model != target.employment_model:
        reasons.append(
            f"{label}: договор {amount.employment_model} не совпадает с {target.employment_model}"
        )
    return tuple(reasons)


def _normalize(observation: CompensationObservation) -> NormalizedCompensation:
    amount = observation.amount
    if amount.minimum is None or amount.maximum is None or amount.period not in ("month", "year"):
        raise AssertionError("only compatible compensation observations are normalized")
    return NormalizedCompensation(
        minimum_monthly=_period_amount(amount.minimum, amount.period, "month"),
        maximum_monthly=_period_amount(amount.maximum, amount.period, "month"),
        minimum_annual=_period_amount(amount.minimum, amount.period, "year"),
        maximum_annual=_period_amount(amount.maximum, amount.period, "year"),
    )


def _period_amount(value: Decimal, source: str, target: ComparablePeriod) -> Decimal:
    if source == target:
        return value
    if source == "year" and target == "month":
        return value / Decimal("12")
    if source == "month" and target == "year":
        return value * Decimal("12")
    raise ConfigurationError("only month/year compensation periods can be compared")


def _floor_check(opportunity: NormalizedCompensation, monthly_floor: Decimal) -> FloorCheck:
    if opportunity.minimum_monthly >= monthly_floor:
        return FloorCheck(
            status="meets",
            rationale="Весь заявленный диапазон базы не ниже личного минимума.",
            monthly_floor=monthly_floor,
        )
    if opportunity.maximum_monthly < monthly_floor:
        return FloorCheck(
            status="not_met",
            rationale="Верхняя граница заявленной базы ниже личного минимума.",
            monthly_floor=monthly_floor,
        )
    return FloorCheck(
        status="partial",
        rationale="Диапазон пересекает личный минимум; достижимая база остаётся неизвестной.",
        monthly_floor=monthly_floor,
    )
