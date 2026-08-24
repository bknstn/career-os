"""Versioned deterministic normalizers for non-skill canonical job fields."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Final, cast

from career_os.domain.normalization import (
    CompensationComponentType,
    CompensationInterval,
    JobNormalization,
    JobNormalizationInput,
    NormalizationMethod,
    NormalizedCompensation,
    NormalizedField,
    NormalizedLocation,
    SourceCompensationEvidence,
    SourceLocationEvidence,
)
from career_os.normalization.text import clean_text

JOB_NORMALIZER_VERSION: Final = "job-normalization-v5"

_TITLE_SENIORITY: Final = (
    ("executive", re.compile(r"\b(?:chief|cto|vp|vice president)\b", re.I)),
    ("director", re.compile(r"\bdirector\b", re.I)),
    ("manager", re.compile(r"\b(?:engineering|people) manager\b", re.I)),
    ("principal", re.compile(r"\bprincipal\b", re.I)),
    ("staff", re.compile(r"\bstaff\b", re.I)),
    ("lead", re.compile(r"\b(?:tech(?:nical)? lead|lead)\b", re.I)),
    ("senior", re.compile(r"\b(?:senior|sr\.)\b", re.I)),
    ("mid", re.compile(r"\b(?:mid(?:-level)?|intermediate)\b", re.I)),
    ("junior", re.compile(r"\b(?:junior|jr\.)\b", re.I)),
    ("intern", re.compile(r"\b(?:intern|internship)\b", re.I)),
)
_ROLE_FAMILIES: Final = (
    ("engineering_management", re.compile(r"\b(?:engineering|software) manager\b", re.I)),
    ("product_management", re.compile(r"\bproduct manage(?:r|ment)\b", re.I)),
    ("data_engineering", re.compile(r"\bdata engineer(?:ing)?\b", re.I)),
    ("data_science", re.compile(r"\bdata scient(?:ist|ce)\b", re.I)),
    (
        "platform_engineering",
        re.compile(
            r"\b(?:platform engineer(?:ing)?|platform developer|site reliability|sre)\b"
            r"|\b(?:software|systems?) engineer\b[^\n]*\b(?:platforms?|infrastructure)\b"
            r"|\b(?:platforms?|infrastructure)\b[^\n]*\b(?:software|systems?) engineer\b"
            r"|\binfrastructure engineer(?:ing)?\b",
            re.I,
        ),
    ),
    ("devops_engineering", re.compile(r"\bdevops\b", re.I)),
    ("backend_engineering", re.compile(r"\b(?:backend|back-end|server-side)\b", re.I)),
    ("frontend_engineering", re.compile(r"\b(?:frontend|front-end)\b", re.I)),
    ("fullstack_engineering", re.compile(r"\b(?:full[ -]?stack)\b", re.I)),
    ("cloud_engineering", re.compile(r"\bcloud engineer(?:ing)?\b", re.I)),
)
_SOURCE_SENIORITY: Final = {
    "intern": "intern",
    "internship": "intern",
    "junior": "junior",
    "jr": "junior",
    "mid": "mid",
    "intermediate": "mid",
    "senior": "senior",
    "lead": "lead",
    "staff": "staff",
    "principal": "principal",
    "manager": "manager",
    "director": "director",
    "executive": "executive",
}
_DESCRIPTION_SENIORITY: Final = re.compile(
    r"\b(?P<level>senior|staff|principal|junior|mid-level)"
    r"(?:-level|\s+level)?\s+(?:engineer|developer)\s+role\b",
    re.I,
)
_EMPLOYMENT: Final = {
    "fulltime": "full_time",
    "full time": "full_time",
    "full-time": "full_time",
    "full_time": "full_time",
    "parttime": "part_time",
    "part time": "part_time",
    "part-time": "part_time",
    "part_time": "part_time",
    "contract": "contract",
    "contractor": "contract",
    "internship": "internship",
    "intern": "internship",
    "temporary": "temporary",
    "temp": "temporary",
    "other": "other",
}
_EMPLOYMENT_PROSE: Final = (
    (
        "full_time",
        re.compile(
            r"\b(?:full[- ]?time)\s+(?:role|position|job|employment|engagement)\b"
            r"|\b(?:role|position|job|employment|engagement)\s+(?:is\s+)?full[- ]?time\b",
            re.I,
        ),
    ),
    (
        "part_time",
        re.compile(
            r"\b(?:part[- ]?time)\s+(?:role|position|job|employment|engagement)\b"
            r"|\b(?:role|position|job|employment|engagement)\s+(?:is\s+)?part[- ]?time\b",
            re.I,
        ),
    ),
    (
        "contract",
        re.compile(
            r"\bcontract\s+(?:role|position|job|employment|engagement)\b"
            r"|\b(?:role|position|job|employment|engagement)\s+(?:is\s+)?contract\b",
            re.I,
        ),
    ),
    (
        "internship",
        re.compile(
            r"\b(?:internship|intern)\s+(?:role|position|job|employment)\b"
            r"|\b(?:role|position|job|employment)\s+(?:is\s+)?(?:an\s+)?(?:internship|intern)\b",
            re.I,
        ),
    ),
    (
        "temporary",
        re.compile(
            r"\b(?:temporary|temp)\s+(?:role|position|job|employment|engagement)\b"
            r"|\b(?:role|position|job|employment|engagement)\s+(?:is\s+)?temporary\b",
            re.I,
        ),
    ),
)
_EMPLOYMENT_TOKEN: Final = re.compile(
    r"\b(?:full[- ]?time|part[- ]?time|contract|internship|intern|temporary|temp)\b", re.I
)
_WORKPLACE: Final = {
    "remote": "remote",
    "hybrid": "hybrid",
    "onsite": "onsite",
    "on-site": "onsite",
    "on site": "onsite",
    "unspecified": "unspecified",
}
_COUNTRIES: Final = {
    "poland": "PL",
    "polska": "PL",
    "pl": "PL",
    "united kingdom": "GB",
    "uk": "GB",
    "germany": "DE",
    "de": "DE",
}
_CURRENCY_TOKEN: Final = r"USD|EUR|PLN|GBP|€|£|zł"
_AMOUNT_TOKEN: Final = r"\d+(?:[,.]\d+)?(?:\s*\d{3})?[kKmM]?"
_TEXT_RANGE: Final = re.compile(
    rf"(?:(?P<currency_before_min>{_CURRENCY_TOKEN})\s*)?"
    rf"(?P<minimum>{_AMOUNT_TOKEN})\s*"
    rf"(?P<currency_after_min>{_CURRENCY_TOKEN})?\s*"
    r"(?:-|\u2013|to)\s*"
    rf"(?:(?P<currency_before_max>{_CURRENCY_TOKEN})\s*)?"
    rf"(?P<maximum>{_AMOUNT_TOKEN})\s*"
    rf"(?P<currency_after_max>{_CURRENCY_TOKEN})?",
    re.I,
)
_COMPONENT_MARKER: Final = re.compile(r"\b(?:base\s+(?:salary|pay)|salary|bonus|equity)\b", re.I)
_INTERVAL_MARKER: Final = re.compile(
    r"\b(?:annual(?:ly)?|per\s+year|year|monthly|per\s+month|month|hourly|per\s+hour|hour|daily|per\s+day|day)\b",
    re.I,
)
_SENTENCE_BOUNDARY: Final = re.compile(r"[.;\n]")
_NEGATION_PREFIX: Final = re.compile(r"\b(?:not|no|without)\s+(?:a\s+|an\s+)?(?:fully\s+)?$", re.I)
_WORKPLACE_MATCHERS: Final = {
    "hybrid": re.compile(
        r"\bhybrid\s+(?:role|model|schedule|work(?:ing)?\s+arrangement)\b"
        r"|\b(?:role|model|schedule|work(?:ing)?\s+arrangement)\s+(?:is\s+)?hybrid\b",
        re.I,
    ),
    "remote": re.compile(
        r"\bfully\s+remote\b|\bremote\s+(?:role|job|work)\b|\bwork(?:ing)?\s+remotely\b",
        re.I,
    ),
    "onsite": re.compile(
        r"\b(?:on[ -]?site|office-based)\s+(?:role|job|work|position|attendance)\b"
        r"|\b(?:role|job|position)\s+(?:is\s+)?(?:on[ -]?site|office-based)\b",
        re.I,
    ),
}
_REMOTE_SCOPE_TOKENS: Final = (
    ("Poland", r"(?:poland|pl)"),
    ("EU", r"(?:eu|european\s+union)"),
    ("Europe", r"europe"),
)
_CURRENCY: Final = {
    "€": "EUR",
    "£": "GBP",
    "USD": "USD",
    "EUR": "EUR",
    "PLN": "PLN",
    "GBP": "GBP",
    "ZŁ": "PLN",
}


def normalize_job(value: JobNormalizationInput) -> JobNormalization:
    """Normalize source facts deterministically; absent evidence remains explicitly unknown."""
    title = clean_text(value.title)
    description = clean_text(value.description)
    return JobNormalization(
        normalizer_version=JOB_NORMALIZER_VERSION,
        title_normalized=title,
        role_family=_role_family(title),
        seniority=_seniority(title, value.source_level, value.source_category, description),
        locations=_locations(value.locations),
        employment_type=_employment(value.source_employment_type, description),
        workplace_type=_workplace(
            value.source_workplace_type,
            value.source_is_remote,
            value.locations,
            description,
        ),
        remote_scope=_remote_scope(
            value.locations,
            description,
            value.source_workplace_type,
            value.source_is_remote,
        ),
        compensation=_compensation(value.structured_compensation, value.compensation_text),
    )


def _role_family(title: str) -> NormalizedField:
    matches = [(role, pattern.search(title)) for role, pattern in _ROLE_FAMILIES]
    matched = [(role, match) for role, match in matches if match is not None]
    explicit_fullstack = next(
        ((role, match) for role, match in matched if role == "fullstack_engineering"), None
    )
    if explicit_fullstack is not None:
        role, match = explicit_fullstack
        return _field(role, "title_rule", match.group(0), Decimal("0.900"))
    non_platform = [(role, match) for role, match in matched if role != "platform_engineering"]
    if len(non_platform) == 1:
        role, match = non_platform[0]
        return _field(role, "title_rule", match.group(0), Decimal("0.900"))
    if len(matched) == 1:
        role, match = matched[0]
        return _field(role, "title_rule", match.group(0), Decimal("0.900"))
    return NormalizedField(None)


def _seniority(
    title: str, source_level: str | None, source_category: str | None, description: str
) -> NormalizedField:
    title_matches = [
        (seniority, match)
        for seniority, pattern in _TITLE_SENIORITY
        for match in (pattern.search(title),)
        if match is not None
    ]
    if len(title_matches) > 1:
        return NormalizedField(None)
    if title_matches:
        seniority, match = title_matches[0]
        return _field(seniority, "title_rule", match.group(0), Decimal("1.000"))
    for source_value in (source_level, source_category):
        if source_value is None:
            continue
        normalized = _SOURCE_SENIORITY.get(_key(source_value))
        if normalized is not None:
            return _field(normalized, "source_level", source_value, Decimal("0.900"))
    description_match = _DESCRIPTION_SENIORITY.search(description)
    if description_match is not None:
        first_token = description_match.group("level").casefold().rstrip(".")
        if first_token == "mid-level":
            first_token = "mid"
        normalized = _SOURCE_SENIORITY.get(first_token)
        if normalized is not None:
            return _field(
                normalized,
                "description_rule",
                description_match.group(0),
                Decimal("0.700"),
            )
    return NormalizedField(None)


def _locations(values: tuple[SourceLocationEvidence, ...]) -> tuple[NormalizedLocation, ...]:
    return tuple(_location(index, value) for index, value in enumerate(values))


def _location(ordinal: int, value: SourceLocationEvidence) -> NormalizedLocation:
    if value.city is not None or value.region is not None or value.country_code is not None:
        parsed_country = _parse_location(value.raw)[1]
        parsed_country_only = value.country_code is None and parsed_country is not None
        return NormalizedLocation(
            ordinal,
            value.raw,
            value.city,
            value.region,
            value.country_code or parsed_country,
            value.is_primary,
            Decimal("0.800") if parsed_country_only else Decimal("1.000"),
            "text_parse" if parsed_country_only else "structured_source",
        )
    city, country = _parse_location(value.raw)
    confidence = Decimal("0.800") if country is not None else Decimal("0.400")
    return NormalizedLocation(
        ordinal, value.raw, city, None, country, value.is_primary, confidence, "text_parse"
    )


def _parse_location(raw: str) -> tuple[str | None, str | None]:
    stripped = raw.strip()
    country_only = _country_code(stripped)
    if country_only is not None:
        return None, country_only

    comma_parts = [part.strip() for part in stripped.split(",") if part.strip()]
    if len(comma_parts) >= 2:
        country = _country_code(comma_parts[-1])
        if country is not None:
            return ", ".join(comma_parts[:-1]), country

    slash_match = re.fullmatch(r"(?P<city>.+?)\s*/\s*(?P<country>[A-Za-z ]+)", stripped)
    if slash_match is not None:
        country = _country_code(slash_match.group("country"))
        if country is not None:
            return slash_match.group("city").strip(), country

    hyphen_match = re.fullmatch(r"(?P<prefix>.+?)\s+-\s+(?P<country>[A-Za-z ]+)", stripped)
    if hyphen_match is not None:
        country = _country_code(hyphen_match.group("country"))
        if country is not None:
            prefix = hyphen_match.group("prefix").strip()
            return (None if prefix.casefold() == "remote" else prefix), country
    return None, None


def _country_code(value: str) -> str | None:
    return _COUNTRIES.get(value.casefold().strip())


def _employment(source_value: str | None, description: str) -> NormalizedField:
    if source_value is not None:
        normalized = _EMPLOYMENT.get(_key(source_value))
        if normalized is not None:
            return _field(normalized, "structured_source", source_value, Decimal("1.000"))
    if _has_conflicting_employment_tokens(description):
        return NormalizedField(None)
    matches = [
        (normalized, match)
        for normalized, pattern in _EMPLOYMENT_PROSE
        for match in pattern.finditer(description)
        if not _is_negated(description, match.start())
    ]
    categories = {normalized for normalized, _ in matches}
    if len(categories) == 1 and matches:
        normalized, match = matches[0]
        return _field(normalized, "description_rule", match.group(0), Decimal("0.700"))
    return NormalizedField(None)


def _has_conflicting_employment_tokens(description: str) -> bool:
    categories = {
        _EMPLOYMENT.get(_key(match.group(0)))
        for match in _EMPLOYMENT_TOKEN.finditer(description)
        if not _is_negated(description, match.start())
    }
    return len(categories - {None}) > 1


def _workplace(
    source_value: str | None,
    is_remote: bool | None,
    locations: tuple[SourceLocationEvidence, ...],
    description: str,
) -> NormalizedField:
    if source_value is not None:
        normalized = _WORKPLACE.get(_key(source_value))
        if normalized is not None:
            return _field(normalized, "structured_source", source_value, Decimal("1.000"))
    if is_remote is True:
        return _field("remote", "structured_source", "source_is_remote=true", Decimal("0.900"))
    hybrid = _affirmative_workplace_match(description, "hybrid")
    if hybrid is not None:
        return _field("hybrid", "description_rule", hybrid.group(0), Decimal("0.800"))
    if is_remote is False:
        return NormalizedField(None)
    remote_location = _remote_workplace_location_match(locations)
    if remote_location is not None:
        return _field("remote", "text_parse", remote_location.group(0), Decimal("0.800"))
    remote = _affirmative_workplace_match(description, "remote")
    onsite = _affirmative_workplace_match(description, "onsite")
    if remote is not None and onsite is None:
        return _field("remote", "description_rule", remote.group(0), Decimal("0.700"))
    if onsite is not None and remote is None:
        return _field("onsite", "description_rule", onsite.group(0), Decimal("0.700"))
    return NormalizedField(None)


def _remote_workplace_location_match(
    locations: tuple[SourceLocationEvidence, ...],
) -> re.Match[str] | None:
    for item in locations:
        if re.search(r"\b(?:hybrid|on[ -]?site|in-office)\b", item.raw, re.I) is not None:
            continue
        match = re.search(r"\bremote\b", item.raw, re.I)
        if match is not None:
            return match
    return None


def _affirmative_workplace_match(description: str, kind: str) -> re.Match[str] | None:
    for match in _WORKPLACE_MATCHERS[kind].finditer(description):
        if not _is_negated(description, match.start()):
            return match
    return None


def _is_negated(description: str, position: int) -> bool:
    prefix = description[max(0, position - 32) : position]
    return _NEGATION_PREFIX.search(prefix) is not None


def _remote_scope(
    locations: tuple[SourceLocationEvidence, ...],
    description: str,
    source_workplace_type: str | None,
    source_is_remote: bool | None,
) -> NormalizedField:
    structured_workplace = (
        _WORKPLACE.get(_key(source_workplace_type)) if source_workplace_type else None
    )
    if _has_conflicting_structured_workplace(structured_workplace, source_is_remote):
        return NormalizedField(None)
    if source_is_remote is False or structured_workplace == "onsite":
        return NormalizedField(None)
    structured_remote = structured_workplace == "remote" or source_is_remote is True
    for item in locations:
        for direct_scope, token in _REMOTE_SCOPE_TOKENS:
            match = _remote_location_match(item.raw, token)
            if match is not None:
                structured_country_scope = (
                    _scope_for_country(item.country_code) if item.country_code is not None else None
                )
                direct_method: NormalizationMethod = (
                    "structured_source"
                    if structured_country_scope == direct_scope
                    else "text_parse"
                )
                confidence = (
                    Decimal("1.000") if direct_method == "structured_source" else Decimal("0.700")
                )
                return _field(direct_scope, direct_method, match.group(0), confidence)
    for item in locations:
        parsed_country = item.country_code or _parse_location(item.raw)[1]
        country_scope = _scope_for_country(parsed_country) if parsed_country is not None else None
        if structured_remote and country_scope is not None:
            country_method: NormalizationMethod = (
                "structured_source" if item.country_code is not None else "text_parse"
            )
            confidence = (
                Decimal("1.000") if country_method == "structured_source" else Decimal("0.700")
            )
            return _field(country_scope, country_method, item.raw, confidence)
        if structured_remote:
            for regional_scope, token in _REMOTE_SCOPE_TOKENS[1:]:
                match = re.fullmatch(rf"\s*(?:the\s+)?{token}\s*", item.raw, re.I)
                if match is not None:
                    return _field(
                        regional_scope,
                        "text_parse",
                        match.group(0).strip(),
                        Decimal("0.700"),
                    )
    for scope, token in _REMOTE_SCOPE_TOKENS:
        match = _remote_scope_description_match(description, token)
        if match is not None:
            return _field(scope, "description_rule", match.group(0), Decimal("0.700"))
    return NormalizedField(None)


def _has_conflicting_structured_workplace(
    workplace: str | None, source_is_remote: bool | None
) -> bool:
    return (workplace == "remote" and source_is_remote is False) or (
        workplace == "onsite" and source_is_remote is True
    )


def _scope_for_country(country_code: str) -> str | None:
    return "Poland" if country_code == "PL" else None


def _remote_location_match(raw: str, token: str) -> re.Match[str] | None:
    return re.search(
        rf"\bremote\s*(?:[-\u2013,/]\s*|\s+){token}\b"
        rf"|\b{token}\s*(?:[-\u2013,/]\s*|\s+|\s*\(\s*)(?:full(?:y)?\s+)?remote\b(?:\s*\))?",
        raw,
        re.I,
    )


def _remote_scope_description_match(description: str, token: str) -> re.Match[str] | None:
    patterns = (
        rf"\b(?:fully\s+)?remote(?:\s+work(?:ing)?)?\s+(?:in|from|within|across)\s+(?:the\s+)?{token}\b",
        rf"\bwork\s+remotely\s+(?:in|from|within|across)\s+(?:the\s+)?{token}\b",
        rf"\b{token}\s*(?:[-\u2013]\s*|\s+)only\s+remote\b",
        rf"\b{token}\s+remote\b",
    )
    for pattern in patterns:
        match = re.search(pattern, description, re.I)
        if match is not None and _scope_match_has_affirmative_remote(description, match):
            return match
    return None


def _scope_match_has_affirmative_remote(description: str, match: re.Match[str]) -> bool:
    remote = re.search(r"\bremote(?:ly)?\b", match.group(0), re.I)
    return remote is not None and not _is_negated(description, match.start() + remote.start())


def _compensation(
    structured: tuple[SourceCompensationEvidence, ...], compensation_text: str | None
) -> tuple[NormalizedCompensation, ...]:
    if structured:
        return tuple(_structured_compensation(item) for item in structured)
    if compensation_text is None:
        return ()
    matches = tuple(
        match for match in _TEXT_RANGE.finditer(compensation_text) if _range_currency(match)
    )
    normalized = (
        _text_compensation(
            match,
            compensation_text,
            matches[index - 1] if index else None,
            matches[index + 1] if index + 1 < len(matches) else None,
        )
        for index, match in enumerate(matches)
    )
    return tuple(item for item in normalized if item is not None)


def _structured_compensation(value: SourceCompensationEvidence) -> NormalizedCompensation:
    component_key = _key(value.component_type)
    component = (
        "base_salary"
        if component_key in {"salary", "base salary", "base_salary"}
        else "bonus"
        if component_key in {"bonus", "cash bonus"}
        else "equity"
        if component_key in {"equity", "equitypercentage", "equity percentage"}
        else "other"
    )
    interval = _interval(value.interval)
    return NormalizedCompensation(
        cast(CompensationComponentType, component),
        value.min_value,
        value.max_value,
        value.currency,
        interval,
        value.location_tier,
        value.raw_text,
        "structured_source",
        Decimal("1.000"),
    )


def _text_compensation(
    match: re.Match[str],
    full_text: str,
    previous_match: re.Match[str] | None,
    next_match: re.Match[str] | None,
) -> NormalizedCompensation | None:
    left = _local_left_boundary(full_text, match.start(), previous_match)
    right = _local_right_boundary(full_text, match.end(), next_match)
    before = full_text[left : match.start()]
    after = full_text[match.end() : right]
    before_component = _last_component_match(before)
    after_component = None if before_component is not None else _COMPONENT_MARKER.search(after)
    component_match = before_component or after_component
    if component_match is None:
        return None
    component = _component_from_marker(component_match.group(0))
    after_interval = _INTERVAL_MARKER.search(after)
    before_interval = None if after_interval is not None else _last_interval_match(before)
    interval_match = after_interval or before_interval
    interval = _interval(interval_match.group(0)) if interval_match else None
    evidence_starts = [match.start()]
    evidence_ends = [match.end()]
    if before_component is not None:
        evidence_starts.append(left + before_component.start())
    if before_interval is not None:
        evidence_starts.append(left + before_interval.start())
    if after_component is not None:
        evidence_ends.append(match.end() + after_component.end())
    if after_interval is not None:
        evidence_ends.append(match.end() + after_interval.end())
    return NormalizedCompensation(
        cast(CompensationComponentType, component),
        _amount(match.group("minimum")),
        _amount(match.group("maximum")),
        _range_currency(match),
        interval,
        None,
        full_text[min(evidence_starts) : max(evidence_ends)].strip(),
        "text_parse",
        Decimal("0.700"),
    )


def _local_left_boundary(text: str, position: int, previous_match: re.Match[str] | None) -> int:
    sentence_boundary = max(
        (item.end() for item in _SENTENCE_BOUNDARY.finditer(text, 0, position)), default=0
    )
    return max(sentence_boundary, previous_match.end() if previous_match is not None else 0)


def _local_right_boundary(text: str, position: int, next_match: re.Match[str] | None) -> int:
    sentence = _SENTENCE_BOUNDARY.search(text, position)
    candidates = [len(text)]
    if sentence is not None:
        candidates.append(sentence.start())
    if next_match is not None:
        candidates.append(next_match.start())
    return min(candidates)


def _last_component_match(value: str) -> re.Match[str] | None:
    matches = tuple(_COMPONENT_MARKER.finditer(value))
    return matches[-1] if matches else None


def _last_interval_match(value: str) -> re.Match[str] | None:
    matches = tuple(_INTERVAL_MARKER.finditer(value))
    return matches[-1] if matches else None


def _component_from_marker(value: str) -> str:
    normalized = _key(value)
    if normalized in {"salary", "base salary", "base pay"}:
        return "base_salary"
    if normalized == "bonus":
        return "bonus"
    if normalized == "equity":
        return "equity"
    return "other"


def _range_currency(match: re.Match[str]) -> str | None:
    observed = {
        _CURRENCY[value.upper()]
        for name in (
            "currency_before_min",
            "currency_after_min",
            "currency_before_max",
            "currency_after_max",
        )
        if (value := match.group(name)) is not None
    }
    return next(iter(observed)) if len(observed) == 1 else None


def _amount(value: str) -> Decimal:
    cleaned = value.replace(" ", "").replace(",", "")
    multiplier = (
        Decimal("1000")
        if cleaned.casefold().endswith("k")
        else Decimal("1000000")
        if cleaned.casefold().endswith("m")
        else Decimal("1")
    )
    return Decimal(cleaned.rstrip("kKmM")) * multiplier


def _interval(value: str | None) -> CompensationInterval | None:
    if value is None:
        return None
    normalized = value.casefold()
    if "year" in normalized or "annual" in normalized:
        return "year"
    if "month" in normalized:
        return "month"
    if "hour" in normalized:
        return "hour"
    if "day" in normalized:
        return "day"
    if "none" in normalized:
        return "none"
    return None


def _field(value: str, method: str, evidence: str, confidence: Decimal) -> NormalizedField:
    return NormalizedField(value, cast(NormalizationMethod, method), evidence, confidence)


def _key(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold().replace("_", " ").strip())
