"""Provenance and citation-location checks for research evidence contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from career_os.domain.research import (
    ResearchClaimDraft,
    ResearchDataPeriod,
    ResearchDateState,
    ResearchEvidenceKind,
    ResearchLineageKind,
    ResearchMediaType,
    ResearchMissingReason,
    ResearchOriginalVersion,
    ResearchQuestion,
    ResearchTextLocation,
    ResearchTextRegion,
    ResearchURLValidationError,
    normalize_research_url,
    quote_matches_location,
)


def test_source_urls_are_exact_https_hosts_and_fragment_free() -> None:
    assert (
        normalize_research_url(
            "https://WWW.ANTHROPIC.COM/research/paper#results",
            allowed_hosts=("www.anthropic.com",),
        )
        == "https://www.anthropic.com/research/paper"
    )
    for value in (
        "http://www.anthropic.com/paper",
        "https://user:pass@www.anthropic.com/paper",
        "https://www.anthropic.com:8443/paper",
        "https://anthropic.com/paper",
        "https://127.0.0.1/paper",
        "https://www.anthropic.com/paper with spaces",
    ):
        with pytest.raises(ResearchURLValidationError):
            normalize_research_url(value, allowed_hosts=("www.anthropic.com",))


def test_quote_location_requires_exact_bounds_not_slicing_truncation() -> None:
    text = "The measured task changed."
    quote = "The measured"
    valid = ResearchTextLocation(ResearchMediaType.HTML, 0, len(quote), section="Results")
    forged = ResearchTextLocation(ResearchMediaType.HTML, 0, 999, section="Results")

    assert quote_matches_location(text, quote, valid)
    assert not quote_matches_location(text, quote, forged)


def test_original_version_rejects_unsupported_media_type() -> None:
    with pytest.raises(TypeError, match="ResearchMediaType"):
        ResearchOriginalVersion(
            id=uuid4(),
            source_key="metr",
            canonical_url="https://metr.org/research/report",
            discovered_url="https://metr.org/research/report",
            retrieved_url="https://metr.org/research/report",
            title="Report",
            retrieved_at=datetime.now(UTC),
            media_type="text/plain",
            original_bytes=b"unsupported",
        )


def test_pdf_quote_must_be_inside_the_recorded_page_range() -> None:
    text = "Observed value\nForecast value"
    split = text.index("\n")
    version = ResearchOriginalVersion(
        id=uuid4(),
        source_key="metr",
        canonical_url="https://metr.org/research/report",
        discovered_url="https://metr.org/research/report",
        retrieved_url="https://metr.org/research/report",
        title="Report",
        retrieved_at=datetime.now(UTC),
        media_type=ResearchMediaType.PDF,
        original_bytes=b"%PDF-original-body",
        normalized_text=text,
        extraction_regions=(
            ResearchTextRegion(ResearchMediaType.PDF, 0, split, page_number=1),
            ResearchTextRegion(ResearchMediaType.PDF, split + 1, len(text), page_number=2),
        ),
        extraction_version="pdf-text-v1",
    )
    quote_start = text.index("Observed")
    location = ResearchTextLocation(
        ResearchMediaType.PDF,
        quote_start,
        quote_start + len("Observed value"),
        page_number=2,
    )

    assert quote_matches_location(text, "Observed value", location)
    with pytest.raises(ValueError, match="section or PDF page"):
        _claim_for(version.id, "Observed value", location).validate_against_version(version)


def test_version_checks_citation_version_identity_and_extraction_regions() -> None:
    text = "Finding A\nFinding B"
    version_id = uuid4()
    version = ResearchOriginalVersion(
        id=version_id,
        source_key="anthropic_economics",
        canonical_url="https://www.anthropic.com/research/report",
        discovered_url="https://www.anthropic.com/research/report",
        retrieved_url="https://www.anthropic.com/research/report",
        title="Report",
        retrieved_at=datetime.now(UTC),
        media_type=ResearchMediaType.HTML,
        original_bytes=b"<html>Finding A</html>",
        normalized_text=text,
        extraction_regions=(
            ResearchTextRegion(ResearchMediaType.HTML, 0, len("Finding A"), section="Results"),
            ResearchTextRegion(
                ResearchMediaType.HTML,
                len("Finding A\n"),
                len(text),
                section="Discussion",
            ),
        ),
        extraction_version="html-text-v1",
    )
    claim = _claim_for(
        version_id,
        "Finding A",
        ResearchTextLocation(ResearchMediaType.HTML, 0, len("Finding A"), section="Results"),
    )

    claim.validate_against_version(version)
    with pytest.raises(ValueError, match="supplied immutable document version"):
        _claim_for(uuid4(), "Finding A", claim.location).validate_against_version(version)


def _claim_for(version_id: UUID, quote: str, location: ResearchTextLocation) -> ResearchClaimDraft:
    """Keep this wrong-page regression focused on citation validation."""
    return ResearchClaimDraft(
        document_version_id=version_id,
        question=ResearchQuestion.DEVELOPER_TASKS,
        statement="A claim remains a candidate until reviewed.",
        exact_quote=quote,
        location=location,
        evidence_kind=ResearchEvidenceKind.OBSERVED,
        metric_definition=None,
        metric_value=None,
        denominator=None,
        geography=None,
        sample_description=None,
        sample_size=None,
        data_period=ResearchDataPeriod(None, None, ResearchDateState.NOT_REPORTED),
        limitations=None,
        missing_reasons={
            "metric_definition": ResearchMissingReason.NOT_REPORTED,
            "metric_value": ResearchMissingReason.NOT_REPORTED,
            "denominator": ResearchMissingReason.NOT_REPORTED,
            "geography": ResearchMissingReason.NOT_REPORTED,
            "sample_description": ResearchMissingReason.NOT_REPORTED,
            "sample_size": ResearchMissingReason.NOT_REPORTED,
            "limitations": ResearchMissingReason.UNCLEAR,
        },
        lineage_kind=ResearchLineageKind.UNKNOWN,
    )
