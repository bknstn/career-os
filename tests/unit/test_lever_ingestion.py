"""Lever canonical candidate mapping tests without persistence infrastructure."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from career_os.application import LEVER_NORMALIZER_VERSION, IngestionError, lever_candidate
from career_os.config.sources import LeverSourceConfig
from career_os.domain import RawJobPosting


def test_lever_candidate_uses_documented_fields_and_retains_raw_payload() -> None:
    payload = {
        "id": "lever-123",
        "text": "  Senior Platform Engineer  ",
        "description": "<p>Build <strong>safe</strong> systems.</p><script>ignore()</script>",
        "descriptionPlain": "Fallback is not selected when HTML description exists.",
        "lists": [
            {
                "text": "Requirements",
                "content": "<ul><li>Python</li><li>SQL</li></ul>",
            }
        ],
        "additional": "<p>We welcome applicants from all backgrounds.</p>",
        "hostedUrl": "https://jobs.lever.co/example/lever-123",
        "applyUrl": "https://jobs.lever.co/example/lever-123/apply",
        "categories": {"department": "Engineering", "team": "Infrastructure"},
        "createdAt": 1_700_000_000,
        "future": {"kept": True},
    }

    candidate = lever_candidate(_config(), _posting(payload))

    assert candidate.title_raw == "Senior Platform Engineer"
    assert candidate.description_html == (
        "<p>Build <strong>safe</strong> systems.</p>\n"
        "<h2>Requirements</h2><ul><li>Python</li><li>SQL</li></ul>\n"
        "<p>We welcome applicants from all backgrounds.</p>"
    )
    assert candidate.description_text == (
        "Build safe systems.\n\nRequirements\n• Python\n\n• SQL\n\n"
        "We welcome applicants from all backgrounds."
    )
    assert candidate.department_raw == "Engineering"
    assert candidate.team_raw == "Infrastructure"
    assert candidate.source_url == "https://jobs.lever.co/example/lever-123"
    assert candidate.apply_url == "https://jobs.lever.co/example/lever-123/apply"
    assert candidate.published_at is None
    assert candidate.normalizer_version == LEVER_NORMALIZER_VERSION
    assert candidate.raw_payload["createdAt"] == 1_700_000_000
    assert candidate.raw_payload["future"] == {"kept": True}


def test_lever_candidate_uses_documented_plain_description_fallback() -> None:
    payload = {
        "id": "lever-123",
        "text": "Engineer",
        "descriptionPlain": "Plain description",
    }

    candidate = lever_candidate(_config(), _posting(payload))

    assert candidate.description_text == "Plain description"
    assert candidate.description_html is None
    assert candidate.department_raw is None
    assert candidate.team_raw is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"description": "<p>description</p>"}, "no non-blank text title"),
        ({"text": "Engineer"}, "no non-blank description content"),
        ({"text": "Engineer", "description": "<script>only code</script>"}, "description"),
    ],
)
def test_lever_candidate_fails_closed_for_required_undocumented_or_empty_content(
    payload: dict[str, object], message: str
) -> None:
    with pytest.raises(IngestionError, match=message):
        lever_candidate(_config(), _posting({"id": "lever-123", **payload}))


def test_lever_candidate_rejects_a_raw_posting_from_another_source() -> None:
    posting = RawJobPosting(
        source_key="other",
        source_kind="lever",
        external_id="lever-123",
        source_url=None,
        apply_url=None,
        payload={"text": "Engineer", "description": "<p>description</p>"},
        fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
    )

    with pytest.raises(IngestionError, match="identity does not match"):
        lever_candidate(_config(), posting)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"text": "Engineer", "description": "<p>description</p>", "lists": {}},
            "lists was not a list",
        ),
        (
            {
                "text": "Engineer",
                "description": "<p>description</p>",
                "lists": [{"text": "Requirements", "content": 42}],
            },
            "lists item 0 content HTML was not a string",
        ),
        (
            {
                "text": "Engineer",
                "description": "<p>description</p>",
                "lists": [{"text": "Requirements", "contentPlain": "Unsupported fallback"}],
            },
            "lists item 0 content had no non-blank content",
        ),
        (
            {
                "text": "Engineer",
                "description": "<p>description</p>",
                "additionalPlain": ["not", "text"],
            },
            "additional plain text was not a string",
        ),
    ],
)
def test_lever_candidate_fails_closed_for_malformed_optional_description_sections(
    payload: dict[str, object], message: str
) -> None:
    with pytest.raises(IngestionError, match=message):
        lever_candidate(_config(), _posting({"id": "lever-123", **payload}))


def _config() -> LeverSourceConfig:
    return LeverSourceConfig(
        key="example-lever",
        kind="lever",
        company_name="Example Lever",
        site="example",
    )


def _posting(payload: dict[str, object]) -> RawJobPosting:
    return RawJobPosting(
        source_key="example-lever",
        source_kind="lever",
        external_id="lever-123",
        source_url=payload.get("hostedUrl") if isinstance(payload.get("hostedUrl"), str) else None,
        apply_url=payload.get("applyUrl") if isinstance(payload.get("applyUrl"), str) else None,
        payload=payload,
        fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
