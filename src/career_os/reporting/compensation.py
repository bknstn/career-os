"""Readable projection of the bounded compensation comparison."""

# ruff: noqa: RUF001

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from career_os.application.compensation import CompensationComparison
from career_os.config.compensation_supplements import CompensationObservation


def render_compensation_comparison(comparison: CompensationComparison) -> str:
    """Render the same canonical comparison object emitted as JSON by the CLI."""
    current = comparison.current_role
    opportunity = comparison.opportunity
    lines = [
        f"# Сравнение компенсации: {current.subject.label} и {opportunity.subject.label}",
        "",
        "Сведения об оплате добавлены вручную с указанием источника и даты. "
        "Диапазон рекрутера не является оффером.",
        "",
        "| Сторона | Заявленная компенсация | Условия и происхождение |",
        "| --- | --- | --- |",
        _observation_row(current, comparison),
        _observation_row(opportunity, comparison),
        "",
        "## Сопоставимость и минимум",
        "",
    ]
    if not comparison.comparable:
        lines.extend(
            [
                "Суммы не сопоставимы; разница не рассчитана. Минимум возможности "
                "проверяется независимо, если её условия позволяют.",
                "",
                *[f"- {reason}" for reason in comparison.non_comparable_reasons],
                "",
                f"Личный минимум: {_money(comparison.floor.monthly_floor)} "
                f"{comparison.target.currency} {comparison.target.pay_basis}/base в месяц; "
                f"Статус: {_floor_name(comparison.floor.status)}. {comparison.floor.rationale}",
            ]
        )
    else:
        assert comparison.current_role_normalized is not None
        assert comparison.opportunity_normalized is not None
        currency = comparison.target.currency
        current_monthly = _period_range(
            comparison.current_role_normalized.minimum_monthly,
            comparison.current_role_normalized.maximum_monthly,
            currency,
            "месяц",
        )
        current_annual = _period_range(
            comparison.current_role_normalized.minimum_annual,
            comparison.current_role_normalized.maximum_annual,
            currency,
            "год",
        )
        opportunity_monthly = _period_range(
            comparison.opportunity_normalized.minimum_monthly,
            comparison.opportunity_normalized.maximum_monthly,
            currency,
            "месяц",
        )
        opportunity_annual = _period_range(
            comparison.opportunity_normalized.minimum_annual,
            comparison.opportunity_normalized.maximum_annual,
            currency,
            "год",
        )
        monthly_delta = _period_range(
            comparison.monthly_delta_minimum,
            comparison.monthly_delta_maximum,
            currency,
            "месяц",
            signed=True,
        )
        percent_delta = _signed_range(
            comparison.monthly_change_percent_minimum,
            comparison.monthly_change_percent_maximum,
            suffix="%",
        )
        lines.extend(
            [
                "| Показатель | Значение |",
                "| --- | --- |",
                f"| Текущая роль | {current_monthly}; {current_annual} |",
                f"| Возможность | {opportunity_monthly}; {opportunity_annual} |",
                f"| Разница | {monthly_delta}; {percent_delta} |",
                "",
                f"Личный минимум: {_money(comparison.floor.monthly_floor)} "
                f"{comparison.target.currency} {comparison.target.pay_basis}/base в месяц; "
                f"Статус: {_floor_name(comparison.floor.status)}. {comparison.floor.rationale}",
            ]
        )
    lines.extend(["", "## История сведений об оплате", ""])
    superseded = {item.supersedes for item in comparison.history if item.supersedes is not None}
    lines.extend(
        [
            "| Запись | Статус и дата раскрытия | Сумма | Заменяет |",
            "| --- | --- | --- |",
        ]
    )
    for observation in comparison.history:
        state = "заменена" if observation.id in superseded else "активна"
        lines.append(
            f"| `{observation.id}` | {state}; {_source_summary(observation, comparison)}; "
            f"раскрыто {observation.disclosed_on.isoformat()} | "
            f"{_amount_summary(observation)} | `{observation.supersedes or '—'}` |"
        )
    lines.extend(["", "## Источники", ""])
    for source in comparison.sources:
        lines.append(
            f"- `{source.id}` ({source.recorded_on.isoformat()}): {source.reference} — "
            f"{source.locator}"
        )
    notes = [item for item in comparison.history if item.note is not None]
    if notes:
        lines.extend(["", "## Примечания записей", ""])
        for observation in notes:
            lines.append(f"- `{observation.id}`: {observation.note}")
    lines.extend(
        [
            "",
            "Нет преобразований gross/net, base/total, валюты или типа договора. "
            "Неизвестные и несовпадающие условия остаются видимыми и не доказывают "
            "соответствие минимуму.",
            "",
        ]
    )
    return "\n".join(lines)


def _observation_row(
    observation: CompensationObservation, comparison: CompensationComparison
) -> str:
    return (
        f"| {observation.subject.label} | {_amount_summary(observation)} | "
        f"{observation.amount.basis}/{observation.amount.component}/"
        f"{observation.amount.employment_model}; "
        f"{_source_summary(observation, comparison)} |"
    )


def _amount_summary(observation: CompensationObservation) -> str:
    amount = observation.amount
    if amount.minimum is None:
        return "не раскрыта"
    assert amount.maximum is not None and amount.currency is not None
    return (
        f"{_range(amount.minimum, amount.maximum)} {amount.currency}/{_period_name(amount.period)}"
    )


def _source_summary(
    observation: CompensationObservation, comparison: CompensationComparison
) -> str:
    source = next(item for item in comparison.sources if item.id == observation.source_id)
    disclosure = {
        "current_compensation": "текущая компенсация",
        "recruiter_range": "диапазон рекрутера, не оффер",
        "offer": "оффер",
        "unknown": "тип раскрытия неизвестен",
    }[observation.disclosure_kind]
    return f"{disclosure}; пользователь сообщил {source.recorded_on.isoformat()}"


def _period_name(period: str) -> str:
    return {"month": "месяц", "year": "год", "unknown": "неизвестный период"}[period]


def _floor_name(status: str) -> str:
    return {
        "meets": "минимум подтверждён диапазоном",
        "partial": "частично подтверждён",
        "not_met": "не соответствует",
        "unknown": "неизвестно",
    }[status]


def _range(minimum: Decimal, maximum: Decimal) -> str:
    return _money(minimum) if minimum == maximum else f"{_money(minimum)}–{_money(maximum)}"


def _period_range(
    minimum: Decimal | None,
    maximum: Decimal | None,
    currency: str,
    period: str,
    *,
    signed: bool = False,
) -> str:
    if signed:
        return _signed_range(minimum, maximum) + f" {currency}/{period}"
    assert minimum is not None and maximum is not None
    return _range(minimum, maximum) + f" {currency}/{period}"


def _signed_range(minimum: Decimal | None, maximum: Decimal | None, *, suffix: str = "") -> str:
    assert minimum is not None and maximum is not None
    return (
        _signed(minimum, suffix=suffix)
        if minimum == maximum
        else (f"{_signed(minimum, suffix=suffix)}–{_signed(maximum, suffix=suffix)}")
    )


def _signed(value: Decimal, *, suffix: str) -> str:
    sign = "+" if value >= 0 else ""
    return sign + _money(value, decimal_places=3 if suffix else None) + suffix


def _money(value: Decimal, *, decimal_places: int | None = None) -> str:
    if decimal_places is not None:
        value = value.quantize(Decimal("1." + "0" * decimal_places), rounding=ROUND_HALF_UP)
        rendered = f"{value:.{decimal_places}f}"
    else:
        rendered = f"{value.quantize(Decimal('1.00'), rounding=ROUND_HALF_UP):.2f}"
    sign = "-" if rendered.startswith("-") else ""
    absolute = rendered.removeprefix("-")
    integer, dot, fraction = absolute.partition(".")
    grouped = f"{int(integer):,}".replace(",", " ")
    return sign + grouped + (dot + fraction if dot else "")
