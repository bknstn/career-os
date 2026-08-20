"""Workable public canonical candidate mapping without persistence I/O."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from career_os.application import (
    WORKABLE_PUBLIC_NORMALIZER_VERSION,
    IngestionError,
    workable_public_candidate,
)
from career_os.config.sources import WorkablePublicSourceConfig
from career_os.domain import RawJobPosting


def test_candidate_maps_documented_identity_title_department_urls_version_and_raw() -> None:
    payload = {
        "title": " Senior Engineer ",
        "description": "<p>Build <strong>reliable</strong> systems.</p>",
        "department": " Engineering ",
        "unknownFutureField": {"retained": True},
    }
    candidate = workable_public_candidate(_config(), _posting(payload=payload))

    assert candidate.source_key == "example-workable"
    assert candidate.source_kind == "workable_public"
    assert candidate.external_id == "engineering-1"
    assert candidate.title_raw == "Senior Engineer"
    assert candidate.department_raw == "Engineering"
    assert candidate.source_url == "https://jobs.example/engineering-1"
    assert candidate.apply_url == "https://apply.example/engineering-1"
    assert candidate.normalizer_version == WORKABLE_PUBLIC_NORMALIZER_VERSION
    assert candidate.raw_payload == payload
    assert candidate.raw_payload["unknownFutureField"] == {"retained": True}


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("<p>Build <strong>systems</strong>.</p>", "Build systems."),
        ("Plain source description", "Plain source description"),
    ],
)
def test_candidate_derives_text_without_claiming_source_html(
    description: str, expected: str
) -> None:
    candidate = workable_public_candidate(
        _config(), _posting(payload={"title": "Engineer", "description": description})
    )

    assert candidate.description_html is None
    assert candidate.description_text == expected


def test_candidate_keeps_dates_and_later_fields_raw_without_published_claim() -> None:
    payload = {
        "title": "Engineer",
        "description": "Description",
        "published_on": "2026-08-20",
        "created_at": "2026-08-19T10:00:00Z",
        "city": "Warsaw",
        "workplace_type": "remote",
        "employment_type": "full_time",
    }
    candidate = workable_public_candidate(_config(), _posting(payload=payload))

    assert candidate.published_at is None
    assert candidate.raw_payload["published_on"] == "2026-08-20"
    assert candidate.raw_payload["created_at"] == "2026-08-19T10:00:00Z"
    assert candidate.raw_payload["city"] == "Warsaw"
    assert candidate.raw_payload["workplace_type"] == "remote"
    assert candidate.raw_payload["employment_type"] == "full_time"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"description": "Description"}, "no non-blank title"),
        ({"title": " ", "description": "Description"}, "no non-blank title"),
        ({"title": [], "description": "Description"}, "title"),
        ({"title": "Engineer"}, "no non-blank description"),
        ({"title": "Engineer", "description": " "}, "no non-blank description"),
        ({"title": "Engineer", "description": []}, "description"),
        (
            {"title": "Engineer", "description": "<script>alert(1)</script>"},
            "no non-blank description",
        ),
    ],
)
def test_candidate_fails_closed_for_missing_or_malformed_canonical_evidence(
    payload: dict[str, object], message: str
) -> None:
    with pytest.raises(IngestionError, match=message):
        workable_public_candidate(_config(), _posting(payload=payload))


def test_candidate_rejects_wrong_source_identity() -> None:
    wrong = RawJobPosting(
        source_key="other",
        source_kind="workable_public",
        external_id="engineering-1",
        source_url="https://jobs.example/engineering-1",
        apply_url=None,
        payload={"title": "Engineer", "description": "Description"},
        fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    with pytest.raises(IngestionError, match="identity does not match"):
        workable_public_candidate(_config(), wrong)


def _config() -> WorkablePublicSourceConfig:
    return WorkablePublicSourceConfig(
        key="example-workable",
        kind="workable_public",
        company_name="Example Workable",
        account_subdomain="example",
    )


def _posting(*, payload: dict[str, object]) -> RawJobPosting:
    return RawJobPosting(
        source_key="example-workable",
        source_kind="workable_public",
        external_id="engineering-1",
        source_url="https://jobs.example/engineering-1",
        apply_url="https://apply.example/engineering-1",
        payload=payload,
        fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
