"""Durable scheduling and review products for the research evidence stream.

This module has no import-time filesystem, database, or network effects. Callers
provide the bounded collector and a read-only evidence snapshot when they run
the monitor command.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

SnapshotProvider = Callable[[], Mapping[str, object]]
Collector = Callable[[], object]
Disposition = Literal["reviewed", "no_claims"]
_LOCAL_ZONE = ZoneInfo("Europe/Warsaw")
DEFAULT_RESEARCH_MONITOR_STATE_PATH = Path(".local/radar/research-monitor-state.json")
_STATE_SCHEMA_VERSION = 1
_MAX_NOTIFICATION_HISTORY = 500
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_HYPOTHESIS_STATUSES = frozenset({"supported", "mixed", "contradicted", "insufficient"})

_HYPOTHESES: tuple[dict[str, object], ...] = (
    {
        "id": "developer_task_mix_change",
        "question": "developer_tasks",
        "statement": (
            "Developer task mix is changing beyond adoption alone. Adoption or reported use "
            "by itself does not establish task-composition change."
        ),
    },
    {
        "id": "seniority_differentiation",
        "question": "experience_seniority",
        "statement": "AI impacts or task changes differ by experience or seniority.",
    },
    {
        "id": "production_ai_skills_shift",
        "question": "production_ai_skills",
        "statement": (
            "Production use increases the value of evaluation, reliability, integration, data, "
            "observability, or security skills relative to prompt-only familiarity."
        ),
    },
)


class ResearchMonitorError(ValueError):
    """Raised when saved monitor state or an explicit review is invalid."""


class ResearchMonitorBusyError(ResearchMonitorError):
    """Raised when another process currently owns the monitor state lock."""


@dataclass(frozen=True, slots=True)
class MonitorRunResult:
    """Stable JSON-friendly result returned by one daily monitor invocation."""

    status: str
    collection: dict[str, object] | None
    weekly_output: str | None
    monthly_proposal: str | None
    pending_versions: tuple[dict[str, object], ...]
    notifications: tuple[dict[str, object], ...]
    due_outputs: tuple[str, ...]
    state_updated_at: str

    def to_dict(self) -> dict[str, object]:
        """Return a stable JSON-ready representation for the CLI."""
        return {
            "status": self.status,
            "collection": self.collection,
            "weekly_output": self.weekly_output,
            "monthly_proposal": self.monthly_proposal,
            "pending_versions": list(self.pending_versions),
            "notifications": list(self.notifications),
            "due_outputs": list(self.due_outputs),
            "state_updated_at": self.state_updated_at,
        }


class ResearchMonitor:
    """Run collection and due products against injected local application seams.

    ``state_path`` identifies the durable JSON state file. Reports are written
    under a sibling ``research`` directory. The caller owns all network and DB
    configuration and passes its normal bounded collector and read-only API
    projection functions to :meth:`run`.
    """

    def __init__(self, state_path: Path) -> None:
        self._state_path = Path(state_path)
        self._output_dir = self._state_path.parent / "research"
        self._lock_path = self._state_path.with_name(self._state_path.name + ".lock")

    def latest_products(
        self, *, snapshot_provider: SnapshotProvider | None = None
    ) -> dict[str, object]:
        """Read products with fresh citation eligibility, without writing files.

        Stored products remain immutable historical records. A fresh snapshot
        annotates citations that are no longer current and makes a monthly status
        unresolved until it is explicitly reviewed again. Without a provider, or
        if the provider fails, saved statuses are not presented as current.
        """
        checked_at = datetime.now(UTC).astimezone(_LOCAL_ZONE).isoformat()
        current_claims: dict[str, dict[str, object]] | None = None
        check_error: str | None = None
        if snapshot_provider is not None:
            try:
                current_claims = _current_claims(_normalize_snapshot(snapshot_provider()))
            except Exception as error:
                check_error = type(error).__name__

        state = _load_state(self._state_path)
        if state is None:
            return {
                "schema_version": "career-radar.research.products.v1",
                "weekly": None,
                "monthly_proposal": None,
                "monthly_reviews": [],
                "latest_monthly_statuses": [],
                "eligibility_check_status": _eligibility_check_status(current_claims, check_error),
                "eligibility_checked_at": checked_at if snapshot_provider is not None else None,
                "eligibility_check_error_type": check_error,
                "state_updated_at": None,
            }
        weekly_name = _as_string(state.get("latest_weekly_output"))
        proposal_name = _as_string(state.get("latest_monthly_proposal"))
        weekly = _project_weekly_product(
            _read_product(self._output_dir, weekly_name), current_claims, check_error, checked_at
        )
        proposal = _project_monthly_product(
            _read_product(self._output_dir, proposal_name), current_claims, check_error, checked_at
        )
        artifacts = _mapping(state.get("monthly_review_artifacts"))
        review_history: list[dict[str, object]] = []
        latest_statuses: list[dict[str, object]] = []
        latest_review_period: str | None = None
        for period_id, raw_history in sorted(artifacts.items()):
            rows = _mapping_list(raw_history)
            products = [
                _project_monthly_product(product, current_claims, check_error, checked_at)
                for row in rows
                if (product := _read_product(self._output_dir, _as_string(row.get("path"))))
                is not None
            ]
            if not products:
                continue
            latest = products[-1]
            if latest is None:
                continue
            review_history.append({"period_id": period_id, "revisions": products, "latest": latest})
            latest_review_period = period_id
            latest_statuses = [
                {
                    "id": item.get("id"),
                    "status": item.get("status"),
                    "review_state": item.get("review_state"),
                    "requires_review": item.get("requires_review", False),
                    "excluded_claim_ids": item.get("excluded_claim_ids", []),
                }
                for item in _mapping_list(latest.get("hypotheses"))
            ]
        proposal_period = _as_string(None if proposal is None else proposal.get("period_id"))
        if proposal is not None and (
            latest_review_period is None
            or (proposal_period is not None and proposal_period > latest_review_period)
        ):
            latest_statuses = [
                {
                    "id": item.get("id"),
                    "status": item.get("status"),
                    "review_state": item.get("review_state"),
                    "requires_review": item.get("requires_review", False),
                    "excluded_claim_ids": item.get("excluded_claim_ids", []),
                }
                for item in _mapping_list(proposal.get("hypotheses"))
            ]
        return {
            "schema_version": "career-radar.research.products.v1",
            "weekly": None if weekly is None else {"path": weekly_name, "product": weekly},
            "monthly_proposal": (
                None if proposal is None else {"path": proposal_name, "product": proposal}
            ),
            "monthly_reviews": review_history,
            "latest_monthly_statuses": latest_statuses,
            "eligibility_check_status": _eligibility_check_status(current_claims, check_error),
            "eligibility_checked_at": checked_at if snapshot_provider is not None else None,
            "eligibility_check_error_type": check_error,
            "state_updated_at": state.get("state_updated_at"),
        }

    def pending_versions(
        self, *, snapshot_provider: SnapshotProvider
    ) -> tuple[dict[str, object], ...]:
        """Read current extracted versions that still need an explicit disposition.

        This projection does not create monitor state or directories. It returns
        safe source/version identifiers and hashes; callers can open the existing
        read-only version endpoint to inspect normalized text and locators.
        """
        snapshot = _normalize_snapshot(snapshot_provider())
        state = _load_state(self._state_path)
        dispositions = _mapping({} if state is None else state.get("version_dispositions"))
        pending: list[dict[str, object]] = []
        seen: set[str] = set()
        for document in _document_rows(snapshot):
            document_id = _as_string(document.get("id"))
            latest = _mapping(document.get("latest_version"))
            digest = _as_string(latest.get("normalized_text_sha256"))
            if document_id is None or digest is None or not _DIGEST_RE.fullmatch(digest):
                continue
            key = _version_key(document_id, digest)
            if key in seen or key in dispositions:
                continue
            seen.add(key)
            pending.append(
                {
                    "document_id": document_id,
                    "document_version_id": latest.get("id"),
                    "version_number": latest.get("version_number"),
                    "normalized_text_sha256": digest,
                    "title": latest.get("title"),
                    "source_key": document.get("source_key"),
                    "canonical_url": document.get("canonical_url"),
                    "source_content_is_untrusted_data": True,
                }
            )
        pending.sort(
            key=lambda item: (_as_string(item.get("source_key")) or "", str(item["document_id"]))
        )
        return tuple(pending)

    def run(
        self,
        *,
        collector: Collector,
        snapshot_provider: SnapshotProvider,
        now: datetime | None = None,
        bootstrap: bool = False,
    ) -> MonitorRunResult:
        """Run one bounded daily collection, update durable state and due products.

        The interprocess lock covers collection and state updates so two daily
        heartbeats cannot overlap. A collector exception is recorded as an
        operational failure; the read-only snapshot and due products still run
        when available.
        """
        current_time = _as_local(now or datetime.now(UTC))
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self._lock_path) as lock_acquired:
            if not lock_acquired:
                return MonitorRunResult(
                    status="already_running",
                    collection=None,
                    weekly_output=None,
                    monthly_proposal=None,
                    pending_versions=(),
                    notifications=(),
                    due_outputs=(),
                    state_updated_at=current_time.isoformat(),
                )
            state = _load_state(self._state_path)
            first_run = state is None
            if bootstrap and not first_run:
                raise ResearchMonitorError("bootstrap is only valid on the first monitor run")
            if state is None:
                state = _new_state(current_time)

            collection: dict[str, object] | None
            collector_error: str | None = None
            try:
                collection = _to_json_object(collector())
            except Exception as error:  # the monitor must still attempt its read-only products
                collector_error = type(error).__name__
                collection = {"status": "failed", "error_type": collector_error}

            notifications: list[dict[str, object]] = []
            self._record_collection_health(
                state, collection, collector_error, current_time, notifications
            )
            state["last_collected_at"] = current_time.isoformat()

            try:
                snapshot = _normalize_snapshot(snapshot_provider())
            except Exception as error:
                failure_name = type(error).__name__
                signature = _stable_digest({"kind": "snapshot_failure", "error_type": failure_name})
                if state.get("active_snapshot_failure") != signature:
                    notifications.append(
                        _notification(
                            "snapshot_failure",
                            {
                                "error_type": failure_name,
                                "action": "Restore local research read access.",
                            },
                            current_time,
                        )
                    )
                state["active_snapshot_failure"] = signature
                state["state_updated_at"] = current_time.isoformat()
                _append_notifications(state, notifications)
                _write_json_atomically(self._state_path, state)
                return MonitorRunResult(
                    status="snapshot_failed",
                    collection=collection,
                    weekly_output=None,
                    monthly_proposal=None,
                    pending_versions=tuple(_pending_versions(state)),
                    notifications=tuple(notifications),
                    due_outputs=(),
                    state_updated_at=current_time.isoformat(),
                )

            if state.get("active_snapshot_failure") is not None:
                notifications.append(
                    _notification(
                        "snapshot_recovered",
                        {"action": "Local research reads recovered."},
                        current_time,
                    )
                )
                state["active_snapshot_failure"] = None

            self._record_claim_changes(state, snapshot, first_run, current_time, notifications)
            pending = self._refresh_version_queue(state, snapshot, current_time, notifications)
            if bootstrap:
                state["weekly_pending_claim_ids"] = sorted(_current_claims(snapshot))

            weekly_output: str | None = None
            monthly_proposal: str | None = None
            due_outputs: list[str] = []
            output_failed = False
            weekly_due = _is_due(state.get("next_weekly_due_at"), current_time)
            monthly_due = _is_due(state.get("next_monthly_due_at"), current_time)
            if weekly_due or bootstrap:
                report = self._build_weekly_report(state, snapshot, current_time)
                if bootstrap:
                    report["trigger"] = "initial_bootstrap"
                    report["scheduled_for"] = current_time.date().isoformat()
                report_path = self._weekly_path(state, current_time, bootstrap=bootstrap)
                try:
                    _write_json_atomically(report_path, report)
                except OSError as error:
                    output_failed = True
                    notifications.append(
                        _notification(
                            "weekly_output_failure",
                            {
                                "error_type": type(error).__name__,
                                "action": "Restore local output directory write access.",
                            },
                            current_time,
                        )
                    )
                else:
                    weekly_output = str(report_path)
                    due_outputs.append("weekly")
                    emitted = set(_string_list(report.get("emitted_claim_ids")))
                    state["weekly_emitted_claim_ids"] = sorted(
                        set(_string_list(state.get("weekly_emitted_claim_ids"))) | emitted
                    )
                    state["weekly_pending_claim_ids"] = [
                        claim_id
                        for claim_id in _string_list(state.get("weekly_pending_claim_ids"))
                        if claim_id not in emitted
                    ]
                    state["last_weekly_success_at"] = current_time.isoformat()
                    if not bootstrap:
                        state["next_weekly_due_at"] = _next_weekly_due(current_time).isoformat()
                    state["latest_weekly_output"] = report_path.name
                    if report["item_count"]:
                        notifications.append(
                            _notification(
                                "weekly_report_ready",
                                {
                                    "path": weekly_output,
                                    "item_count": report["item_count"],
                                    "accepted_claim_ids": report["emitted_claim_ids"],
                                    "action": "Review the weekly source-grounded items.",
                                },
                                current_time,
                            )
                        )

            if monthly_due or bootstrap:
                proposal = self._build_monthly_proposal(state, snapshot, current_time)
                if bootstrap:
                    proposal["trigger"] = "initial_bootstrap"
                    proposal["period_id"] = current_time.strftime("%Y-%m")
                proposal_path = self._monthly_proposal_path(
                    state, current_time, bootstrap=bootstrap
                )
                try:
                    _write_json_atomically(proposal_path, proposal)
                except OSError as error:
                    output_failed = True
                    notifications.append(
                        _notification(
                            "monthly_output_failure",
                            {
                                "error_type": type(error).__name__,
                                "action": "Restore local output directory write access.",
                            },
                            current_time,
                        )
                    )
                else:
                    monthly_proposal = str(proposal_path)
                    due_outputs.append("monthly")
                    proposal_period = str(proposal["period_id"])
                    state["monthly_proposals"] = list(
                        dict.fromkeys(
                            [*_string_list(state.get("monthly_proposals")), proposal_period]
                        )
                    )
                    state["last_monthly_proposal_at"] = current_time.isoformat()
                    if not bootstrap:
                        state["next_monthly_due_at"] = _next_monthly_due(current_time).isoformat()
                    state["latest_monthly_proposal"] = proposal_path.name
                    notifications.append(
                        _notification(
                            "monthly_review_due",
                            {
                                "path": monthly_proposal,
                                "period_id": proposal["period_id"],
                                "action": (
                                    "Review all three hypotheses and publish explicit statuses."
                                ),
                            },
                            current_time,
                        )
                    )

            if state.get("baseline_initialized") is False:
                state["baseline_claim_ids"] = sorted(_current_claims(snapshot).keys())
                state["baseline_initialized"] = True
                state["current_claims"] = _current_claims(snapshot)
                state["current_claim_ids"] = sorted(_current_claims(snapshot).keys())

            state["state_updated_at"] = current_time.isoformat()
            _append_notifications(state, notifications)
            _write_json_atomically(self._state_path, state)
            return MonitorRunResult(
                status=(
                    "output_failed"
                    if output_failed
                    else "collection_failed"
                    if _collection_failure_details(collection, collector_error) is not None
                    else "succeeded"
                ),
                collection=collection,
                weekly_output=weekly_output,
                monthly_proposal=monthly_proposal,
                pending_versions=tuple(pending),
                notifications=tuple(notifications),
                due_outputs=tuple(due_outputs),
                state_updated_at=current_time.isoformat(),
            )

    def record_version_disposition(
        self,
        *,
        document_id: str,
        normalized_text_sha256: str,
        disposition: Disposition,
        reviewer: str,
        reason: str,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Save an explicit review/no-claim decision for one normalized-text version.

        The digest key makes equivalent raw versions share a single disposition;
        a changed normalized digest opens a separate pending queue entry.
        """
        current_time = _as_local(now or datetime.now(UTC))
        doc_id = _nonblank(document_id, "document_id")
        digest = normalized_text_sha256.lower()
        if not _DIGEST_RE.fullmatch(digest):
            raise ResearchMonitorError("normalized_text_sha256 must be a lowercase SHA-256 digest")
        if disposition not in {"reviewed", "no_claims"}:
            raise ResearchMonitorError("disposition must be 'reviewed' or 'no_claims'")
        reviewer_value = _nonblank(reviewer, "reviewer")
        reason_value = _nonblank(reason, "reason")
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self._lock_path) as lock_acquired:
            if not lock_acquired:
                raise ResearchMonitorBusyError("another research monitor operation is running")
            state = _load_state(self._state_path)
            if state is None:
                raise ResearchMonitorError("run the monitor before recording a version disposition")
            key = _version_key(doc_id, digest)
            queue = _mapping(state.get("version_queue"))
            entry = _mapping(queue.get(key))
            if not entry:
                raise ResearchMonitorError(
                    "version digest is not present in the monitor review queue"
                )
            if entry.get("latest") is not True:
                raise ResearchMonitorError(
                    "only the latest normalized document text can be reviewed"
                )
            dispositions = _mapping(state.get("version_dispositions"))
            previous = _mapping(dispositions.get(key))
            event: dict[str, object] = {
                "disposition": disposition,
                "reviewer": reviewer_value,
                "reason": reason_value,
                "recorded_at": current_time.isoformat(),
            }
            history = _mapping_list(previous.get("history"))
            if (
                previous.get("disposition") == disposition
                and previous.get("reviewer") == reviewer_value
                and previous.get("reason") == reason_value
            ):
                return previous
            history.append(event)
            record = event | {"history": history}
            dispositions[key] = record
            state["version_dispositions"] = dispositions
            state["state_updated_at"] = current_time.isoformat()
            _write_json_atomically(self._state_path, state)
            return record

    def publish_monthly_review(
        self,
        review: Mapping[str, object],
        *,
        snapshot_provider: SnapshotProvider,
        now: datetime | None = None,
    ) -> str:
        """Validate and publish an explicitly reviewed monthly synthesis.

        Every submitted claim id is checked against a fresh read-only snapshot,
        including the question it supports, before an allowed status is saved.
        """
        current_time = _as_local(now or datetime.now(UTC))
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self._lock_path) as lock_acquired:
            if not lock_acquired:
                raise ResearchMonitorBusyError("another research monitor operation is running")
            state = _load_state(self._state_path)
            if state is None:
                raise ResearchMonitorError("run the monitor before publishing a monthly review")
            period_id = _nonblank(review.get("period_id"), "period_id")
            if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", period_id):
                raise ResearchMonitorError("period_id must use YYYY-MM")
            if period_id not in _string_list(state.get("monthly_proposals")):
                raise ResearchMonitorError("period_id has no generated monthly proposal")
            reviewer = _nonblank(review.get("reviewer"), "reviewer")
            hypotheses = review.get("hypotheses")
            if not isinstance(hypotheses, Sequence) or isinstance(hypotheses, (str, bytes)):
                raise ResearchMonitorError("hypotheses must contain all three reviewed hypotheses")
            normalized = _normalize_review_hypotheses(hypotheses)
            snapshot = _normalize_snapshot(snapshot_provider())
            current_claims = _current_claims(snapshot)
            current_by_question: dict[str, set[str]] = defaultdict(set)
            for claim_id, claim in current_claims.items():
                question = _as_string(claim.get("question"))
                if question is not None:
                    current_by_question[question].add(claim_id)
            for hypothesis in normalized:
                question = _as_string(hypothesis.get("question")) or ""
                supplied = set(_string_list(hypothesis.get("accepted_claim_ids")))
                if not supplied <= current_by_question.get(question, set()):
                    raise ResearchMonitorError(
                        f"{hypothesis['id']} contains a claim that is not current accepted "
                        f"evidence for {question}"
                    )
                if hypothesis["status"] != "insufficient" and not supplied:
                    raise ResearchMonitorError(
                        f"{hypothesis['id']} needs current accepted claims for status "
                        f"{hypothesis['status']}"
                    )
            published_hypotheses: list[dict[str, object]] = []
            for hypothesis in normalized:
                evidence_claims = [
                    current_claims[claim_id]
                    for claim_id in _string_list(hypothesis.get("accepted_claim_ids"))
                ]
                published_hypotheses.append(
                    hypothesis
                    | {
                        "evidence": [
                            {
                                "claim_id": claim.get("id"),
                                "statement": claim.get("statement"),
                                "citation": _citation(claim),
                            }
                            for claim in evidence_claims
                        ]
                    }
                )
            published: dict[str, object] = {
                "schema_version": "career-radar.research.monthly-review.v1",
                "period_id": period_id,
                "review_state": "reviewed",
                "reviewed_at": current_time.isoformat(),
                "reviewer": reviewer,
                "hypotheses": published_hypotheses,
                "current_evidence_claim_ids_at_publication": sorted(current_claims),
                "interpretation_note": (
                    "Statuses are explicit reviewer judgments. They are not inferred from claim "
                    "counts, publisher brands, or automatic summaries; prior monthly reviews "
                    "remain historical."
                ),
            }
            fingerprint = _stable_digest(
                {
                    "period_id": period_id,
                    "reviewer": reviewer,
                    "hypotheses": normalized,
                }
            )
            artifact_map = _mapping(state.get("monthly_review_artifacts"))
            history = _mapping_list(artifact_map.get(period_id))
            previous = next(
                (entry for entry in history if entry.get("fingerprint") == fingerprint), None
            )
            if previous is not None:
                previous_path = _as_string(previous.get("path"))
                if previous_path is not None:
                    return str(self._output_dir / previous_path)
            revision = len(history) + 1
            output_name = f"monthly-review-{period_id}-r{revision:02d}.json"
            output_path = self._output_dir / output_name
            while output_path.exists():
                existing = _read_product(self._output_dir, output_name)
                if existing is not None and existing.get("review_fingerprint") == fingerprint:
                    history.append({"path": output_name, "fingerprint": fingerprint})
                    artifact_map[period_id] = history
                    state["monthly_review_artifacts"] = artifact_map
                    state["monthly_reviews"] = list(
                        dict.fromkeys([*_string_list(state.get("monthly_reviews")), period_id])
                    )
                    state["state_updated_at"] = current_time.isoformat()
                    _write_json_atomically(self._state_path, state)
                    return str(output_path)
                revision += 1
                output_name = f"monthly-review-{period_id}-r{revision:02d}.json"
                output_path = self._output_dir / output_name
            published["review_revision"] = revision
            published["supersedes"] = history[-1].get("path") if history else None
            published["review_fingerprint"] = fingerprint
            _write_json_atomically(output_path, published)
            history.append({"path": output_name, "fingerprint": fingerprint})
            artifact_map[period_id] = history
            state["monthly_review_artifacts"] = artifact_map
            state["monthly_reviews"] = list(
                dict.fromkeys([*_string_list(state.get("monthly_reviews")), period_id])
            )
            state["state_updated_at"] = current_time.isoformat()
            _write_json_atomically(self._state_path, state)
            return str(output_path)

    def _record_collection_health(
        self,
        state: dict[str, object],
        collection: dict[str, object] | None,
        collector_error: str | None,
        now: datetime,
        notifications: list[dict[str, object]],
    ) -> None:
        details = _collection_failure_details(collection, collector_error)
        active = _as_string(state.get("active_collection_failure"))
        if details is None:
            if active is not None:
                notifications.append(
                    _notification(
                        "collection_recovered",
                        {"action": "Bounded daily collection succeeded."},
                        now,
                    )
                )
            state["active_collection_failure"] = None
            return
        signature = _stable_digest(details)
        if active != signature:
            notifications.append(_notification("collection_failure", details, now))
        state["active_collection_failure"] = signature

    def _record_claim_changes(
        self,
        state: dict[str, object],
        snapshot: Mapping[str, object],
        first_run: bool,
        now: datetime,
        notifications: list[dict[str, object]],
    ) -> None:
        claims = _all_claims(snapshot)
        current = {
            claim_id: claim
            for claim_id, claim in claims.items()
            if claim.get("current_evidence") is True
        }
        previous = _mapping(state.get("current_claims"))
        initialized = state.get("baseline_initialized") is True
        if initialized and not first_run:
            old_ids = set(previous)
            new_ids = set(current) - old_ids
            removed_ids = old_ids - set(current)
            baseline = set(_string_list(state.get("baseline_claim_ids")))
            pending_weekly = set(_string_list(state.get("weekly_pending_claim_ids")))
            for claim_id in new_ids:
                claim = current[claim_id]
                event_type = (
                    "new_current_claim" if claim_id not in baseline else "evidence_current_again"
                )
                notifications.append(
                    _notification(
                        event_type,
                        {
                            "claim_id": claim_id,
                            "question": claim.get("question"),
                            "statement": claim.get("statement"),
                            "document_version_id": claim.get("document_version_id"),
                            "action": "Review the newly current, explicitly accepted evidence.",
                        },
                        now,
                    )
                )
                pending_weekly.add(claim_id)
            for claim_id in removed_ids:
                claim = claims.get(claim_id) or _mapping(previous.get(claim_id))
                notifications.append(
                    _notification(
                        "claim_no_longer_current",
                        {
                            "claim_id": claim_id,
                            "question": claim.get("question"),
                            "statement": claim.get("statement"),
                            "review_state": claim.get("review_state"),
                            "reason": claim.get("excluded_reason")
                            or "latest text or review state changed",
                            "action": "Reassess reports that cited this historical claim.",
                        },
                        now,
                    )
                )
                pending_weekly.discard(claim_id)
            state["weekly_pending_claim_ids"] = sorted(pending_weekly)
        elif not initialized:
            state["baseline_claim_ids"] = sorted(current)
            state["weekly_pending_claim_ids"] = []
            state["baseline_initialized"] = True
        state["current_claims"] = current
        state["current_claim_ids"] = sorted(current)

    def _refresh_version_queue(
        self,
        state: dict[str, object],
        snapshot: Mapping[str, object],
        now: datetime,
        notifications: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        queue = _mapping(state.get("version_queue"))
        dispositions = _mapping(state.get("version_dispositions"))
        latest_by_document: dict[str, str] = {}
        current_queue_keys: set[str] = set()
        new_pending: list[dict[str, object]] = []
        for document in _document_rows(snapshot):
            document_id = _as_string(document.get("id"))
            latest = _mapping(document.get("latest_version"))
            digest = _as_string(latest.get("normalized_text_sha256"))
            if document_id is None or digest is None or not _DIGEST_RE.fullmatch(digest):
                continue
            key = _version_key(document_id, digest)
            latest_by_document[document_id] = key
            current_queue_keys.add(key)
            existing = _mapping(queue.get(key))
            item: dict[str, object] = {
                "document_id": document_id,
                "document_version_id": latest.get("id"),
                "version_number": latest.get("version_number"),
                "normalized_text_sha256": digest,
                "title": latest.get("title"),
                "source_key": document.get("source_key"),
                "canonical_url": document.get("canonical_url"),
                "first_seen_at": existing.get("first_seen_at") or now.isoformat(),
                "last_seen_at": now.isoformat(),
                "latest": True,
                "source_content_is_untrusted_data": True,
            }
            if key not in dispositions:
                item["status"] = "pending_review"
                if not existing:
                    new_pending.append(item)
            else:
                item["status"] = "disposed"
                item["disposition"] = _mapping(dispositions.get(key)).get("disposition")
            queue[key] = item

        for key, raw_entry in list(queue.items()):
            entry = _mapping(raw_entry)
            document_id = _as_string(entry.get("document_id"))
            is_latest = document_id is not None and latest_by_document.get(document_id) == key
            if entry.get("latest") is True and not is_latest:
                entry["latest"] = False
                if entry.get("status") == "pending_review":
                    entry["status"] = "superseded_by_new_text"
                    entry["superseded_at"] = now.isoformat()
                queue[key] = entry

        previous_pending = set(_string_list(state.get("pending_version_keys")))
        pending_keys = {
            key
            for key in current_queue_keys
            if key not in dispositions
            and _mapping(queue.get(key)).get("status") == "pending_review"
        }
        newly_pending_keys = sorted(pending_keys - previous_pending)
        if newly_pending_keys:
            notifications.append(
                _notification(
                    "version_review_queue_changed",
                    {
                        "new_pending_count": len(newly_pending_keys),
                        "version_keys": newly_pending_keys[:100],
                        "action": (
                            "Review each new normalized document version or record a "
                            "no-claim disposition."
                        ),
                    },
                    now,
                )
            )
        state["version_queue"] = queue
        state["pending_version_keys"] = sorted(pending_keys)
        state["latest_version_keys_by_document"] = latest_by_document
        return _pending_versions(state)

    def _build_weekly_report(
        self,
        state: Mapping[str, object],
        snapshot: Mapping[str, object],
        now: datetime,
    ) -> dict[str, object]:
        claims = _current_claims(snapshot)
        pending_ids = set(_string_list(state.get("weekly_pending_claim_ids"))) & set(claims)
        grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
        for claim_id in pending_ids:
            claim = claims[claim_id]
            question = _as_string(claim.get("question")) or "unknown"
            statement = _as_string(claim.get("statement")) or ""
            key = (question, " ".join(statement.casefold().split()))
            grouped[key].append(claim | {"id": claim_id})

        sorted_groups = sorted(
            grouped.values(),
            key=lambda group: min(
                _parse_datetime(_as_string(claim.get("created_at"))) or now for claim in group
            ),
        )
        selected_groups = sorted_groups[:5]
        items: list[dict[str, object]] = []
        emitted_ids: set[str] = set()
        for group in selected_groups:
            group.sort(key=lambda claim: _as_string(claim.get("id")) or "")
            ids = [_as_string(claim.get("id")) or "" for claim in group]
            emitted_ids.update(ids)
            first = group[0]
            citations = [_citation(claim) for claim in group]
            limitations = [
                value
                for claim in group
                if (value := _as_string(_mapping(claim.get("evidence_details")).get("limitations")))
            ]
            items.append(
                {
                    "plain_language_summary": first.get("statement"),
                    "affected_question": first.get("question"),
                    "accepted_claim_ids": ids,
                    "evidence_kinds": sorted(
                        {
                            kind
                            for claim in group
                            if (kind := _as_string(claim.get("evidence_kind")))
                        }
                    ),
                    "key_limitations": list(dict.fromkeys(limitations)),
                    "citations": citations,
                }
            )

        next_due = _as_local(_parse_datetime(_as_string(state.get("next_weekly_due_at"))) or now)
        last_success = _parse_datetime(_as_string(state.get("last_weekly_success_at")))
        start = last_success or _parse_datetime(_as_string(state.get("started_at"))) or next_due
        return {
            "schema_version": "career-radar.research.weekly.v1",
            "report_kind": "historical_snapshot",
            "scheduled_for": next_due.date().isoformat(),
            "generated_at": now.isoformat(),
            "window": {
                "start": start.isoformat(),
                "end": now.isoformat(),
                "coalesced_missed_due_dates": True,
            },
            "eligibility_checked_at": now.isoformat(),
            "items": items,
            "item_count": len(items),
            "target_range": [3, 5],
            "below_target": len(items) < 3,
            "queued_item_count_after_report": max(0, len(grouped) - len(selected_groups)),
            "emitted_claim_ids": sorted(emitted_ids),
            "interpretation_note": (
                "Items include only currently eligible, explicitly accepted structured claims. "
                "Each citation names its exact document version and locator. This is a historical "
                "snapshot as of eligibility_checked_at; later revisions, supersessions, or "
                "retractions are reported as current eligibility changes and do not rewrite it. "
                "Fewer than three items are reported without padding."
            ),
        }

    def _build_monthly_proposal(
        self,
        state: Mapping[str, object],
        snapshot: Mapping[str, object],
        now: datetime,
    ) -> dict[str, object]:
        current = _current_claims(snapshot)
        by_question: dict[str, list[dict[str, object]]] = defaultdict(list)
        for claim_id, claim in current.items():
            question = _as_string(claim.get("question"))
            if question is not None:
                by_question[question].append(claim | {"id": claim_id})
        due = _as_local(_parse_datetime(_as_string(state.get("next_monthly_due_at"))) or now)
        last = _parse_datetime(_as_string(state.get("last_monthly_proposal_at")))
        start = last or _parse_datetime(_as_string(state.get("started_at"))) or due
        period_id = due.strftime("%Y-%m")
        hypotheses: list[dict[str, object]] = []
        for definition in _HYPOTHESES:
            question = _as_string(definition.get("question")) or ""
            evidence = sorted(
                by_question.get(question, []),
                key=lambda claim: _as_string(claim.get("id")) or "",
            )
            hypotheses.append(
                {
                    "id": definition["id"],
                    "question": question,
                    "hypothesis": definition["statement"],
                    "status": "unresolved",
                    "review_state": "pending_explicit_review",
                    "accepted_claim_ids": [_as_string(claim.get("id")) for claim in evidence],
                    "evidence": [
                        {
                            "claim_id": claim.get("id"),
                            "statement": claim.get("statement"),
                            "citation": _citation(claim),
                        }
                        for claim in evidence
                    ],
                    "alternative_explanations": [],
                    "limitations": list(
                        dict.fromkeys(
                            limit
                            for claim in evidence
                            if (
                                limit := _as_string(
                                    _mapping(claim.get("evidence_details")).get("limitations")
                                )
                            )
                        )
                    ),
                    "review_prompt": (
                        "Add evidence-linked rationale, at least one plausible alternative "
                        "explanation, and limitations. Then select supported, mixed, contradicted, "
                        "or insufficient explicitly."
                    ),
                }
            )
        return {
            "schema_version": "career-radar.research.monthly-proposal.v1",
            "period_id": period_id,
            "review_state": "unresolved",
            "generated_at": now.isoformat(),
            "window": {
                "start": start.isoformat(),
                "end": now.isoformat(),
                "coalesced_missed_due_dates": True,
            },
            "eligibility_checked_at": now.isoformat(),
            "hypotheses": hypotheses,
            "interpretation_note": (
                "This file is a review proposal, not a reviewed monthly conclusion. All three "
                "statuses remain unresolved until a reviewer publishes a complete review. "
                "No status "
                "is derived from counts or publisher identity. The evidence list contains only "
                "currently eligible accepted claims and cites exact versions."
            ),
        }

    def _weekly_path(
        self, state: Mapping[str, object], now: datetime, *, bootstrap: bool = False
    ) -> Path:
        if bootstrap:
            return self._output_dir / f"weekly-bootstrap-{now.date().isoformat()}.json"
        due = _as_local(_parse_datetime(_as_string(state.get("next_weekly_due_at"))) or now)
        due_year, due_week, _ = due.isocalendar()
        current_year, current_week, _ = now.isocalendar()
        if (due_year, due_week) == (current_year, current_week):
            name = f"weekly-{due_year}-W{due_week:02d}.json"
        else:
            name = (
                f"weekly-recovery-{due_year}-W{due_week:02d}-through-"
                f"{current_year}-W{current_week:02d}.json"
            )
        return self._output_dir / name

    def _monthly_proposal_path(
        self, state: Mapping[str, object], now: datetime, *, bootstrap: bool = False
    ) -> Path:
        if bootstrap:
            return self._output_dir / f"monthly-proposal-bootstrap-{now.strftime('%Y-%m')}.json"
        due = _as_local(_parse_datetime(_as_string(state.get("next_monthly_due_at"))) or now)
        due_period = due.strftime("%Y-%m")
        current_period = now.strftime("%Y-%m")
        if due_period == current_period:
            name = f"monthly-proposal-{due_period}.json"
        else:
            name = f"monthly-proposal-recovery-{due_period}-through-{current_period}.json"
        return self._output_dir / name


def _normalize_review_hypotheses(value: Sequence[object]) -> list[dict[str, object]]:
    by_id: dict[str, dict[str, object]] = {}
    for raw in value:
        item = _mapping(raw)
        hypothesis_id = _nonblank(item.get("id"), "hypothesis id")
        if hypothesis_id in by_id:
            raise ResearchMonitorError(f"duplicate monthly hypothesis: {hypothesis_id}")
        definition = next(
            (candidate for candidate in _HYPOTHESES if candidate["id"] == hypothesis_id), None
        )
        if definition is None:
            raise ResearchMonitorError(f"unknown monthly hypothesis: {hypothesis_id}")
        status = _nonblank(item.get("status"), f"{hypothesis_id} status")
        if status not in _ALLOWED_HYPOTHESIS_STATUSES:
            raise ResearchMonitorError(f"{hypothesis_id} status is not an allowed reviewed outcome")
        alternatives = _nonblank_list(
            item.get("alternative_explanations"), f"{hypothesis_id} alternative_explanations"
        )
        limitations = _nonblank_list(item.get("limitations"), f"{hypothesis_id} limitations")
        claim_ids = sorted(set(_string_list(item.get("accepted_claim_ids"))))
        rationale = _nonblank(item.get("rationale"), f"{hypothesis_id} rationale")
        by_id[hypothesis_id] = {
            "id": hypothesis_id,
            "question": definition["question"],
            "hypothesis": definition["statement"],
            "status": status,
            "rationale": rationale,
            "accepted_claim_ids": claim_ids,
            "alternative_explanations": alternatives,
            "limitations": limitations,
        }
    expected = {str(item["id"]) for item in _HYPOTHESES}
    if set(by_id) != expected:
        missing = sorted(expected - set(by_id))
        raise ResearchMonitorError(
            f"monthly review must explicitly cover all three hypotheses; missing {missing}"
        )
    return [by_id[str(item["id"])] for item in _HYPOTHESES]


def _citation(claim: Mapping[str, object]) -> dict[str, object]:
    version = _mapping(claim.get("version"))
    document = _mapping(claim.get("document"))
    details = _mapping(claim.get("evidence_details"))
    return {
        "claim_id": claim.get("id"),
        "document_version_id": claim.get("document_version_id"),
        "document_id": document.get("id"),
        "source_key": document.get("source_key"),
        "title": version.get("title"),
        "canonical_url": document.get("canonical_url"),
        "discovered_url": version.get("discovered_url"),
        "publication_date": version.get("publication_date"),
        "data_period": details.get("data_period"),
        "retrieved_at": version.get("retrieved_at"),
        "media_type": version.get("media_type"),
        "raw_sha256": version.get("raw_sha256"),
        "normalized_text_sha256": version.get("normalized_text_sha256"),
        "extraction_version": version.get("extraction_version"),
        "evidence_kind": claim.get("evidence_kind"),
        "lineage_kind": claim.get("lineage_kind"),
        "upstream_dataset_or_document": claim.get("upstream_dataset_or_document"),
        "exact_quote": claim.get("exact_quote"),
        "location": claim.get("location"),
        "evidence_details": details,
    }


def _normalize_snapshot(snapshot: Mapping[str, object]) -> dict[str, object]:
    claims = snapshot.get("claims")
    documents = snapshot.get("documents")
    if not isinstance(claims, Sequence) or isinstance(claims, (str, bytes)):
        raise ResearchMonitorError("snapshot claims must be a sequence of API claim payloads")
    if not isinstance(documents, Sequence) or isinstance(documents, (str, bytes)):
        raise ResearchMonitorError("snapshot documents must be a sequence of API document payloads")
    return {
        "claims": [_mapping(item) for item in claims],
        "documents": [_mapping(item) for item in documents],
    }


def _all_claims(snapshot: Mapping[str, object]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    raw = snapshot.get("claims", ())
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return result
    for item in raw:
        claim = _mapping(item)
        claim_id = _as_string(claim.get("id"))
        if claim_id:
            result[claim_id] = claim
    return result


def _current_claims(snapshot: Mapping[str, object]) -> dict[str, dict[str, object]]:
    return {
        claim_id: claim
        for claim_id, claim in _all_claims(snapshot).items()
        if claim.get("current_evidence") is True and claim.get("review_state") == "accepted"
    }


def _eligibility_check_status(
    current_claims: Mapping[str, object] | None, error: str | None
) -> str:
    if current_claims is not None:
        return "checked"
    return "unavailable" if error is not None else "not_checked"


def _project_weekly_product(
    product: dict[str, object] | None,
    current_claims: Mapping[str, Mapping[str, object]] | None,
    check_error: str | None,
    checked_at: str,
) -> dict[str, object] | None:
    if product is None:
        return None
    projected = dict(product)
    projected["items"] = []
    excluded_ids: set[str] = set()
    unknown_ids: set[str] = set()
    for item in _mapping_list(product.get("items")):
        status = _project_evidence_item(item, current_claims)
        excluded_ids.update(_string_list(item.get("excluded_claim_ids")))
        unknown_ids.update(_string_list(item.get("unverified_claim_ids")))
        projected_items = projected["items"]
        if isinstance(projected_items, list):
            projected_items.append(item)
        if status == "historical_only":
            item["current_eligibility"] = "historical_only"
        elif status == "partially_current":
            item["current_eligibility"] = "partially_current"
        elif status == "current":
            item["current_eligibility"] = "current"
        else:
            item["current_eligibility"] = "unverified"
    projected["eligibility_checked_at"] = checked_at if current_claims is not None else None
    projected["eligibility_check_status"] = _eligibility_check_status(current_claims, check_error)
    projected["excluded_claim_ids"] = sorted(excluded_ids)
    projected["unverified_claim_ids"] = sorted(unknown_ids)
    projected["requires_review"] = bool(excluded_ids or unknown_ids)
    projected["current_eligibility_note"] = (
        "This remains a historical weekly snapshot. Current eligibility is projected per item; "
        "excluded citations are not current evidence."
        if current_claims is not None
        else (
            "Current citation eligibility was not verified; do not present these items "
            "as current evidence."
        )
    )
    return projected


def _project_monthly_product(
    product: dict[str, object] | None,
    current_claims: Mapping[str, Mapping[str, object]] | None,
    check_error: str | None,
    checked_at: str,
) -> dict[str, object] | None:
    if product is None:
        return None
    projected = dict(product)
    original_review_state = product.get("review_state")
    hypotheses: list[dict[str, object]] = []
    excluded_ids: set[str] = set()
    unverified_ids: set[str] = set()
    for raw_item in _mapping_list(product.get("hypotheses")):
        item = dict(raw_item)
        _project_evidence_item(item, current_claims)
        excluded_ids.update(_string_list(item.get("excluded_claim_ids")))
        unverified_ids.update(_string_list(item.get("unverified_claim_ids")))
        stored_status = _as_string(item.get("published_status")) or _as_string(item.get("status"))
        if stored_status is not None:
            item["published_status"] = stored_status
        valid = current_claims is not None and not item.get("excluded_claim_ids")
        if original_review_state == "reviewed":
            if valid:
                item["status"] = stored_status or "unresolved"
                item["review_state"] = "reviewed"
                item["requires_review"] = False
            else:
                item["status"] = "unresolved"
                item["review_state"] = "review_required"
                item["requires_review"] = True
        else:
            item["requires_review"] = True
        hypotheses.append(item)
    projected["hypotheses"] = hypotheses
    projected["published_review_state"] = original_review_state
    projected["eligibility_checked_at"] = checked_at if current_claims is not None else None
    projected["eligibility_check_status"] = _eligibility_check_status(current_claims, check_error)
    projected["excluded_claim_ids"] = sorted(excluded_ids)
    projected["unverified_claim_ids"] = sorted(unverified_ids)
    if original_review_state == "reviewed" and (
        current_claims is None or excluded_ids or unverified_ids
    ):
        projected["review_state"] = "review_required"
    projected["requires_review"] = bool(
        original_review_state != "reviewed"
        or current_claims is None
        or excluded_ids
        or unverified_ids
    )
    projected["current_eligibility_note"] = (
        "This is a current projection of a preserved monthly review. Any excluded citation makes "
        "that hypothesis unresolved until reviewed against current evidence."
        if current_claims is not None
        else (
            "Current citation eligibility was not verified; reviewed statuses are unresolved "
            "in this response."
        )
    )
    return projected


def _project_evidence_item(
    item: dict[str, object],
    current_claims: Mapping[str, Mapping[str, object]] | None,
) -> str:
    original_ids = sorted(set(_string_list(item.get("accepted_claim_ids"))))
    question = _as_string(item.get("affected_question")) or _as_string(item.get("question"))
    eligible_ids: list[str] = []
    excluded_ids: list[str] = []
    unverified_ids: list[str] = []
    for claim_id in original_ids:
        if current_claims is None:
            unverified_ids.append(claim_id)
            continue
        current = current_claims.get(claim_id)
        if current is not None and (
            question is None or _as_string(current.get("question")) == question
        ):
            eligible_ids.append(claim_id)
        else:
            excluded_ids.append(claim_id)
    citations: list[dict[str, object]] = []
    for citation in _mapping_list(item.get("citations")):
        citation = dict(citation)
        citation_claim_id = _as_string(citation.get("claim_id"))
        if citation_claim_id is not None and current_claims is not None:
            citation["currently_eligible"] = citation_claim_id in eligible_ids
            citation["eligibility_state"] = (
                "eligible" if citation_claim_id in eligible_ids else "excluded"
            )
        elif citation_claim_id is not None:
            citation["currently_eligible"] = None
            citation["eligibility_state"] = "unverified"
        citations.append(citation)
    if "citations" in item:
        item["historical_citations"] = citations
        item["citations"] = [
            citation for citation in citations if citation.get("currently_eligible") is True
        ]
    evidence: list[dict[str, object]] = []
    for row in _mapping_list(item.get("evidence")):
        row = dict(row)
        evidence_claim_id = _as_string(row.get("claim_id"))
        if evidence_claim_id is not None and current_claims is not None:
            row["currently_eligible"] = evidence_claim_id in eligible_ids
            row["eligibility_state"] = (
                "eligible" if evidence_claim_id in eligible_ids else "excluded"
            )
        elif evidence_claim_id is not None:
            row["currently_eligible"] = None
            row["eligibility_state"] = "unverified"
        citation = _mapping(row.get("citation"))
        if citation and evidence_claim_id is not None:
            citation["currently_eligible"] = (
                evidence_claim_id in eligible_ids if current_claims is not None else None
            )
            citation["eligibility_state"] = (
                "eligible"
                if evidence_claim_id in eligible_ids
                else "excluded"
                if current_claims is not None
                else "unverified"
            )
            row["citation"] = citation
        evidence.append(row)
    if "evidence" in item:
        item["historical_evidence"] = evidence
        item["evidence"] = [row for row in evidence if row.get("currently_eligible") is True]
    item["historical_accepted_claim_ids"] = original_ids
    item["accepted_claim_ids"] = eligible_ids
    item["current_accepted_claim_ids"] = eligible_ids
    item["excluded_claim_ids"] = excluded_ids
    item["unverified_claim_ids"] = unverified_ids
    if current_claims is None:
        return "unverified"
    if excluded_ids and eligible_ids:
        return "partially_current"
    if excluded_ids:
        return "historical_only"
    return "current"


def _document_rows(snapshot: Mapping[str, object]) -> list[dict[str, object]]:
    raw = snapshot.get("documents", ())
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [_mapping(item) for item in raw]


def _refresh_pending_versions(state: Mapping[str, object]) -> list[dict[str, object]]:
    queue = _mapping(state.get("version_queue"))
    keys = set(_string_list(state.get("pending_version_keys")))
    return [
        _mapping(queue.get(key))
        for key in sorted(keys)
        if _mapping(queue.get(key)).get("status") == "pending_review"
    ]


def _pending_versions(state: Mapping[str, object]) -> list[dict[str, object]]:
    return _refresh_pending_versions(state)


def _new_state(now: datetime) -> dict[str, object]:
    return {
        "schema_version": _STATE_SCHEMA_VERSION,
        "started_at": now.isoformat(),
        "state_updated_at": now.isoformat(),
        "baseline_initialized": False,
        "baseline_claim_ids": [],
        "current_claim_ids": [],
        "current_claims": {},
        "weekly_pending_claim_ids": [],
        "weekly_emitted_claim_ids": [],
        "next_weekly_due_at": _first_weekly_due(now).isoformat(),
        "next_monthly_due_at": _first_monthly_due(now).isoformat(),
        "last_weekly_success_at": None,
        "last_monthly_proposal_at": None,
        "last_collected_at": None,
        "active_collection_failure": None,
        "active_snapshot_failure": None,
        "version_queue": {},
        "pending_version_keys": [],
        "latest_version_keys_by_document": {},
        "version_dispositions": {},
        "monthly_proposals": [],
        "monthly_reviews": [],
        "monthly_review_artifacts": {},
        "latest_weekly_output": None,
        "latest_monthly_proposal": None,
        "notifications": [],
        "notification_ids": [],
    }


def _load_state(path: Path) -> dict[str, object] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ResearchMonitorError(
            "monitor state is invalid JSON; preserved without overwrite"
        ) from error
    state = _mapping(decoded)
    if state.get("schema_version") != _STATE_SCHEMA_VERSION:
        raise ResearchMonitorError(
            "monitor state schema version is unsupported; preserved without overwrite"
        )
    return state


def _read_product(output_dir: Path, name: str | None) -> dict[str, object] | None:
    if name is None or Path(name).name != name or name in {".", ".."}:
        return None
    try:
        value = json.loads((output_dir / name).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(value, Mapping):
        return None
    return _mapping(value)


def _write_json_atomically(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=_json_default)
        + "\n"
    )
    temporary_path: str | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary_path)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(descriptor, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            descriptor = -1
            yield False
            return
        yield True
    finally:
        if descriptor >= 0:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _append_notifications(
    state: dict[str, object], new_events: Sequence[dict[str, object]]
) -> None:
    history = _mapping_list(state.get("notifications"))
    seen = set(_string_list(state.get("notification_ids")))
    ids = _string_list(state.get("notification_ids"))
    for event in new_events:
        event_id = _as_string(event.get("id"))
        if event_id is None or event_id in seen:
            continue
        history.append(event)
        ids.append(event_id)
        seen.add(event_id)
    state["notifications"] = history[-_MAX_NOTIFICATION_HISTORY:]
    state["notification_ids"] = ids[-_MAX_NOTIFICATION_HISTORY:]


def _notification(kind: str, payload: Mapping[str, object], now: datetime) -> dict[str, object]:
    identity = {"kind": kind, "payload": payload}
    return {
        "id": _stable_digest(identity),
        "type": kind,
        "created_at": now.isoformat(),
        "payload": dict(payload),
    }


def _collection_failure_details(
    result: Mapping[str, object] | None, collector_error: str | None
) -> dict[str, object] | None:
    if collector_error is not None:
        return {
            "failure": True,
            "error_type": collector_error,
            "action": "Check local collector and database access.",
        }
    if result is None:
        return {
            "failure": True,
            "error_type": "missing_result",
            "action": "Check local collector wiring.",
        }
    status = _as_string(result.get("status")) or "unknown"
    source_failures = _stable_source_failures(result.get("source_failures"))
    failed = _as_integer(result.get("failed")) or 0
    if status == "succeeded" and failed == 0 and not source_failures:
        return None
    details: dict[str, object] = {
        "failure": True,
        "status": status,
        "action": "Inspect visible source-attempt outcomes and local network/database errors.",
    }
    if source_failures:
        details["source_failures"] = source_failures
    return details


def _stable_source_failures(value: object) -> list[dict[str, object]]:
    """Normalize stable per-source failure identities, excluding run noise."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    signatures: dict[str, dict[str, object]] = {}
    for raw_item in value:
        item = _mapping(raw_item)
        source_key = _as_string(item.get("source_key"))
        outcome = _as_string(item.get("outcome"))
        if source_key is None or not source_key.strip() or outcome is None or not outcome.strip():
            continue
        if outcome in {"discovered", "acquired", "unchanged"}:
            continue
        error_code = item.get("error_code")
        normalized_error = error_code.strip() if isinstance(error_code, str) else None
        signature: dict[str, object] = {
            "source_key": source_key.strip(),
            "outcome": outcome.strip(),
            "error_code": normalized_error or None,
            "http_status": _as_integer(item.get("http_status")),
        }
        identity = json.dumps(signature, sort_keys=True, separators=(",", ":"))
        signatures[identity] = signature
    return [signatures[key] for key in sorted(signatures)]


def _to_json_object(value: object) -> dict[str, object]:
    normalized = _to_json_value(value)
    if isinstance(normalized, dict):
        return normalized
    if normalized is None:
        return {"status": "unknown"}
    return {"result": normalized}


def _to_json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _to_json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_json_value(item) for item in value]
    return str(value)


def _json_default(value: object) -> object:
    normalized = _to_json_value(value)
    if normalized is value:
        return str(value)
    return normalized


def _stable_digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=_json_default
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _version_key(document_id: str, digest: str) -> str:
    return f"{document_id}:{digest}"


def _first_weekly_due(now: datetime) -> datetime:
    candidate = datetime.combine(now.date(), time(hour=9), _LOCAL_ZONE)
    days = (0 - now.weekday()) % 7
    candidate += timedelta(days=days)
    if candidate < now:
        candidate += timedelta(days=7)
    return candidate


def _next_weekly_due(now: datetime) -> datetime:
    candidate = datetime.combine(now.date(), time(hour=9), _LOCAL_ZONE)
    days = (0 - now.weekday()) % 7
    candidate += timedelta(days=days)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def _first_monthly_due(now: datetime) -> datetime:
    candidate = datetime.combine(now.date().replace(day=1), time(hour=9), _LOCAL_ZONE)
    if candidate < now:
        candidate = _next_month_start(candidate)
    return candidate


def _next_monthly_due(now: datetime) -> datetime:
    candidate = datetime.combine(now.date().replace(day=1), time(hour=9), _LOCAL_ZONE)
    if candidate <= now:
        candidate = _next_month_start(candidate)
    return candidate


def _next_month_start(value: datetime) -> datetime:
    if value.month == 12:
        return datetime(value.year + 1, 1, 1, 9, tzinfo=_LOCAL_ZONE)
    return datetime(value.year, value.month + 1, 1, 9, tzinfo=_LOCAL_ZONE)


def _is_due(value: object, now: datetime) -> bool:
    due = _parse_datetime(_as_string(value))
    return due is not None and _as_local(due) <= now


def _as_local(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ResearchMonitorError("monitor timestamps must include a timezone")
    return value.astimezone(_LOCAL_ZONE)


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _nonblank(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchMonitorError(f"{name} must be non-blank text")
    return value.strip()


def _nonblank_list(value: object, name: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ResearchMonitorError(f"{name} must be a non-empty list of text")
    result = [_nonblank(item, name) for item in value]
    if not result:
        raise ResearchMonitorError(f"{name} must be a non-empty list of text")
    return result


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _mapping_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [_mapping(item) for item in value]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, str)]


def _as_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
