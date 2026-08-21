"""Source-neutral canonical job candidates and stable content hashing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from types import MappingProxyType
from typing import Final
from urllib.parse import SplitResult, urlsplit, urlunsplit

from career_os.domain.jobs import _require_nonempty_text, _require_utc_aware
from career_os.normalization import clean_text, sanitize_html

type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | tuple[JsonValue, ...] | Mapping[str, JsonValue]

CANONICAL_CONTENT_FIELDS: Final = (
    "company_name",
    "title_raw",
    "title_normalized",
    "description_html",
    "description_text",
    "department_raw",
    "team_raw",
    "source_url",
    "apply_url",
    "published_at",
    "normalizer_version",
    "taxonomy_version",
)
"""Complete canonical snapshot and changed-field contract, including enrichment state."""

CANONICAL_HASH_FIELDS: Final = (
    "company_name",
    "title_raw",
    "title_normalized",
    "description_html",
    "description_text",
    "department_raw",
    "team_raw",
    "source_url",
    "apply_url",
    "published_at",
    "normalizer_version",
)
"""Source-content fields that determine canonical job version identity.

``taxonomy_version`` intentionally remains in the complete canonical snapshot
and changed-field contract, but is post-ingestion enrichment state.  Its value
must not create a source-content version or invalidate an existing hash.
"""


def _clean_required_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    cleaned = clean_text(value)
    return _require_nonempty_text(cleaned, field_name=field_name)


def _clean_optional_text(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _clean_required_text(value, field_name=field_name)


def _normalize_url(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string or None")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank when provided")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in cleaned
    ):
        raise ValueError(f"{field_name} must not contain whitespace or control characters")
    try:
        parsed = urlsplit(cleaned)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{field_name} has an invalid authority") from error
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not hostname:
        raise ValueError(f"{field_name} must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_name} must not contain userinfo")
    authority = f"[{hostname.lower()}]" if ":" in hostname else hostname.lower()
    if port is not None:
        authority = f"{authority}:{port}"
    normalized = SplitResult(
        parsed.scheme.lower(),
        authority,
        parsed.path,
        parsed.query,
        parsed.fragment,
    )
    return urlunsplit(normalized)


def _json_value(value: object, *, field_name: str = "value") -> JsonValue:
    """Copy an ordinary JSON value, rejecting runtime-specific representations."""
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{field_name} must not contain a non-finite float")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, JsonValue] = {}
        for key, nested_value in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{field_name} mapping keys must be strings")
            copied[key] = _json_value(nested_value, field_name=f"{field_name}.{key}")
        return MappingProxyType(copied)
    if isinstance(value, (list, tuple)):
        return tuple(
            _json_value(nested_value, field_name=f"{field_name}[{index}]")
            for index, nested_value in enumerate(value)
        )
    raise TypeError(f"{field_name} must contain only JSON-compatible values")


def _json_ready(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _json_ready(nested_value) for key, nested_value in value.items()}
    if isinstance(value, tuple):
        return [_json_ready(nested_value) for nested_value in value]
    return value


def stable_json_dumps(value: object) -> str:
    """Serialize JSON-compatible data without dependence on input mapping order.

    Objects such as ``datetime``, ``Decimal``, sets, arbitrary classes, and
    non-finite floats are rejected.  Callers must convert them deliberately so
    a content hash never relies on a runtime-specific string representation.
    """
    canonical_value = _json_value(value)
    return json.dumps(
        _json_ready(canonical_value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def content_hash(value: object) -> str:
    """Return the SHA-256 digest of a stable JSON representation."""
    return hashlib.sha256(stable_json_dumps(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CanonicalJobCandidate:
    """Validated normalized content for one source posting observation.

    ``raw_payload`` and ``observed_at`` retain evidence and lifecycle context,
    but are intentionally excluded from canonical snapshot and source-content
    hash inputs.  Source identity is likewise excluded because repository upsert
    uses it as the immutable key rather than mutable job content.

    ``taxonomy_version`` is retained in the full snapshot for reproducibility
    and changed-field reporting, but it is post-ingestion enrichment state and
    therefore excluded from the source-content hash input.
    """

    source_key: str
    source_kind: str
    external_id: str
    company_name: str
    title_raw: str
    title_normalized: str
    description_text: str
    observed_at: datetime
    raw_payload: Mapping[str, JsonValue]
    normalizer_version: str
    taxonomy_version: str
    description_html: str | None = None
    department_raw: str | None = None
    team_raw: str | None = None
    source_url: str | None = None
    apply_url: str | None = None
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in ("source_key", "source_kind", "external_id"):
            object.__setattr__(
                self,
                field_name,
                _require_nonempty_text(getattr(self, field_name), field_name=field_name),
            )
        for field_name in (
            "company_name",
            "title_raw",
            "title_normalized",
            "description_text",
            "normalizer_version",
            "taxonomy_version",
        ):
            object.__setattr__(
                self,
                field_name,
                _clean_required_text(getattr(self, field_name), field_name=field_name),
            )
        for field_name in ("department_raw", "team_raw"):
            object.__setattr__(
                self,
                field_name,
                _clean_optional_text(getattr(self, field_name), field_name=field_name),
            )
        if self.description_html is not None:
            if not isinstance(self.description_html, str):
                raise TypeError("description_html must be a string or None")
            object.__setattr__(self, "description_html", sanitize_html(self.description_html))
        object.__setattr__(
            self, "source_url", _normalize_url(self.source_url, field_name="source_url")
        )
        object.__setattr__(
            self, "apply_url", _normalize_url(self.apply_url, field_name="apply_url")
        )
        object.__setattr__(
            self,
            "observed_at",
            _require_utc_aware(self.observed_at, field_name="observed_at"),
        )
        if self.published_at is not None:
            object.__setattr__(
                self,
                "published_at",
                _require_utc_aware(self.published_at, field_name="published_at"),
            )
        raw_payload = _json_value(self.raw_payload, field_name="raw_payload")
        if not isinstance(raw_payload, Mapping):
            raise TypeError("raw_payload must be a mapping")
        object.__setattr__(self, "raw_payload", raw_payload)

    def canonical_content(self) -> dict[str, JsonValue]:
        """Return the complete mutable canonical snapshot used for version diffs."""
        return {
            "company_name": self.company_name,
            "title_raw": self.title_raw,
            "title_normalized": self.title_normalized,
            "description_html": self.description_html,
            "description_text": self.description_text,
            "department_raw": self.department_raw,
            "team_raw": self.team_raw,
            "source_url": self.source_url,
            "apply_url": self.apply_url,
            "published_at": self.published_at.isoformat().replace("+00:00", "Z")
            if self.published_at is not None
            else None,
            "normalizer_version": self.normalizer_version,
            "taxonomy_version": self.taxonomy_version,
        }

    def canonical_content_json(self) -> str:
        """Return stable JSON for the complete mutable canonical snapshot."""
        return stable_json_dumps(self.canonical_content())

    def canonical_hash_content(self) -> dict[str, JsonValue]:
        """Return the explicit source-content input for :attr:`content_hash`."""
        complete_content = self.canonical_content()
        return {field_name: complete_content[field_name] for field_name in CANONICAL_HASH_FIELDS}

    @property
    def content_hash(self) -> str:
        """Hash source content, never observation/lifecycle or enrichment metadata."""
        return content_hash(self.canonical_hash_content())
