"""Replay and scheduling checks for durable research monitoring."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from career_os.application.research_monitor import ResearchMonitor, ResearchMonitorError

LOCAL = ZoneInfo("Europe/Warsaw")


def _time(year: int, month: int, day: int, hour: int = 10) -> datetime:
    return datetime(year, month, day, hour, tzinfo=LOCAL)


def _claim(
    claim_id: str,
    statement: str,
    created_at: datetime,
    *,
    current: bool = True,
    question: str = "developer_tasks",
    excluded_reason: str | None = None,
    review_state: str = "accepted",
    digest: str = "a" * 64,
) -> dict[str, Any]:
    return {
        "id": claim_id,
        "document_version_id": f"version-{claim_id}",
        "question": question,
        "statement": statement,
        "exact_quote": f"Quoted evidence for {claim_id}.",
        "location": {
            "media_type": "text/html",
            "start_offset": 20,
            "end_offset": 52,
            "section": "Results",
        },
        "evidence_kind": "observed",
        "evidence_details": {
            "metric_definition": "reported task share",
            "metric_value": "42%",
            "denominator": "respondents",
            "geography": "US",
            "sample_description": "Software developers surveyed",
            "sample_size": 100,
            "data_period": {"start": "2026-01-01", "end": "2026-03-31", "state": "reported"},
            "limitations": "Self-reported and limited to the sampled population.",
        },
        "lineage_kind": "primary",
        "upstream_dataset_or_document": None,
        "created_at": created_at.isoformat(),
        "review_state": review_state,
        "current_evidence": current,
        "excluded_reason": excluded_reason,
        "document": {
            "id": f"doc-{claim_id}",
            "source_key": "publisher",
            "canonical_url": f"https://example.test/{claim_id}",
        },
        "version": {
            "id": f"version-{claim_id}",
            "version_number": 1,
            "title": f"Study {claim_id}",
            "publication_date": {"value": "2026-04-01", "state": "reported"},
            "retrieved_at": created_at.isoformat(),
            "media_type": "text/html",
            "raw_sha256": "b" * 64,
            "normalized_text_sha256": digest,
            "extraction_version": "html-v1",
        },
    }


def _document(
    document_id: str, digest: str, version_id: str, *, number: int = 1, raw: str = "b"
) -> dict[str, Any]:
    return {
        "id": document_id,
        "source_key": "publisher",
        "canonical_url": f"https://example.test/{document_id}",
        "latest_version": {
            "id": version_id,
            "version_number": number,
            "title": f"Study {document_id}",
            "raw_sha256": raw * 64,
            "normalized_text_sha256": digest,
        },
    }


def _snapshot(
    claims: list[dict[str, Any]] | None = None,
    documents: list[dict[str, Any]] | None = None,
) -> dict[str, object]:
    return {"claims": claims or [], "documents": documents or []}


def _collector() -> dict[str, object]:
    return {"status": "succeeded", "failed": 0, "acquired": 0, "new_versions": 0}


def test_weekly_due_emits_two_items_without_padding_and_replay_is_quiet(tmp_path: Path) -> None:
    monitor = ResearchMonitor(tmp_path / "monitor.json")
    claims: list[dict[str, Any]] = []

    first = monitor.run(
        collector=_collector,
        snapshot_provider=lambda: _snapshot(claims),
        now=_time(2026, 10, 20),
    )
    assert first.due_outputs == ()
    assert first.weekly_output is None

    claims.extend(
        [
            _claim("claim-1", "AI changes some drafting tasks.", _time(2026, 10, 21)),
            _claim("claim-2", "Review time shifts toward verification.", _time(2026, 10, 22)),
        ]
    )
    monitor.run(
        collector=_collector,
        snapshot_provider=lambda: _snapshot(claims),
        now=_time(2026, 10, 22),
    )
    result = monitor.run(
        collector=_collector,
        snapshot_provider=lambda: _snapshot(claims),
        now=_time(2026, 10, 26, 9),
    )

    assert result.due_outputs == ("weekly",)
    assert result.weekly_output is not None
    report = json.loads(Path(result.weekly_output).read_text(encoding="utf-8"))
    assert report["item_count"] == 2
    assert report["below_target"] is True
    assert {item["accepted_claim_ids"][0] for item in report["items"]} == {"claim-1", "claim-2"}
    citation = report["items"][0]["citations"][0]
    assert citation["document_version_id"]
    assert citation["normalized_text_sha256"] == "a" * 64
    assert citation["exact_quote"]
    assert citation["location"]["section"] == "Results"
    replay = monitor.run(
        collector=_collector,
        snapshot_provider=lambda: _snapshot(claims),
        now=_time(2026, 10, 26, 9),
    )
    assert replay.weekly_output is None
    assert replay.due_outputs == ()
    assert replay.notifications == ()


def test_monthly_proposal_stays_unresolved_until_explicit_complete_review(tmp_path: Path) -> None:
    monitor = ResearchMonitor(tmp_path / "monitor.json")
    claims = [
        _claim("tasks", "Task change claim.", _time(2026, 10, 10), question="developer_tasks"),
        _claim(
            "seniority", "Seniority claim.", _time(2026, 10, 11), question="experience_seniority"
        ),
        _claim(
            "skills",
            "Production skill claim.",
            _time(2026, 10, 12),
            question="production_ai_skills",
        ),
    ]

    def snapshot():
        return _snapshot(claims)

    monitor.run(collector=_collector, snapshot_provider=snapshot, now=_time(2026, 10, 20))
    result = monitor.run(collector=_collector, snapshot_provider=snapshot, now=_time(2026, 11, 4))

    assert result.monthly_proposal is not None
    proposal = json.loads(Path(result.monthly_proposal).read_text(encoding="utf-8"))
    assert proposal["review_state"] == "unresolved"
    assert len(proposal["hypotheses"]) == 3
    assert {item["status"] for item in proposal["hypotheses"]} == {"unresolved"}
    assert sum(len(item["accepted_claim_ids"]) for item in proposal["hypotheses"]) == 3

    review = {
        "period_id": "2026-11",
        "reviewer": "codex:parent",
        "hypotheses": [
            {
                "id": "developer_task_mix_change",
                "status": "mixed",
                "rationale": "The accepted claim indicates a task shift, with limited scope.",
                "accepted_claim_ids": ["tasks"],
                "alternative_explanations": ["Survey selection could shape the reported mix."],
                "limitations": ["One survey does not represent all developers."],
            },
            {
                "id": "seniority_differentiation",
                "status": "insufficient",
                "rationale": "The claim does not establish a comparative effect.",
                "accepted_claim_ids": ["seniority"],
                "alternative_explanations": [
                    "Role composition could explain observed differences."
                ],
                "limitations": ["No comparable seniority groups are reported."],
            },
            {
                "id": "production_ai_skills_shift",
                "status": "supported",
                "rationale": "The claim directly concerns production evaluation skills.",
                "accepted_claim_ids": ["skills"],
                "alternative_explanations": [
                    "Broader AI hiring demand may also drive skill mentions."
                ],
                "limitations": ["Evidence is limited to one source and period."],
            },
        ],
    }
    output = monitor.publish_monthly_review(
        review, snapshot_provider=snapshot, now=_time(2026, 11, 5)
    )
    published = json.loads(Path(output).read_text(encoding="utf-8"))
    assert published["review_state"] == "reviewed"
    assert {item["status"] for item in published["hypotheses"]} == {
        "mixed",
        "insufficient",
        "supported",
    }
    same_output = monitor.publish_monthly_review(
        review, snapshot_provider=snapshot, now=_time(2026, 11, 6)
    )
    assert same_output == output
    claims.append(
        _claim(
            "unrelated-current-claim",
            "A separate task finding was accepted.",
            _time(2026, 11, 6),
        )
    )
    same_after_unrelated_evidence = monitor.publish_monthly_review(
        review, snapshot_provider=snapshot, now=_time(2026, 11, 6, 11)
    )
    assert same_after_unrelated_evidence == output

    corrected = json.loads(json.dumps(review))
    corrected["hypotheses"][0]["rationale"] = (
        "Correction: the finding is limited to the sampled respondents."
    )
    corrected_output = monitor.publish_monthly_review(
        corrected, snapshot_provider=snapshot, now=_time(2026, 11, 7)
    )
    corrected_record = json.loads(Path(corrected_output).read_text(encoding="utf-8"))
    assert corrected_output != output
    assert corrected_record["review_revision"] == 2
    assert corrected_record["supersedes"] == Path(output).name
    assert Path(output).exists()
    products = monitor.latest_products(snapshot_provider=snapshot)
    monthly_reviews = products["monthly_reviews"]
    assert isinstance(monthly_reviews, list)
    assert len(monthly_reviews[0]["revisions"]) == 2
    assert monthly_reviews[0]["latest"]["review_revision"] == 2

    incomplete = dict(review)
    incomplete["hypotheses"] = review["hypotheses"][:2]
    with pytest.raises(ResearchMonitorError, match="all three"):
        monitor.publish_monthly_review(
            incomplete, snapshot_provider=snapshot, now=_time(2026, 11, 5)
        )


def test_equivalent_version_keeps_disposition_and_changed_text_reopens_queue(
    tmp_path: Path,
) -> None:
    monitor = ResearchMonitor(tmp_path / "monitor.json")
    first_digest = "a" * 64
    documents = [_document("doc-1", first_digest, "version-1")]

    def snapshot():
        return _snapshot(documents=documents)

    first = monitor.run(collector=_collector, snapshot_provider=snapshot, now=_time(2026, 10, 20))
    assert len(first.pending_versions) == 1
    monitor.record_version_disposition(
        document_id="doc-1",
        normalized_text_sha256=first_digest,
        disposition="no_claims",
        reviewer="codex:parent",
        reason="No supported claim in the reviewed text.",
        now=_time(2026, 10, 20, 11),
    )

    documents[0] = _document("doc-1", first_digest, "version-2", number=2, raw="c")
    equivalent = monitor.run(
        collector=_collector, snapshot_provider=snapshot, now=_time(2026, 10, 21)
    )
    assert equivalent.pending_versions == ()
    assert not any(
        event["type"] == "version_review_queue_changed" for event in equivalent.notifications
    )

    changed_digest = "d" * 64
    documents[0] = _document("doc-1", changed_digest, "version-3", number=3, raw="e")
    changed = monitor.run(collector=_collector, snapshot_provider=snapshot, now=_time(2026, 10, 22))
    assert len(changed.pending_versions) == 1
    assert changed.pending_versions[0]["normalized_text_sha256"] == changed_digest
    assert any(event["type"] == "version_review_queue_changed" for event in changed.notifications)
    assert (
        monitor.record_version_disposition(
            document_id="doc-1",
            normalized_text_sha256=changed_digest,
            disposition="reviewed",
            reviewer="codex:parent",
            reason="Reviewed against the new text.",
            now=_time(2026, 10, 22, 11),
        )["disposition"]
        == "reviewed"
    )


def test_retracted_claim_leaves_current_evidence_and_prior_report_is_historical(
    tmp_path: Path,
) -> None:
    monitor = ResearchMonitor(tmp_path / "monitor.json")
    claims: list[dict[str, Any]] = []

    def snapshot():
        return _snapshot(claims)

    monitor.run(collector=_collector, snapshot_provider=snapshot, now=_time(2026, 10, 20))
    claims.append(
        _claim("claim-new", "Review effort shifts toward verification.", _time(2026, 10, 21))
    )
    monitor.run(collector=_collector, snapshot_provider=snapshot, now=_time(2026, 10, 21))
    weekly = monitor.run(
        collector=_collector, snapshot_provider=snapshot, now=_time(2026, 10, 26, 9)
    )
    assert weekly.weekly_output is not None
    historical_path = Path(weekly.weekly_output)

    claims[0] = _claim(
        "claim-new",
        "Review effort shifts toward verification.",
        _time(2026, 10, 21),
        current=False,
        review_state="retracted",
        excluded_reason="retracted",
        digest="f" * 64,
    )
    retracted = monitor.run(
        collector=_collector, snapshot_provider=snapshot, now=_time(2026, 10, 27)
    )
    assert any(event["type"] == "claim_no_longer_current" for event in retracted.notifications)
    history = json.loads(historical_path.read_text(encoding="utf-8"))
    assert history["report_kind"] == "historical_snapshot"
    assert history["items"][0]["accepted_claim_ids"] == ["claim-new"]
    saved_state = json.loads((tmp_path / "monitor.json").read_text(encoding="utf-8"))
    assert saved_state["current_claim_ids"] == []


def test_missed_weeks_and_months_coalesce_to_one_recovery_product_each(tmp_path: Path) -> None:
    monitor = ResearchMonitor(tmp_path / "monitor.json")

    def snapshot():
        return _snapshot()

    monitor.run(collector=_collector, snapshot_provider=snapshot, now=_time(2026, 10, 20))
    recovered = monitor.run(
        collector=_collector, snapshot_provider=snapshot, now=_time(2027, 2, 10)
    )
    assert recovered.due_outputs == ("weekly", "monthly")
    assert recovered.weekly_output is not None and "weekly-recovery" in recovered.weekly_output
    assert (
        recovered.monthly_proposal is not None
        and "monthly-proposal-recovery" in recovered.monthly_proposal
    )
    assert len(list((tmp_path / "research").glob("*.json"))) == 2
    replay = monitor.run(collector=_collector, snapshot_provider=snapshot, now=_time(2027, 2, 10))
    assert replay.due_outputs == ()
    assert len(list((tmp_path / "research").glob("*.json"))) == 2


def test_repeated_collection_failure_notifies_only_on_new_failure(tmp_path: Path) -> None:
    monitor = ResearchMonitor(tmp_path / "monitor.json")
    collection: dict[str, object] = {
        "status": "partial",
        "failed": 2,
        "acquired": 1,
        "run_id": "run-1",
        "body_bytes": 200,
        "source_failures": [
            {
                "source_key": "publisher-a",
                "outcome": "network_error",
                "error_code": "timeout",
                "http_status": None,
            }
        ],
    }

    def partial():
        return dict(collection)

    def snapshot():
        return _snapshot()

    first = monitor.run(collector=partial, snapshot_provider=snapshot, now=_time(2026, 10, 20))
    collection["failed"] = 8
    collection["run_id"] = "run-2"
    collection["body_bytes"] = 900
    again = monitor.run(collector=partial, snapshot_provider=snapshot, now=_time(2026, 10, 21))
    assert [event["type"] for event in first.notifications] == ["collection_failure"]
    assert again.notifications == ()

    collection["source_failures"] = [
        {
            "source_key": "publisher-a",
            "outcome": "http_error",
            "error_code": "http_403",
            "http_status": 403,
        }
    ]
    changed_error = monitor.run(
        collector=partial, snapshot_provider=snapshot, now=_time(2026, 10, 22)
    )
    assert [event["type"] for event in changed_error.notifications] == ["collection_failure"]

    collection["source_failures"] = [
        {
            "source_key": "publisher-b",
            "outcome": "http_error",
            "error_code": "http_403",
            "http_status": 403,
        }
    ]
    changed_source = monitor.run(
        collector=partial, snapshot_provider=snapshot, now=_time(2026, 10, 23)
    )
    assert [event["type"] for event in changed_source.notifications] == ["collection_failure"]


def test_overlapping_monitor_calls_are_serialized(tmp_path: Path) -> None:
    monitor = ResearchMonitor(tmp_path / "monitor.json")
    entered = threading.Event()
    release = threading.Event()
    guard = threading.Lock()
    active = 0
    max_active = 0
    call_count = 0
    errors: list[BaseException] = []
    results: list[str] = []

    def collector() -> dict[str, object]:
        nonlocal active, max_active, call_count
        with guard:
            call_count += 1
            this_call = call_count
            active += 1
            max_active = max(max_active, active)
        if this_call == 1:
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release first monitor call")
        with guard:
            active -= 1
        return _collector()

    def invoke(now: datetime) -> None:
        try:
            result = monitor.run(
                collector=collector, snapshot_provider=lambda: _snapshot(), now=now
            )
            results.append(result.status)
        except BaseException as error:  # make thread failures visible to the test
            errors.append(error)

    first = threading.Thread(target=invoke, args=(_time(2026, 10, 20),))
    second = threading.Thread(target=invoke, args=(_time(2026, 10, 21),))
    first.start()
    assert entered.wait(timeout=2)
    second.start()
    second.join(timeout=2)
    assert not second.is_alive()
    assert results == ["already_running"]
    release.set()
    first.join(timeout=3)

    assert not errors
    assert max_active == 1
    assert call_count == 1
    assert sorted(results) == ["already_running", "succeeded"]


def test_first_run_can_bootstrap_current_products_without_moving_future_schedule(
    tmp_path: Path,
) -> None:
    monitor = ResearchMonitor(tmp_path / "monitor.json")
    claims = [
        _claim("existing-1", "Existing task finding one.", _time(2026, 10, 1)),
        _claim("existing-2", "Existing task finding two.", _time(2026, 10, 2)),
    ]
    result = monitor.run(
        collector=_collector,
        snapshot_provider=lambda: _snapshot(claims),
        now=_time(2026, 10, 20),
        bootstrap=True,
    )
    assert result.due_outputs == ("weekly", "monthly")
    assert result.weekly_output is not None and "weekly-bootstrap" in result.weekly_output
    assert (
        result.monthly_proposal is not None
        and "monthly-proposal-bootstrap" in result.monthly_proposal
    )
    report = json.loads(Path(result.weekly_output).read_text(encoding="utf-8"))
    assert report["trigger"] == "initial_bootstrap"
    assert report["item_count"] == 2
    state = json.loads((tmp_path / "monitor.json").read_text(encoding="utf-8"))
    assert state["next_weekly_due_at"].startswith("2026-10-26T09:00")
    assert state["next_monthly_due_at"].startswith("2026-11-01T09:00")


def test_latest_products_read_does_not_create_state_or_output_paths(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing"
    monitor = ResearchMonitor(missing_root / "monitor.json")
    products = monitor.latest_products()
    assert products["weekly"] is None
    assert not missing_root.exists()


def test_latest_products_rechecks_citations_before_next_monitor_run(tmp_path: Path) -> None:
    monitor = ResearchMonitor(tmp_path / "monitor.json")
    claims = [
        _claim("claim-tasks", "Task composition changes.", _time(2026, 10, 10)),
        _claim(
            "claim-seniority",
            "Effects differ across seniority groups.",
            _time(2026, 10, 11),
            question="experience_seniority",
        ),
        _claim(
            "claim-skills",
            "Production use increases reliability skill value.",
            _time(2026, 10, 12),
            question="production_ai_skills",
        ),
    ]

    def snapshot():
        return _snapshot(claims)

    first = monitor.run(
        collector=_collector,
        snapshot_provider=snapshot,
        now=_time(2026, 10, 20),
        bootstrap=True,
    )
    assert first.monthly_proposal is not None
    review = {
        "period_id": "2026-10",
        "reviewer": "codex:parent",
        "hypotheses": [
            {
                "id": "developer_task_mix_change",
                "status": "supported",
                "rationale": "The accepted evidence directly measures task composition.",
                "accepted_claim_ids": ["claim-tasks"],
                "alternative_explanations": ["Survey selection could affect the estimate."],
                "limitations": ["The sample covers one population."],
            },
            {
                "id": "seniority_differentiation",
                "status": "mixed",
                "rationale": "The evidence reports differences but has limited comparability.",
                "accepted_claim_ids": ["claim-seniority"],
                "alternative_explanations": ["Role mix could explain the difference."],
                "limitations": ["Only one source compares groups."],
            },
            {
                "id": "production_ai_skills_shift",
                "status": "supported",
                "rationale": "The claim links production use with reliability skills.",
                "accepted_claim_ids": ["claim-skills"],
                "alternative_explanations": ["General hiring demand may affect skill value."],
                "limitations": ["The finding is not a broad labor-market measure."],
            },
        ],
    }
    artifact = Path(
        monitor.publish_monthly_review(review, snapshot_provider=snapshot, now=_time(2026, 10, 21))
    )
    assert (
        json.loads(artifact.read_text(encoding="utf-8"))["hypotheses"][0]["status"] == "supported"
    )

    claims[0] = _claim(
        "claim-tasks",
        "Task composition changes.",
        _time(2026, 10, 10),
        current=False,
        review_state="retracted",
        excluded_reason="retracted",
        digest="f" * 64,
    )
    products = monitor.latest_products(snapshot_provider=snapshot)
    reviews = products["monthly_reviews"]
    assert isinstance(reviews, list)
    latest = reviews[0]["latest"]
    assert latest["hypotheses"][0]["published_status"] == "supported"
    assert latest["hypotheses"][0]["status"] == "unresolved"
    assert latest["hypotheses"][0]["review_state"] == "review_required"
    assert latest["hypotheses"][0]["excluded_claim_ids"] == ["claim-tasks"]
    assert products["latest_monthly_statuses"][0]["status"] == "unresolved"

    weekly = products["weekly"]["product"]
    task_item = next(
        item for item in weekly["items"] if item["historical_accepted_claim_ids"] == ["claim-tasks"]
    )
    assert task_item["current_eligibility"] == "historical_only"
    assert task_item["current_accepted_claim_ids"] == []
    assert task_item["accepted_claim_ids"] == []
    assert task_item["excluded_claim_ids"] == ["claim-tasks"]
    assert task_item["citations"] == []
    assert task_item["historical_citations"][0]["currently_eligible"] is False
    assert (
        json.loads(artifact.read_text(encoding="utf-8"))["hypotheses"][0]["status"] == "supported"
    )


def test_latest_products_without_snapshot_does_not_present_saved_review_as_current(
    tmp_path: Path,
) -> None:
    monitor = ResearchMonitor(tmp_path / "monitor.json")
    claims = [
        _claim("claim-tasks", "Task composition changes.", _time(2026, 10, 10)),
        _claim(
            "claim-seniority",
            "Effects differ.",
            _time(2026, 10, 11),
            question="experience_seniority",
        ),
        _claim(
            "claim-skills",
            "Production skills matter.",
            _time(2026, 10, 12),
            question="production_ai_skills",
        ),
    ]

    def snapshot():
        return _snapshot(claims)

    monitor.run(
        collector=_collector,
        snapshot_provider=snapshot,
        now=_time(2026, 10, 20),
        bootstrap=True,
    )
    review = {
        "period_id": "2026-10",
        "reviewer": "codex:parent",
        "hypotheses": [
            {
                "id": hypothesis_id,
                "status": "supported",
                "rationale": "Current evidence supports this interpretation.",
                "accepted_claim_ids": [claim_id],
                "alternative_explanations": ["Sampling could affect the result."],
                "limitations": ["The evidence base is small."],
            }
            for hypothesis_id, claim_id in (
                ("developer_task_mix_change", "claim-tasks"),
                ("seniority_differentiation", "claim-seniority"),
                ("production_ai_skills_shift", "claim-skills"),
            )
        ],
    }
    monitor.publish_monthly_review(review, snapshot_provider=snapshot, now=_time(2026, 10, 21))

    products = monitor.latest_products()
    assert products["eligibility_check_status"] == "not_checked"
    assert {item["status"] for item in products["latest_monthly_statuses"]} == {"unresolved"}
    assert {item["review_state"] for item in products["latest_monthly_statuses"]} == {
        "review_required"
    }


def test_partial_collection_is_reflected_in_monitor_status(tmp_path: Path) -> None:
    monitor = ResearchMonitor(tmp_path / "monitor.json")
    result = monitor.run(
        collector=lambda: {"status": "partial", "failed": 0},
        snapshot_provider=lambda: _snapshot(),
        now=_time(2026, 10, 20),
    )
    assert result.status == "collection_failed"
