"""Golden and validation tests for the canonical job candidate boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from career_os.domain import (
    CANONICAL_CONTENT_FIELDS,
    CANONICAL_HASH_FIELDS,
    CanonicalJobCandidate,
    content_hash,
    stable_json_dumps,
)
from career_os.normalization import clean_text, html_to_text, sanitize_html


def _candidate(**overrides: object) -> CanonicalJobCandidate:
    values: dict[str, object] = {
        "source_key": "example",
        "source_kind": "greenhouse",
        "external_id": "job-123",
        "company_name": "Example Inc.",
        "title_raw": "Senior Platform Engineer",
        "title_normalized": "Senior Platform Engineer",
        "description_text": "Build reliable systems.",
        "description_html": "<p>Build reliable systems.</p>",
        "observed_at": datetime(2026, 8, 18, 10, tzinfo=UTC),
        "raw_payload": {"id": "job-123", "metadata": {"team": "Platform"}},
        "normalizer_version": "1.0.0",
        "taxonomy_version": "1.0.0",
        "source_url": "HTTPS://EXAMPLE.COM/jobs/123",
        "apply_url": "https://example.com/jobs/123/apply",
        "published_at": datetime(2026, 8, 1, 12, tzinfo=UTC),
    }
    values.update(overrides)
    return CanonicalJobCandidate(**values)  # type: ignore[arg-type]


def test_text_cleanup_has_a_stable_golden_result() -> None:
    source_html = """
      <section><h2>  What\u00a0you'll do </h2><ul><li>Build\tplatforms</li>
      <li>Operate &amp; improve</li></ul><script>alert('never')</script></section>
    """

    assert (
        clean_text("  Senior\u00a0\u00a0Engineer\r\n\r\n\r\nPlatform  ")
        == "Senior Engineer\n\nPlatform"
    )
    assert sanitize_html(source_html) == (
        "\n      <h2>  What" + "\u00a0" + "you'll do </h2><ul><li>Build\tplatforms</li>\n"
        "      <li>Operate &amp; improve</li></ul>\n    "
    )
    assert html_to_text(source_html) == "What you'll do\n\n• Build platforms\n\n• Operate & improve"


def test_mismatched_closing_tag_cannot_leak_dropped_content() -> None:
    source_html = "<template></style>alert(1)</template><p>safe</p>"

    assert sanitize_html(source_html) == "<p>safe</p>"
    assert html_to_text(source_html) == "safe"


def test_candidate_normalizes_text_html_urls_and_timestamps() -> None:
    candidate = _candidate(
        company_name="  Example\u00a0 Inc. ",
        description_html=(
            '<p class="ignored">Hello <strong>world</strong><script>bad()</script></p>'
        ),
        observed_at=datetime(2026, 8, 18, 12, tzinfo=timezone(timedelta(hours=2))),
        published_at=datetime(2026, 8, 1, 14, tzinfo=timezone(timedelta(hours=2))),
    )

    assert candidate.company_name == "Example Inc."
    assert candidate.description_html == "<p>Hello <strong>world</strong></p>"
    assert candidate.source_url == "https://example.com/jobs/123"
    assert candidate.observed_at == datetime(2026, 8, 18, 10, tzinfo=UTC)
    assert candidate.published_at == datetime(2026, 8, 1, 12, tzinfo=UTC)


def test_candidate_normalizes_an_absolute_ipv6_url() -> None:
    candidate = _candidate(source_url="HTTP://[2001:DB8::1]:0443/jobs/123")

    assert candidate.source_url == "http://[2001:db8::1]:443/jobs/123"


@pytest.mark.parametrize(
    ("overrides", "exception_type", "message"),
    [
        ({"title_raw": " \t "}, ValueError, "title_raw"),
        ({"source_key": 1}, TypeError, "source_key"),
        ({"source_url": "javascript:alert(1)"}, ValueError, "http"),
        ({"source_url": "https://example.com:99999/jobs"}, ValueError, "authority"),
        ({"source_url": "https://:443/jobs"}, ValueError, "http"),
        ({"source_url": "https://user@example.com/jobs"}, ValueError, "userinfo"),
        ({"source_url": "https://example.com/jobs\x00"}, ValueError, "control"),
        ({"observed_at": datetime(2026, 8, 18)}, ValueError, "timezone-aware"),
        ({"observed_at": "2026-08-18"}, TypeError, "datetime"),
        ({"published_at": datetime(2026, 8, 1)}, ValueError, "timezone-aware"),
        ({"published_at": "2026-08-01"}, TypeError, "datetime"),
        (
            {"raw_payload": {"when": datetime(2026, 8, 18, tzinfo=UTC)}},
            TypeError,
            "JSON-compatible",
        ),
        ({"raw_payload": {"amount": float("nan")}}, ValueError, "non-finite"),
    ],
)
def test_candidate_fails_closed_on_invalid_values(
    overrides: dict[str, object], exception_type: type[Exception], message: str
) -> None:
    with pytest.raises(exception_type, match=message):
        _candidate(**overrides)


def test_stable_serialization_and_hash_ignore_mapping_order() -> None:
    first = {"z": [True, {"b": 2, "a": 1}], "a": "é"}
    second = {"a": "é", "z": [True, {"a": 1, "b": 2}]}

    assert stable_json_dumps(first) == '{"a":"é","z":[true,{"a":1,"b":2}]}'
    assert stable_json_dumps(first) == stable_json_dumps(second)
    assert content_hash(first) == content_hash(second)
    assert content_hash(first) == (
        "55b5a0313c01f1b5f5e614cfc4c38d34bafde1c20e1ed8ee98a902ea96ccbe90"
    )


@pytest.mark.parametrize(
    "value",
    [{"decimal": Decimal("1.0")}, {"set": {"one"}}, {"bytes": b"one"}, {"range": range(3)}],
)
def test_stable_serialization_rejects_runtime_specific_values(value: object) -> None:
    with pytest.raises(TypeError, match="JSON-compatible"):
        stable_json_dumps(value)


def test_content_hash_covers_only_the_explicit_source_content() -> None:
    first = _candidate(raw_payload={"id": "job-123", "source_order": ["a", "b"]})
    same_content_new_observation = _candidate(
        observed_at=datetime(2026, 8, 19, 10, tzinfo=UTC),
        raw_payload={"source_order": ["a", "b"], "id": "job-123", "new_field": "retained"},
    )
    changed_content = _candidate(title_raw="Staff Platform Engineer")
    different_taxonomy = _candidate(taxonomy_version="2.0.0")

    assert tuple(first.canonical_content()) == CANONICAL_CONTENT_FIELDS
    assert tuple(first.canonical_hash_content()) == CANONICAL_HASH_FIELDS
    assert first.content_hash == same_content_new_observation.content_hash
    assert first.content_hash != changed_content.content_hash
    assert first.canonical_content() != different_taxonomy.canonical_content()
    assert first.content_hash == different_taxonomy.content_hash
    assert first.canonical_hash_content() == different_taxonomy.canonical_hash_content()
    assert "raw_payload" not in first.canonical_content()
    assert "observed_at" not in first.canonical_content()
