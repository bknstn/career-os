"""Pure denominator and reconciliation invariants for market DTOs."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from career_os.application.market import (
    MarketAggregateService,
    MarketDataConsistencyError,
    _assert_same_selection,
)
from career_os.domain.markets import CompensationAggregate, MarketAggregates, MarketBucket


def test_bucket_exposes_exact_denominator_and_share() -> None:
    first, second = sorted((uuid4(), uuid4()))
    bucket = MarketBucket("python", (first,), (first, second))

    assert bucket.job_count == 1
    assert bucket.denominator == 2
    assert bucket.job_share == Decimal("0.5")


def test_bucket_rejects_duplicate_unsorted_or_out_of_denominator_ids() -> None:
    first, second = sorted((uuid4(), uuid4()))
    with pytest.raises(ValueError, match="stable order"):
        MarketBucket("python", (second, first), (first, second))
    with pytest.raises(ValueError, match="subset"):
        MarketBucket("python", (second,), (first,))


def test_labels_and_service_versions_must_be_nonblank() -> None:
    job_id = uuid4()
    with pytest.raises(ValueError, match="value"):
        MarketBucket("  ", (job_id,), (job_id,))
    with pytest.raises(ValueError, match="taxonomy_version"):
        MarketAggregateService(object(), taxonomy_version=" ", normalizer_version="v1")  # type: ignore[arg-type]


def test_selection_change_fails_closed() -> None:
    first, second = sorted((uuid4(), uuid4()))
    _assert_same_selection((first, second), (first, second))

    with pytest.raises(MarketDataConsistencyError, match="selection changed"):
        _assert_same_selection((first, second), (first,))


def test_market_aggregate_validates_cutoff_versions_and_denominator_subsets() -> None:
    first, second = sorted((uuid4(), uuid4()))
    valid = _market(matching=(first, second), skill_denominator=(first,))
    assert valid.matching_job_count == 2

    with pytest.raises(ValueError, match="skill denominator"):
        _market(matching=(first,), skill_denominator=(second,))
    with pytest.raises(ValueError, match="timezone-aware"):
        _market(matching=(first,), cutoff=datetime(2026, 8, 21))
    with pytest.raises(ValueError, match="market_name"):
        _market(matching=(first,), market_name=" ")
    with pytest.raises(ValueError, match="normalizer_version"):
        _market(matching=(first,), normalizer_version="")
    wrong_denominator = MarketBucket("Example", (first,), (first, second))
    with pytest.raises(ValueError, match="companies"):
        _market(matching=(first,), companies=(wrong_denominator,))


def test_compensation_ids_counts_share_and_numeric_bounds_are_explicit() -> None:
    first, second = sorted((uuid4(), uuid4()))
    component_one, component_two = sorted((uuid4(), uuid4()))
    group = CompensationAggregate(
        component_type="base_salary",
        currency="USD",
        interval="year",
        location_tier="Poland",
        component_ids=(component_one, component_two),
        job_ids=(first,),
        denominator_job_ids=(first, second),
        min_value=Decimal("100"),
        max_value=Decimal("200"),
    )

    assert (group.component_count, group.job_count, group.denominator) == (2, 1, 2)
    assert group.job_share == Decimal("0.5")
    with pytest.raises(ValueError, match="reconcile"):
        _market(matching=(first, second), compensation_job_ids=(first,))
    with pytest.raises(ValueError, match="finite non-negative"):
        _compensation(minimum=Decimal("NaN"), maximum=Decimal("200"))
    with pytest.raises(ValueError, match="must not exceed"):
        _compensation(minimum=Decimal("201"), maximum=Decimal("200"))
    with pytest.raises(ValueError, match="component_type"):
        _compensation(component_type=" ")


def _market(
    *,
    matching: tuple = (),
    skill_denominator: tuple = (),
    cutoff: datetime | None = datetime(2026, 8, 21, tzinfo=UTC),
    market_name: str = "market",
    normalizer_version: str = "normalizer-v1",
    companies: tuple[MarketBucket, ...] = (),
    compensation_job_ids: tuple = (),
) -> MarketAggregates:
    return MarketAggregates(
        market_name=market_name,
        matching_job_ids=matching,
        skill_denominator_job_ids=skill_denominator,
        compensation_job_ids=compensation_job_ids,
        collection_cutoff=cutoff,
        taxonomy_version="1.0.0",
        normalizer_version=normalizer_version,
        companies=companies,
        skills=(),
        seniorities=(),
        workplace_types=(),
        countries=(),
        compensation=(),
    )


def _compensation(
    *,
    minimum: Decimal = Decimal("100"),
    maximum: Decimal = Decimal("200"),
    component_type: str = "base_salary",
) -> CompensationAggregate:
    job_id = uuid4()
    return CompensationAggregate(
        component_type=component_type,
        currency="USD",
        interval="year",
        location_tier=None,
        component_ids=(uuid4(),),
        job_ids=(job_id,),
        denominator_job_ids=(job_id,),
        min_value=minimum,
        max_value=maximum,
    )
