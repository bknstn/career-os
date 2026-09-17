"""Readable projections of an auditable single-vacancy review."""

# ruff: noqa: RUF001

from career_os.application.career_review import CareerReview

SECTION_NAMES = {
    "required": "Требования",
    "preferred": "Предпочтения работодателя",
    "responsibility": "Обязанности",
    "context": "Контекст",
}
STATUS_NAMES = {
    "user_confirmed": "подтверждено пользователем",
    "document_unreviewed": "из документа, требует проверки",
    "interpretation": "интерпретация",
    "unknown": "неизвестно",
}
RELATION_NAMES = {
    "relevant": "есть релевантные свидетельства",
    "partial": "частичные свидетельства",
    "not_established": "соответствие не установлено",
}
DIMENSIONS = {
    "role": "Направление",
    "location": "География",
    "work_mode": "Формат работы",
    "employment": "Договор",
    "base_pay": "Базовая зарплата",
}


def _text(value: str) -> str:
    # Keep user/document text as text, including in Markdown tables.
    for char in ("\\", "`", "*", "_", "[", "]", "<", ">", "|", "#"):
        value = value.replace(char, "\\" + char)
    return value.replace("\n", " ").replace("\r", " ")


def render_career_review(review: CareerReview) -> str:
    vacancy = review.vacancy
    target = review.profile.target
    has_evidence = any(row.relation != "not_established" for row in review.rows)
    opening = (
        "Есть пересечения по опыту; соответствие всем требованиям не установлено. "
        if has_evidence
        else "Релевантные свидетельства пока не установлены. "
    )
    roles = {"backend_engineering": "Backend", "platform_engineering": "Platform"}
    periods = {"month": "месяц", "year": "год", "hour": "час"}
    role_names = ", ".join(roles.get(role, role) for role in target.role_families)
    remote = "допустима" if target.remote_from_country else "не выбрана"
    relocation = "допустима" if target.relocation else "исключена"
    lines = [
        f"# Разбор: {_text(vacancy.company)} — {_text(vacancy.title)}",
        "",
        opening
        + "Итог по личным ограничениям: "
        + {
            "unresolved": "остаются неизвестные условия.",
            "conflict": "есть несовместимые условия.",
            "compatible_by_manual_review": "совместимы по ручной проверке; это не оценка найма.",
        }[review.constraint_status],
        "",
        f"Дата разбора: {review.reviewed_on}. Снимок вакансии: {vacancy.captured_on}. "
        f"Профиль: {review.profile.evidence_as_of}, статус {_text(review.profile.review_status)}.",
        "Метод: ручное сопоставление свидетельств с автоматической проверкой ссылок и сборкой "
        "отчёта. Это один пример; выводы о рынке, готовности к уровню и вероятности найма "
        "не рассчитываются. Релевантное свидетельство не означает выполнение требования.",
        "",
        f"Источник: {_text(vacancy.source_url)}. Сохранённый ответ: "
        f"{_text(vacancy.retained_reference)}. Доступность относится к дате снимка.",
        "",
        "## Личные условия",
        "",
        f"Цель: {role_names}, {target.primary_seniority.title()}; "
        f"возможные следующие уровни: {', '.join(target.exploratory_seniorities)}. "
        f"{target.country}; офис/гибрид: {', '.join(target.onsite_or_hybrid_cities)}; "
        f"удалённая работа из страны: {remote}; релокация {relocation}. "
        f"{target.employment_model}, минимум {target.minimum_base_pay} {target.currency} "
        f"{target.pay_basis}/{periods[target.pay_period]} базой, без бонуса и акций.",
        "",
        "| Условие | Состояние | Основание и следующий шаг |",
        "| --- | --- | --- |",
    ]
    for item in review.plan.constraints:
        status = {"compatible": "совместимо", "unknown": "неизвестно", "conflict": "конфликт"}[
            item.status
        ]
        lines.append(
            f"| {DIMENSIONS[item.dimension]} | {status} | "
            f"{_text(item.rationale)} {_text(item.next_check)} |"
        )
    lines.extend(["", "## Что влияет на решение в первую очередь", ""])
    for condition in review.plan.constraints:
        if condition.status != "compatible":
            lines.append(f"- {DIMENSIONS[condition.dimension]}: {_text(condition.next_check)}")
    for priority in review.plan.priority_evidence_ids:
        focus = next(row for row in review.rows if row.evidence_id == priority)
        lines.append(f"- {_text(focus.summary)}: {_text(focus.rationale)}")
    lines.extend(
        [
            "",
            "Далее — полный разбор свидетельств. Он не требует отвечать "
            "на все вопросы до решения, стоит ли обсуждать вакансию.",
        ]
    )
    for section in ("required", "responsibility", "preferred", "context"):
        rows = [row for row in review.rows if row.section == section]
        if not rows:
            continue
        lines.extend(["", f"## {SECTION_NAMES[section]}", ""])
        for row in rows:
            lines.extend(
                [
                    f"### {_text(row.summary)}",
                    "",
                    f"**{RELATION_NAMES[row.relation]}.** {_text(row.rationale)}",
                    "",
                ]
            )
            for profile_item in row.profile_evidence:
                lines.append(
                    f"- `{profile_item.reference.collection}:{profile_item.reference.id}` "
                    f"({STATUS_NAMES[profile_item.evidence.status]}): "
                    f"{_text(profile_item.statement)} "
                    f"[источники: {', '.join(profile_item.evidence.source_ids)}]"
                )
            if row.profile_evidence:
                lines.append("")
            evidence = next(item for item in vacancy.evidence if item.id == row.evidence_id)
            lines.extend(
                [
                    f"Проверить: {_text(row.next_check)}",
                    "",
                    f"Вакансия: `{row.evidence_id}`, символы {evidence.start}:{evidence.end} "
                    "в сохранённом тексте (полная формулировка доступна в JSON).",
                    "",
                ]
            )
    lines.extend(["## Источники профиля", ""])
    for source in review.profile.sources:
        lines.append(f"- `{source.id}`: {_text(source.reference)} — {_text(source.locator)}")
    lines.extend(
        [
            "",
            "## Ограничения",
            "",
            "Проверяются все пункты трёх разделов выбранного снимка. "
            "Смысл ручных сопоставлений требует проверки человеком. "
            "Отсутствие сведений не считается отсутствием опыта; предпочтение работодателя "
            "не превращается в обязательное требование. Указанные в одном пункте языки "
            "не считаются независимыми обязательными навыками.",
            "",
        ]
    )
    return "\n".join(lines)
