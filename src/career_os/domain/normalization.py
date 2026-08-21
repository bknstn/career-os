"""Frozen source-neutral contracts for deterministic job normalization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

_COUNTRY_CODE: Final = re.compile(r"^[A-Z]{2}$")
_CURRENCY_CODE: Final = re.compile(r"^[A-Z]{3}$")
_SENIORITIES: Final = frozenset(
    {
        "intern",
        "junior",
        "mid",
        "senior",
        "lead",
        "staff",
        "principal",
        "manager",
        "director",
        "executive",
    }
)
_ROLE_FAMILIES: Final = frozenset(
    {
        "engineering_management",
        "product_management",
        "data_engineering",
        "data_science",
        "platform_engineering",
        "devops_engineering",
        "backend_engineering",
        "frontend_engineering",
        "fullstack_engineering",
        "cloud_engineering",
    }
)
_EMPLOYMENT_TYPES: Final = frozenset(
    {"full_time", "part_time", "contract", "internship", "temporary", "other"}
)
_WORKPLACE_TYPES: Final = frozenset({"onsite", "hybrid", "remote", "unspecified"})
_COMPONENT_TYPES: Final = frozenset({"base_salary", "bonus", "equity", "other"})
_INTERVALS: Final = frozenset({"hour", "day", "month", "year", "none"})
_METHODS: Final = frozenset(
    {"structured_source", "title_rule", "source_level", "description_rule", "text_parse"}
)

Seniority = Literal[
    "intern",
    "junior",
    "mid",
    "senior",
    "lead",
    "staff",
    "principal",
    "manager",
    "director",
    "executive",
]
RoleFamily = Literal[
    "engineering_management",
    "product_management",
    "data_engineering",
    "data_science",
    "platform_engineering",
    "devops_engineering",
    "backend_engineering",
    "frontend_engineering",
    "fullstack_engineering",
    "cloud_engineering",
]
EmploymentType = Literal["full_time", "part_time", "contract", "internship", "temporary", "other"]
WorkplaceType = Literal["onsite", "hybrid", "remote", "unspecified"]
CompensationComponentType = Literal["base_salary", "bonus", "equity", "other"]
CompensationInterval = Literal["hour", "day", "month", "year", "none"]
NormalizationMethod = Literal[
    "structured_source", "title_rule", "source_level", "description_rule", "text_parse"
]


def _nonblank(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-blank")
    return value.strip()


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _nonblank(value, field_name)


def _confidence(value: Decimal, field_name: str) -> Decimal:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or not Decimal("0") <= value <= Decimal("1")
    ):
        raise ValueError(f"{field_name} must be a Decimal from zero to one")
    return value


def _nonnegative_decimal(value: Decimal | None, field_name: str) -> None:
    if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value < 0):
        raise ValueError(f"{field_name} must be a finite non-negative Decimal or None")


@dataclass(frozen=True, slots=True)
class SourceLocationEvidence:
    """One source-preserved location, already structured when the source provides it."""

    raw: str
    city: str | None = None
    region: str | None = None
    country_code: str | None = None
    is_primary: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", _nonblank(self.raw, "raw"))
        for name in ("city", "region"):
            object.__setattr__(self, name, _optional_text(getattr(self, name), name))
        if self.country_code is not None and (
            not isinstance(self.country_code, str) or not _COUNTRY_CODE.fullmatch(self.country_code)
        ):
            raise ValueError("country_code must be ISO 3166-1 alpha-2 uppercase")
        if not isinstance(self.is_primary, bool):
            raise TypeError("is_primary must be a bool")


@dataclass(frozen=True, slots=True)
class SourceCompensationEvidence:
    """A source-structured compensation component before controlled normalization."""

    component_type: str
    min_value: Decimal | None = None
    max_value: Decimal | None = None
    currency: str | None = None
    interval: str | None = None
    location_tier: str | None = None
    raw_text: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "component_type", _nonblank(self.component_type, "component_type"))
        for name in ("min_value", "max_value"):
            _nonnegative_decimal(getattr(self, name), name)
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError("min_value must not exceed max_value")
        if self.currency is not None and (
            not isinstance(self.currency, str) or not _CURRENCY_CODE.fullmatch(self.currency)
        ):
            raise ValueError("currency must be ISO 4217 uppercase or None")
        for name in ("interval", "location_tier", "raw_text"):
            object.__setattr__(self, name, _optional_text(getattr(self, name), name))
        if (
            self.min_value is None
            and self.max_value is None
            and self.currency is None
            and self.raw_text is None
        ):
            raise ValueError("compensation evidence requires an amount, currency, or raw_text")


@dataclass(frozen=True, slots=True)
class JobNormalizationInput:
    """Canonical text plus source facts; no inferred field is accepted as input."""

    title: str
    description: str
    source_level: str | None = None
    source_category: str | None = None
    locations: tuple[SourceLocationEvidence, ...] = ()
    source_employment_type: str | None = None
    source_workplace_type: str | None = None
    source_is_remote: bool | None = None
    structured_compensation: tuple[SourceCompensationEvidence, ...] = ()
    compensation_text: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _nonblank(self.title, "title"))
        if not isinstance(self.description, str):
            raise TypeError("description must be a string")
        for name in (
            "source_level",
            "source_category",
            "source_employment_type",
            "source_workplace_type",
            "compensation_text",
        ):
            object.__setattr__(self, name, _optional_text(getattr(self, name), name))
        if self.source_is_remote is not None and not isinstance(self.source_is_remote, bool):
            raise TypeError("source_is_remote must be a bool or None")
        if not isinstance(self.locations, tuple) or not all(
            isinstance(item, SourceLocationEvidence) for item in self.locations
        ):
            raise TypeError("locations must be a tuple of SourceLocationEvidence")
        if not isinstance(self.structured_compensation, tuple) or not all(
            isinstance(item, SourceCompensationEvidence) for item in self.structured_compensation
        ):
            raise TypeError("structured_compensation must be a tuple of SourceCompensationEvidence")


@dataclass(frozen=True, slots=True)
class NormalizedField:
    """An inferred or source-backed scalar; absent values explicitly mean unknown."""

    value: str | None
    method: NormalizationMethod | None = None
    evidence: str | None = None
    confidence: Decimal | None = None

    def __post_init__(self) -> None:
        if self.value is None:
            if any(value is not None for value in (self.method, self.evidence, self.confidence)):
                raise ValueError(
                    "unknown normalized values cannot carry method, evidence, or confidence"
                )
            return
        object.__setattr__(self, "value", _nonblank(self.value, "value"))
        if self.method is None or self.evidence is None or self.confidence is None:
            raise ValueError("known normalized values require method, evidence, and confidence")
        if self.method not in _METHODS:
            raise ValueError("method must be a supported deterministic normalization method")
        object.__setattr__(self, "evidence", _nonblank(self.evidence, "evidence"))
        object.__setattr__(self, "confidence", _confidence(self.confidence, "confidence"))


@dataclass(frozen=True, slots=True)
class NormalizedLocation:
    """A source-preserved location with conservative parsed fields and provenance."""

    ordinal: int
    raw: str
    city: str | None
    region: str | None
    country_code: str | None
    is_primary: bool
    confidence: Decimal
    method: NormalizationMethod

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ValueError("ordinal must be a non-negative integer")
        object.__setattr__(self, "raw", _nonblank(self.raw, "raw"))
        for name in ("city", "region"):
            object.__setattr__(self, name, _optional_text(getattr(self, name), name))
        if self.country_code is not None and (
            not isinstance(self.country_code, str) or not _COUNTRY_CODE.fullmatch(self.country_code)
        ):
            raise ValueError("country_code must be ISO 3166-1 alpha-2 uppercase or None")
        if not isinstance(self.is_primary, bool):
            raise TypeError("is_primary must be a bool")
        if self.method not in _METHODS:
            raise ValueError("method must be a supported deterministic normalization method")
        object.__setattr__(self, "confidence", _confidence(self.confidence, "confidence"))


@dataclass(frozen=True, slots=True)
class NormalizedCompensation:
    """One non-combined compensation component ready for the existing schema."""

    component_type: CompensationComponentType
    min_value: Decimal | None
    max_value: Decimal | None
    currency: str | None
    interval: CompensationInterval | None
    location_tier: str | None
    raw_text: str | None
    source_method: Literal["structured_source", "text_parse"]
    confidence: Decimal

    def __post_init__(self) -> None:
        if self.component_type not in _COMPONENT_TYPES:
            raise ValueError("component_type must match the job_compensation vocabulary")
        for name in ("min_value", "max_value"):
            _nonnegative_decimal(getattr(self, name), name)
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError("min_value must not exceed max_value")
        if self.currency is not None and (
            not isinstance(self.currency, str) or not _CURRENCY_CODE.fullmatch(self.currency)
        ):
            raise ValueError("currency must be ISO 4217 uppercase or None")
        if self.interval is not None and self.interval not in _INTERVALS:
            raise ValueError("interval must match the job_compensation vocabulary")
        if self.source_method not in {"structured_source", "text_parse"}:
            raise ValueError("source_method must match the job_compensation vocabulary")
        for name in ("location_tier", "raw_text"):
            object.__setattr__(self, name, _optional_text(getattr(self, name), name))
        if (
            self.min_value is None
            and self.max_value is None
            and self.currency is None
            and self.raw_text is None
        ):
            raise ValueError("compensation requires an amount, currency, or raw_text")
        object.__setattr__(self, "confidence", _confidence(self.confidence, "confidence"))


@dataclass(frozen=True, slots=True)
class JobNormalization:
    """Complete deterministic outputs; scalar unknowns use ``NormalizedField(None)``."""

    normalizer_version: str
    title_normalized: str
    role_family: NormalizedField
    seniority: NormalizedField
    locations: tuple[NormalizedLocation, ...]
    employment_type: NormalizedField
    workplace_type: NormalizedField
    remote_scope: NormalizedField
    compensation: tuple[NormalizedCompensation, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "normalizer_version", _nonblank(self.normalizer_version, "normalizer_version")
        )
        object.__setattr__(
            self, "title_normalized", _nonblank(self.title_normalized, "title_normalized")
        )
        if not all(
            isinstance(item, NormalizedField)
            for item in (
                self.role_family,
                self.seniority,
                self.employment_type,
                self.workplace_type,
                self.remote_scope,
            )
        ):
            raise TypeError("scalar fields must be NormalizedField values")
        if self.seniority.value is not None and self.seniority.value not in _SENIORITIES:
            raise ValueError("seniority must match the job_postings vocabulary")
        if self.role_family.value is not None and self.role_family.value not in _ROLE_FAMILIES:
            raise ValueError("role_family must match the controlled normalizer vocabulary")
        if (
            self.employment_type.value is not None
            and self.employment_type.value not in _EMPLOYMENT_TYPES
        ):
            raise ValueError("employment_type must match the job_postings vocabulary")
        if (
            self.workplace_type.value is not None
            and self.workplace_type.value not in _WORKPLACE_TYPES
        ):
            raise ValueError("workplace_type must match the job_postings vocabulary")
        if not isinstance(self.locations, tuple) or not all(
            isinstance(item, NormalizedLocation) for item in self.locations
        ):
            raise TypeError("locations must contain NormalizedLocation values")
        if tuple(item.ordinal for item in self.locations) != tuple(range(len(self.locations))):
            raise ValueError("locations must have stable contiguous ordinals")
        if not isinstance(self.compensation, tuple) or not all(
            isinstance(item, NormalizedCompensation) for item in self.compensation
        ):
            raise TypeError("compensation must contain NormalizedCompensation values")
