"""Replay retained official Warsaw source evidence through canonical mapping."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from career_os.application.ingestion import greenhouse_candidate
from career_os.config.sources import GreenhouseSourceConfig
from career_os.domain import RawJobPosting
from career_os.domain.normalization import JobNormalizationInput
from career_os.normalization.jobs import normalize_job


def test_retained_warsaw_postings_map_to_reviewed_roles() -> None:
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures/normalization/waymo-2026-09-08.json").read_text()
    )
    assert len(fixture["jobs"]) == 9
    for row in fixture["jobs"]:
        candidate = _candidate(row["title"], row["content"])
        assert "<li>" not in candidate.description_text
        result = normalize_job(
            JobNormalizationInput(title=candidate.title_raw, description=candidate.description_text)
        )
        assert [result.role_family.value, result.seniority.value] == row["expected"], row["id"]
        if result.role_family.method == "description_rule":
            assert result.role_family.evidence in candidate.description_text


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("<p>Use &lt;T&gt; and C++.</p>", "Use <T> and C++."),
        ("&lt;p&gt;Use &amp;lt;T&amp;gt; and C++.&lt;/p&gt;", "Use <T> and C++."),
        ("Ordinary text &amp; APIs", "Ordinary text & APIs"),
    ],
)
def test_greenhouse_decodes_one_document_layer_preserving_inline_text(
    content: str, expected: str
) -> None:
    candidate = _candidate("Software Engineer", content)
    assert candidate.description_text == expected
    assert candidate.raw_payload["content"] == content
    assert candidate.normalizer_version == "greenhouse-v2"


def test_mixed_ic_manager_title_remains_unknown() -> None:
    result = normalize_job(
        JobNormalizationInput(
            title="Staff Software Engineer / Technical Lead Manager", description=""
        )
    )
    assert result.role_family.value is None
    assert result.seniority.value is None


def _candidate(title: str, content: str):
    config = GreenhouseSourceConfig(
        key="waymo-greenhouse",
        company_name="Waymo",
        kind="greenhouse",
        board_token="waymo",
        enabled=True,
    )
    posting = RawJobPosting(
        source_key=config.key,
        source_kind="greenhouse",
        external_id="test",
        source_url=None,
        apply_url=None,
        payload={"title": title, "content": content},
        fetched_at=datetime(2026, 9, 8, tzinfo=UTC),
    )
    return greenhouse_candidate(config, posting)
