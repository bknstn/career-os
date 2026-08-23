"""Pure operational-policy contract tests."""

from __future__ import annotations

import pytest

from career_os.application.operations import CollectionOperationalPolicy


def test_default_operational_policy_is_bounded_and_conservative() -> None:
    policy = CollectionOperationalPolicy()

    assert policy.max_fetch_drop_ratio == 0.5
    assert policy.circuit_failure_threshold == 3
    assert policy.circuit_cooldown_seconds == 900
    assert policy.freshness_target_seconds == 86_400


@pytest.mark.parametrize(
    "values",
    [
        {"max_fetch_drop_ratio": -0.1},
        {"max_fetch_drop_ratio": 1.0},
        {"circuit_failure_threshold": 0},
        {"circuit_cooldown_seconds": 0},
        {"freshness_target_seconds": 0},
    ],
)
def test_operational_policy_rejects_unsafe_boundaries(values: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        CollectionOperationalPolicy(**values)  # type: ignore[arg-type]
