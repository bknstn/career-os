"""Bounded scheduling and robots-policy behavior for research collection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from career_os.application.research_collection import (
    ACQUIRED_REVISIT_INTERVAL,
    ROBOTS_USER_AGENT,
    ResearchRobotsPolicy,
    ScheduledResearchCandidate,
    fair_due_candidates,
    parse_robots_rules,
    robots_policy_allows,
)


def _candidate(
    source: str,
    url: str,
    *,
    has_version: bool = False,
    last_attempt_at: datetime | None = None,
) -> ScheduledResearchCandidate:
    return ScheduledResearchCandidate(
        source_key=source,
        canonical_url=f"https://{source}.example/{url}",
        discovered_url=f"https://{source}.example/{url}",
        title=None,
        has_version=has_version,
        last_attempt_at=last_attempt_at,
    )


def test_daily_due_queue_prioritizes_unfetched_and_round_robins_sources() -> None:
    now = datetime(2026, 10, 19, 9, tzinfo=UTC)
    candidates = (
        _candidate(
            "alpha", "a1", has_version=True, last_attempt_at=now - ACQUIRED_REVISIT_INTERVAL
        ),
        _candidate("alpha", "a2"),
        _candidate("alpha", "a3"),
        _candidate("beta", "b1"),
        _candidate("beta", "b2"),
        _candidate("gamma", "g1"),
    )

    selected = fair_due_candidates(candidates, ("alpha", "beta", "gamma"), now=now, limit=5)

    source_order = ("alpha", "beta", "gamma")
    offset = now.date().toordinal() % len(source_order)
    rotated = source_order[offset:] + source_order[:offset]
    assert [item.source_key for item in selected[:3]] == list(rotated)
    assert [item.source_key for item in selected[3:]] == [
        key for key in rotated if key in {"alpha", "beta"}
    ]
    assert selected[0].canonical_url.endswith("/a2")
    assert selected[0].has_version is False


def test_daily_due_queue_skips_recent_failures_and_unchanged_documents_until_due() -> None:
    now = datetime(2026, 10, 19, 9, tzinfo=UTC)
    candidates = (
        _candidate("alpha", "failed-recently", last_attempt_at=now - timedelta(hours=2)),
        _candidate(
            "alpha",
            "version-recently",
            has_version=True,
            last_attempt_at=now - timedelta(days=6),
        ),
        _candidate("alpha", "failed-due", last_attempt_at=now - timedelta(days=1)),
        _candidate(
            "beta",
            "version-due",
            has_version=True,
            last_attempt_at=now - ACQUIRED_REVISIT_INTERVAL,
        ),
    )

    selected = fair_due_candidates(candidates, ("alpha", "beta"), now=now, limit=8)

    assert {item.canonical_url.rsplit("/", maxsplit=1)[-1] for item in selected} == {
        "failed-due",
        "version-due",
    }


def test_robots_policy_prefers_specific_agent_group_then_uses_wildcard_fallback() -> None:
    specific_agent_text = """User-agent: CareerOSResearch
Disallow: /agent-only

User-agent: *
Disallow: /private
"""

    assert not robots_policy_allows(
        specific_agent_text, "https://publisher.example/agent-only/paper"
    )
    assert robots_policy_allows(specific_agent_text, "https://publisher.example/private/paper")
    wildcard_only_text = "User-agent: *\nDisallow: /private\n"
    assert not robots_policy_allows(wildcard_only_text, "https://publisher.example/private/paper")
    assert robots_policy_allows(wildcard_only_text, "https://publisher.example/research/paper")


def test_robots_blank_lines_do_not_end_agent_groups_and_matching_groups_merge() -> None:
    policy = """User-agent: *

Disallow: /private

User-agent: CareerOSResearch
Disallow: /internal
User-agent: CareerOSResearch
Allow: /internal/public
"""

    assert not robots_policy_allows(policy, "https://publisher.example/internal/secret")
    assert robots_policy_allows(policy, "https://publisher.example/internal/public/report")
    # The specific agent group wins over '*' after matching same-specific groups merge.
    assert robots_policy_allows(policy, "https://publisher.example/private/report")


def test_cached_robots_rules_are_evaluated_for_each_target_path_in_either_order() -> None:
    rules = parse_robots_rules(
        """User-agent: CareerOSResearch
Disallow: /private*
Allow: /private/public-paper$
"""
    )
    policy = ResearchRobotsPolicy(
        rules=rules,
        allow_all=False,
        outcome="discovered",
        error_code="robots_policy_loaded",
        http_status=200,
    )
    allowed_index = "https://publisher.example/research/index"
    denied_document = "https://publisher.example/private/report"

    assert policy.decision(allowed_index)[0]
    assert not policy.decision(denied_document)[0]
    assert not policy.decision(denied_document)[0]
    assert policy.decision(allowed_index)[0]
    assert robots_policy_allows(
        "User-agent: *\nDisallow: /private*\nAllow: /private/public-paper$\n",
        "https://publisher.example/private/public-paper",
    )
    assert not robots_policy_allows(
        "User-agent: *\nDisallow: /private*\nAllow: /private/public-paper$\n",
        "https://publisher.example/private/public-paper-extra",
    )
    assert ROBOTS_USER_AGENT == "CareerOSResearch"


def test_robots_longest_octet_match_and_allow_wins_ties() -> None:
    policy = """User-agent: *
Disallow: /docs/*/draft
Allow: /docs/public/draft
Disallow: /equal/path
Allow: /equal/path
"""

    assert robots_policy_allows(policy, "https://publisher.example/docs/public/draft")
    assert not robots_policy_allows(policy, "https://publisher.example/docs/private/draft")
    assert robots_policy_allows(policy, "https://publisher.example/equal/path")


def test_robots_matching_normalizes_unreserved_octets_and_utf8_but_keeps_reserved_escapes() -> None:
    policy = """User-agent: *
Disallow: /café/~private/%2Fsecret
"""

    assert not robots_policy_allows(
        policy, "https://publisher.example/caf%C3%A9/%7Eprivate/%2fsecret"
    )
    assert robots_policy_allows(policy, "https://publisher.example/caf%C3%A9/~private/secret")


def test_malformed_percent_escapes_fail_closed_for_the_target_path() -> None:
    policy = ResearchRobotsPolicy(
        rules=parse_robots_rules("User-agent: *\nDisallow: /private\n"),
        allow_all=False,
        outcome="discovered",
        error_code="robots_policy_loaded",
        http_status=200,
    )

    allowed, outcome, error_code, _ = policy.decision("https://publisher.example/%GG")

    assert not allowed
    assert outcome == "parse_error"
    assert error_code == "robots_policy_evaluation_error"


@pytest.mark.parametrize("directive", ["Disallow: private", "Disallow: /private$extra"])
def test_unsupported_applicable_robots_syntax_is_rejected(directive: str) -> None:
    with pytest.raises(ValueError, match="unsupported robots path pattern"):
        parse_robots_rules(f"User-agent: *\n{directive}\n")
