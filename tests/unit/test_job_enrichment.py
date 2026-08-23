"""Source-aware retained-payload input builder tests, without persistence I/O."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from career_os.application.job_enrichment import (
    JobEnrichmentError,
    build_normalization_input,
)
from career_os.domain.normalization import JobNormalizationInput
from career_os.normalization.jobs import normalize_job
from career_os.persistence.models import JobPosting


def _job(raw: dict[str, object]) -> JobPosting:
    return cast(
        JobPosting,
        SimpleNamespace(
            title_raw="Engineer", description_text="Build Python systems.", raw_latest=raw
        ),
    )


def _fixture_job(name: str) -> dict[str, object]:
    payload = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures" / "ats" / name).read_text()
    )
    jobs = payload["jobs"]
    assert isinstance(jobs, list) and isinstance(jobs[0], dict)
    return cast(dict[str, object], jobs[0])


@pytest.mark.parametrize(
    ("source_kind", "raw", "expected"),
    [
        (
            "greenhouse",
            _fixture_job("greenhouse/populated.json"),
            {"locations": ("Warsaw, Poland",), "employment": None, "workplace": None},
        ),
        (
            "lever",
            {"categories": {"location": "Berlin", "commitment": "FullTime"}},
            {"locations": ("Berlin",), "employment": "FullTime", "workplace": None},
        ),
        (
            "ashby",
            _fixture_job("ashby/populated.json"),
            {
                "locations": ("Warsaw, Poland", "Remote - Poland"),
                "employment": "FullTime",
                "workplace": "Remote",
            },
        ),
        (
            "workable_public",
            _fixture_job("workable_public/populated.json"),
            {
                "locations": ("Warsaw, Mazowieckie, Poland",),
                "employment": "full_time",
                "workplace": "remote",
            },
        ),
    ],
)
def test_builder_replays_only_supported_source_facts(
    source_kind: str, raw: dict[str, object], expected: dict[str, object]
) -> None:
    result = build_normalization_input(job=_job(raw), source_kind=source_kind)

    assert isinstance(result, JobNormalizationInput)
    assert tuple(item.raw for item in result.locations) == expected["locations"]
    assert result.source_employment_type == expected["employment"]
    assert result.source_workplace_type == expected["workplace"]
    if source_kind == "ashby":
        assert [
            (
                item.component_type,
                item.min_value,
                item.max_value,
                item.currency,
                item.interval,
                item.location_tier,
                item.raw_text,
            )
            for item in result.structured_compensation
        ] == [
            (
                "base_salary",
                Decimal("300000"),
                Decimal("420000"),
                "PLN",
                "year",
                "Senior Data Engineer",
                "PLN 300,000 - 420,000 annually",
            ),
            (
                "equity",
                Decimal("0.01"),
                Decimal("0.05"),
                None,
                "none",
                "Senior Data Engineer",
                "Equity percentage grant",
            ),
        ]
    if source_kind == "workable_public":
        assert result.locations[0].city == "Warsaw"
        assert result.locations[0].region == "Mazowieckie"
        assert result.locations[0].is_primary is True
        normalized = normalize_job(result)
        assert (normalized.locations[0].country_code, normalized.locations[0].method) == (
            "PL",
            "text_parse",
        )
        assert normalized.locations[0].confidence == Decimal("0.800")


def test_builder_fails_closed_for_malformed_stored_ashby_payload() -> None:
    with pytest.raises(JobEnrichmentError, match="unsupported Ashby compensation"):
        build_normalization_input(
            job=_job(
                {
                    "compensation": {
                        "compensationTiers": [
                            {
                                "components": [
                                    {
                                        "compensationType": "Mystery",
                                        "summary": "Unknown",
                                    }
                                ]
                            }
                        ]
                    }
                }
            ),
            source_kind="ashby",
        )


def test_builder_maps_observed_ashby_commission_and_equity_cash_value() -> None:
    result = build_normalization_input(
        job=_job(
            {
                "compensation": {
                    "compensationTiers": [
                        {
                            "tierSummary": "United States",
                            "components": [
                                {
                                    "compensationType": "Commission",
                                    "summary": "$20K commission",
                                    "currencyCode": "USD",
                                    "minValue": 20000,
                                    "maxValue": 20000,
                                    "interval": "1 YEAR",
                                },
                                {
                                    "compensationType": "EquityCashValue",
                                    "summary": "$30K equity value",
                                    "currencyCode": "USD",
                                    "minValue": 30000,
                                    "maxValue": 30000,
                                    "interval": "1 YEAR",
                                },
                            ],
                        }
                    ]
                }
            }
        ),
        source_kind="ashby",
    )

    assert [item.component_type for item in result.structured_compensation] == [
        "other",
        "equity",
    ]
    normalized = normalize_job(result)
    assert [item.component_type for item in normalized.compensation] == ["other", "equity"]
    assert [item.raw_text for item in normalized.compensation] == [
        "$20K commission",
        "$30K equity value",
    ]


def test_builder_accepts_actual_lever_top_level_list_with_unknown_optional_facts() -> None:
    raw = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "ats"
            / "lever"
            / "global-page-0.json"
        ).read_text()
    )
    assert isinstance(raw, list) and isinstance(raw[0], dict)
    result = build_normalization_input(job=_job(raw[0]), source_kind="lever")
    assert result.locations == ()
    assert result.source_employment_type is None
    assert result.source_workplace_type is None
