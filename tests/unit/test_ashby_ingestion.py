"""Ashby canonical candidate mapping tests without persistence infrastructure."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from career_os.application import ASHBY_NORMALIZER_VERSION, IngestionError, ashby_candidate
from career_os.config.sources import AshbySourceConfig
from career_os.domain import RawJobPosting


def test_ashby_candidate_maps_documented_evidence_and_leaves_later_fields_raw() -> None:
    payload = {
        "title": " Data Engineer ",
        "descriptionHtml": "<p>Build <strong>data</strong> systems.</p>",
        "descriptionPlain": "Different plain source evidence",
        "department": "Engineering",
        "team": "Data",
        "publishedAt": "2026-08-20T10:30:00+02:00",
        "location": "Warsaw",
        "workplaceType": "Hybrid",
        "employmentType": "FullTime",
        "compensation": {"currency": "PLN"},
    }
    candidate = ashby_candidate(_config(), _posting(payload))

    assert candidate.title_raw == "Data Engineer"
    assert candidate.description_html == "<p>Build <strong>data</strong> systems.</p>"
    assert candidate.description_text == "Different plain source evidence"
    assert candidate.department_raw == "Engineering"
    assert candidate.team_raw == "Data"
    assert candidate.published_at == datetime(2026, 8, 20, 8, 30, tzinfo=UTC)
    assert candidate.normalizer_version == ASHBY_NORMALIZER_VERSION
    assert candidate.raw_payload["compensation"] == {"currency": "PLN"}
    assert candidate.raw_payload["location"] == "Warsaw"


def test_ashby_candidate_uses_plain_only_without_fabricating_html() -> None:
    candidate = ashby_candidate(
        _config(), _posting({"title": "Engineer", "descriptionPlain": "Plain description"})
    )
    assert candidate.description_html is None
    assert candidate.description_text == "Plain description"
    assert candidate.published_at is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"descriptionPlain": "Description"}, "no non-blank title"),
        ({"title": "Engineer"}, "no non-blank description"),
        ({"title": "Engineer", "descriptionPlain": "Text", "publishedAt": "not-a-date"}, "ISO"),
        (
            {"title": "Engineer", "descriptionPlain": "Text", "publishedAt": "2026-08-20T10:00:00"},
            "timezone",
        ),
        ({"title": "Engineer", "descriptionHtml": []}, "descriptionHtml"),
    ],
)
def test_ashby_candidate_fails_closed_for_invalid_canonical_evidence(
    payload: dict[str, object], message: str
) -> None:
    with pytest.raises(IngestionError, match=message):
        ashby_candidate(_config(), _posting(payload))


def test_ashby_candidate_rejects_wrong_source_identity() -> None:
    posting = RawJobPosting(
        source_key="other",
        source_kind="ashby",
        external_id="https://jobs.ashbyhq.com/example/job",
        source_url="https://jobs.ashbyhq.com/example/job",
        apply_url=None,
        payload={"title": "Engineer", "descriptionPlain": "Text"},
        fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    with pytest.raises(IngestionError, match="identity does not match"):
        ashby_candidate(_config(), posting)


def _config() -> AshbySourceConfig:
    return AshbySourceConfig(
        key="example-ashby",
        kind="ashby",
        company_name="Example Ashby",
        board_name="example",
    )


def _posting(payload: dict[str, object]) -> RawJobPosting:
    job_url = "https://jobs.ashbyhq.com/example/job"
    return RawJobPosting(
        source_key="example-ashby",
        source_kind="ashby",
        external_id=job_url,
        source_url=job_url,
        apply_url="https://jobs.ashbyhq.com/example/job/application",
        payload=payload,
        fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
