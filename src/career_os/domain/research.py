"""Source-neutral research evidence contracts with explicit provenance."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from hashlib import sha256
from ipaddress import ip_address
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID


class ResearchQuestion(StrEnum):
    """The three questions tracked by the first research stream."""

    DEVELOPER_TASKS = "developer_tasks"
    EXPERIENCE_SENIORITY = "experience_seniority"
    PRODUCTION_AI_SKILLS = "production_ai_skills"


class ResearchEvidenceKind(StrEnum):
    """Method or evidence type used for a research claim."""

    OBSERVED = "observed"
    SURVEY = "survey"
    EXPERIMENT = "experiment"
    FORECAST = "forecast"
    MODEL_ESTIMATE = "model_estimate"
    CASE_STUDY = "case_study"
    SECONDARY_SYNTHESIS = "secondary_synthesis"


class ResearchLineageKind(StrEnum):
    """Relationship between a claim and its underlying dataset."""

    PRIMARY = "primary"
    REPLICATION = "replication"
    DERIVATIVE = "derivative"
    UNKNOWN = "unknown"


class ResearchClaimReviewState(StrEnum):
    """Append-only review outcomes for a proposed or accepted claim."""

    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REVISION_REVIEW_REQUIRED = "revision_review_required"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


class ResearchVersionRelation(StrEnum):
    """Relationship of a fetched representation to its prior body version."""

    INITIAL = "initial"
    EQUIVALENT_TEXT = "equivalent_text"
    REVISION = "revision"


class ResearchDateState(StrEnum):
    """Whether a date value was explicitly present in a source."""

    REPORTED = "reported"
    NOT_REPORTED = "not_reported"
    NOT_APPLICABLE = "not_applicable"
    UNCLEAR = "unclear"


class ResearchMissingReason(StrEnum):
    """Why a claim field has no value."""

    NOT_REPORTED = "not_reported"
    NOT_APPLICABLE = "not_applicable"
    UNCLEAR = "unclear"


class ResearchMediaType(StrEnum):
    """Supported original document media types."""

    HTML = "text/html"
    PDF = "application/pdf"


class ResearchURLValidationError(ValueError):
    """An index or candidate URL violates the static source allowlist."""


def normalize_research_url(value: str, *, allowed_hosts: tuple[str, ...]) -> str:
    """Validate an HTTPS URL against an exact host allowlist and remove fragments.

    DNS resolution and redirect destination checks belong to the acquisition
    boundary. This pure check rejects credentials, non-HTTPS schemes, alternate
    ports, IP literals, and any host not listed exactly in the registry.
    """
    if not isinstance(value, str) or not value.strip():
        raise ResearchURLValidationError("URL must be non-blank text")
    raw = value.strip()
    if any(ord(character) <= 32 or ord(character) == 127 for character in raw):
        raise ResearchURLValidationError("URL must not contain whitespace or control characters")
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ResearchURLValidationError("URL authority is invalid") from error
    if parsed.scheme.lower() != "https" or not parsed.netloc or hostname is None:
        raise ResearchURLValidationError("URL must be an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ResearchURLValidationError("URL must not contain credentials")
    if port not in (None, 443):
        raise ResearchURLValidationError("URL must use the default HTTPS port")
    try:
        ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ResearchURLValidationError("IP-literal URLs are not allowed")
    normalized_host = hostname.rstrip(".").lower()
    allowed = {host.rstrip(".").lower() for host in allowed_hosts}
    if normalized_host not in allowed:
        raise ResearchURLValidationError("URL host is outside the exact source allowlist")
    return urlunsplit(("https", normalized_host, parsed.path or "/", parsed.query, ""))


@dataclass(frozen=True, slots=True)
class ResearchDateDisclosure:
    """A publication date with an explicit source-disclosure state."""

    value: date | None
    state: ResearchDateState

    def __post_init__(self) -> None:
        if not isinstance(self.state, ResearchDateState):
            raise TypeError("state must be a ResearchDateState")
        if self.state is ResearchDateState.REPORTED and self.value is None:
            raise ValueError("a reported date requires a date value")
        if self.state is not ResearchDateState.REPORTED and self.value is not None:
            raise ValueError("an undisclosed date must not carry a date value")


@dataclass(frozen=True, slots=True)
class ResearchDataPeriod:
    """The period represented by the source data, separate from publication."""

    start: date | None
    end: date | None
    state: ResearchDateState

    def __post_init__(self) -> None:
        if not isinstance(self.state, ResearchDateState):
            raise TypeError("state must be a ResearchDateState")
        if self.state is ResearchDateState.REPORTED:
            if self.start is None and self.end is None:
                raise ValueError("a reported data period requires at least one boundary")
            if self.start is not None and self.end is not None and self.start > self.end:
                raise ValueError("data-period start must not follow its end")
        elif self.start is not None or self.end is not None:
            raise ValueError("an undisclosed data period must not carry date boundaries")


@dataclass(frozen=True, slots=True)
class ResearchTextLocation:
    """Exact normalized-text location for a quotation in HTML or a PDF page."""

    media_type: ResearchMediaType
    start_offset: int
    end_offset: int
    section: str | None = None
    page_number: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.media_type, ResearchMediaType):
            raise TypeError("media_type must be a ResearchMediaType")
        if self.start_offset < 0 or self.end_offset <= self.start_offset:
            raise ValueError("citation offsets must be a non-empty non-negative range")
        if self.media_type is ResearchMediaType.HTML:
            if self.page_number is not None:
                raise ValueError("HTML citations must not specify a PDF page")
            if self.section is None or not self.section.strip():
                raise ValueError("HTML citations require a section heading")
        elif self.page_number is None or self.page_number < 1:
            raise ValueError("PDF citations require a one-based page number")
        if self.section is not None:
            _require_nonblank(self.section, "section")


@dataclass(frozen=True, slots=True)
class ResearchTextRegion:
    """Extracted section or PDF page range in the version's normalized text."""

    media_type: ResearchMediaType
    start_offset: int
    end_offset: int
    section: str | None = None
    page_number: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.media_type, ResearchMediaType):
            raise TypeError("media_type must be a ResearchMediaType")
        if self.start_offset < 0 or self.end_offset <= self.start_offset:
            raise ValueError("extraction regions must be non-empty non-negative ranges")
        if self.media_type is ResearchMediaType.HTML:
            if self.page_number is not None:
                raise ValueError("HTML extraction regions must not specify a PDF page")
            _require_nonblank(self.section, "section")
        else:
            if self.page_number is None or self.page_number < 1:
                raise ValueError("PDF extraction regions require a one-based page number")
            if self.section is not None:
                _require_nonblank(self.section, "section")

    def contains(self, location: ResearchTextLocation) -> bool:
        """Whether the citation points inside this exact section or PDF page."""
        return (
            location.media_type is self.media_type
            and location.start_offset >= self.start_offset
            and location.end_offset <= self.end_offset
            and location.section == self.section
            and location.page_number == self.page_number
        )


def quote_matches_location(text: str, quote: str, location: ResearchTextLocation) -> bool:
    """Check exact quote bytes against the declared normalized-text offsets."""
    if not isinstance(text, str) or not isinstance(quote, str) or not quote:
        return False
    if location.end_offset > len(text) or location.end_offset - location.start_offset != len(quote):
        return False
    return text[location.start_offset : location.end_offset] == quote


@dataclass(frozen=True, slots=True)
class ResearchOriginalVersion:
    """Immutable fetched source body and the extraction tied to that body."""

    id: UUID
    source_key: str
    canonical_url: str
    discovered_url: str
    retrieved_url: str
    title: str
    retrieved_at: datetime
    media_type: ResearchMediaType
    original_bytes: bytes = field(repr=False)
    response_etag: str | None = None
    response_last_modified: str | None = None
    publication_date: ResearchDateDisclosure = field(
        default_factory=lambda: ResearchDateDisclosure(None, ResearchDateState.NOT_REPORTED)
    )
    data_period: ResearchDataPeriod = field(
        default_factory=lambda: ResearchDataPeriod(None, None, ResearchDateState.NOT_REPORTED)
    )
    normalized_text: str | None = field(default=None, repr=False)
    extraction_regions: tuple[ResearchTextRegion, ...] = ()
    extraction_version: str = "none-v1"
    extraction_error: str | None = None
    relation: ResearchVersionRelation = ResearchVersionRelation.INITIAL
    previous_version_id: UUID | None = None

    def __post_init__(self) -> None:
        for field_name in ("source_key", "title", "extraction_version"):
            object.__setattr__(
                self, field_name, _require_nonblank(getattr(self, field_name), field_name)
            )
        if not isinstance(self.media_type, ResearchMediaType):
            raise TypeError("media_type must be a ResearchMediaType")
        if not isinstance(self.publication_date, ResearchDateDisclosure):
            raise TypeError("publication_date must be a ResearchDateDisclosure")
        if not isinstance(self.data_period, ResearchDataPeriod):
            raise TypeError("data_period must be a ResearchDataPeriod")
        if not isinstance(self.relation, ResearchVersionRelation):
            raise TypeError("relation must be a ResearchVersionRelation")
        if not isinstance(self.original_bytes, bytes) or not self.original_bytes:
            raise ValueError("original_bytes must contain the exact non-empty response body")
        retrieved_at = _require_utc(self.retrieved_at, "retrieved_at")
        object.__setattr__(self, "retrieved_at", retrieved_at)
        if (
            self.relation is ResearchVersionRelation.INITIAL
            and self.previous_version_id is not None
        ):
            raise ValueError("an initial version must not name a previous version")
        if (
            self.relation is not ResearchVersionRelation.INITIAL
            and self.previous_version_id is None
        ):
            raise ValueError("a later version must name its previous version")
        # Registry membership is checked by the acquisition boundary. This validates
        # URL structure while preserving cross-host official redirects for later policy.
        _validate_https_url(self.canonical_url)
        _validate_https_url(self.discovered_url)
        _validate_https_url(self.retrieved_url)
        if len(self.original_bytes) > 50 * 1024 * 1024:
            raise ValueError("original_bytes exceeds the 50 MiB hard storage limit")
        if self.normalized_text is not None and not isinstance(self.normalized_text, str):
            raise TypeError("normalized_text must be text or None")
        if self.normalized_text is None:
            if self.extraction_regions:
                raise ValueError("extraction regions require normalized text")
        else:
            if not self.normalized_text:
                raise ValueError("normalized_text must be non-empty when extraction succeeds")
            if not self.extraction_regions:
                raise ValueError("successful extraction requires section/page locator ranges")
            if any(
                region.end_offset > len(self.normalized_text) for region in self.extraction_regions
            ):
                raise ValueError("an extraction region extends beyond normalized text")
            if any(region.media_type is not self.media_type for region in self.extraction_regions):
                raise ValueError("extraction region media type must match the document version")
            if self.extraction_error is not None:
                raise ValueError("a successful extraction must not carry an extraction error")
        for field_name in ("response_etag", "response_last_modified", "extraction_error"):
            field_value = getattr(self, field_name)
            if field_value is not None:
                object.__setattr__(self, field_name, _require_nonblank(field_value, field_name))

    @property
    def raw_sha256(self) -> str:
        """SHA-256 of the retained original response body."""
        return sha256(self.original_bytes).hexdigest()

    @property
    def normalized_text_sha256(self) -> str | None:
        """SHA-256 of the exact normalized text, when extraction succeeded."""
        if self.normalized_text is None:
            return None
        return sha256(self.normalized_text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ResearchClaimDraft:
    """Structured research annotation that remains unaccepted until reviewed."""

    document_version_id: UUID
    question: ResearchQuestion
    statement: str
    exact_quote: str
    location: ResearchTextLocation
    evidence_kind: ResearchEvidenceKind
    metric_definition: str | None
    metric_value: str | None
    denominator: str | None
    geography: str | None
    sample_description: str | None
    sample_size: int | None
    data_period: ResearchDataPeriod
    limitations: str | None
    missing_reasons: Mapping[str, ResearchMissingReason]
    lineage_kind: ResearchLineageKind
    upstream_dataset_or_document: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.question, ResearchQuestion):
            raise TypeError("question must be a ResearchQuestion")
        if not isinstance(self.evidence_kind, ResearchEvidenceKind):
            raise TypeError("evidence_kind must be a ResearchEvidenceKind")
        if not isinstance(self.location, ResearchTextLocation):
            raise TypeError("location must be a ResearchTextLocation")
        if not isinstance(self.data_period, ResearchDataPeriod):
            raise TypeError("data_period must be a ResearchDataPeriod")
        if not isinstance(self.lineage_kind, ResearchLineageKind):
            raise TypeError("lineage_kind must be a ResearchLineageKind")
        for field_name in ("statement", "exact_quote"):
            _require_nonblank(getattr(self, field_name), field_name)
        if self.sample_size is not None and self.sample_size < 1:
            raise ValueError("sample_size must be positive when reported")
        if self.lineage_kind in {ResearchLineageKind.REPLICATION, ResearchLineageKind.DERIVATIVE}:
            _require_nonblank(self.upstream_dataset_or_document, "upstream_dataset_or_document")
        reasons = dict(self.missing_reasons)
        for field_name, value in self._reported_fields().items():
            if value is None and field_name not in reasons:
                raise ValueError(f"missing {field_name} requires an explicit reason")
            if value is not None and field_name in reasons:
                raise ValueError(f"reported {field_name} must not have a missing reason")
        if any(not isinstance(reason, ResearchMissingReason) for reason in reasons.values()):
            raise TypeError("missing_reasons values must be ResearchMissingReason instances")
        if set(reasons) - set(self._reported_fields()):
            raise ValueError("missing_reasons contains an unsupported field name")
        object.__setattr__(self, "missing_reasons", MappingProxyType(reasons))

    def validate_quote(self, normalized_text: str) -> None:
        """Raise when the exact quotation does not occupy its declared location."""
        if not quote_matches_location(normalized_text, self.exact_quote, self.location):
            raise ValueError("exact_quote does not match the declared normalized-text location")

    def validate_against_version(self, version: ResearchOriginalVersion) -> None:
        """Require this quote and locator to match the exact immutable version."""
        if self.document_version_id != version.id:
            raise ValueError("claim must reference the supplied immutable document version")
        if version.normalized_text is None:
            raise ValueError("claim cannot be accepted without extracted normalized text")
        self.validate_quote(version.normalized_text)
        if not any(region.contains(self.location) for region in version.extraction_regions):
            raise ValueError("claim locator does not match an extracted section or PDF page range")

    def _reported_fields(self) -> dict[str, object | None]:
        return {
            "metric_definition": self.metric_definition,
            "metric_value": self.metric_value,
            "denominator": self.denominator,
            "geography": self.geography,
            "sample_description": self.sample_description,
            "sample_size": self.sample_size,
            "limitations": self.limitations,
        }


def _require_nonblank(value: str | None, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-blank text")
    return value.strip()


def _require_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _validate_https_url(value: str) -> None:
    """Apply URL syntax constraints when registry context is not available."""
    if not isinstance(value, str) or not value.strip():
        raise ResearchURLValidationError("URL must be non-blank text")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ResearchURLValidationError("URL authority is invalid") from error
    if parsed.scheme.lower() != "https" or not parsed.netloc or hostname is None:
        raise ResearchURLValidationError("URL must be an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None or port not in (None, 443):
        raise ResearchURLValidationError("URL credentials and alternate ports are not allowed")
    if any(ord(character) <= 32 or ord(character) == 127 for character in value):
        raise ResearchURLValidationError("URL must not contain whitespace or control characters")
    try:
        ip_address(hostname)
    except ValueError:
        return
    raise ResearchURLValidationError("IP-literal URLs are not allowed")
