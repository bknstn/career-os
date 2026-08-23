"""PostgreSQL-backed collection coordination and operational inspection."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from career_os.application.operations import (
    CollectionOperationalPolicy,
    SourceCollectionUnavailableError,
    SourceOperationalStatus,
)
from career_os.persistence.models import CollectionRun, JobPosting, Source

type SessionFactory = sessionmaker[Session]


def _lock_key(source_key: str) -> int:
    digest = hashlib.blake2b(f"career-os:collection:{source_key}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


@dataclass(slots=True)
class SourceCollectionCoordinator:
    """Hold one PostgreSQL advisory lock and enforce the persisted circuit."""

    session_factory: SessionFactory
    policy: CollectionOperationalPolicy

    @contextmanager
    def hold(self, source_key: str, *, now: datetime) -> Iterator[None]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        bootstrap = self.session_factory()
        bind = bootstrap.get_bind()
        bootstrap.close()
        if not isinstance(bind, Engine):
            raise TypeError("source collection coordination requires an Engine-bound factory")
        connection = bind.connect()
        key = _lock_key(source_key)
        acquired = False
        try:
            acquired = bool(
                connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            )
            if not acquired:
                raise SourceCollectionUnavailableError(
                    f"collection already running for source: {source_key}"
                )
            if self._circuit_open(connection, source_key=source_key, now=now):
                raise SourceCollectionUnavailableError(
                    f"collection circuit open for source: {source_key}"
                )
            connection.commit()
            yield
        finally:
            if acquired:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                connection.commit()
            else:
                connection.rollback()
            connection.close()

    def _circuit_open(self, connection: Connection, *, source_key: str, now: datetime) -> bool:
        rows = list(
            connection.execute(
                select(CollectionRun.status, CollectionRun.finished_at)
                .join(Source, CollectionRun.source_id == Source.id)
                .where(Source.key == source_key, CollectionRun.status != "running")
                .order_by(CollectionRun.started_at.desc())
                .limit(self.policy.circuit_failure_threshold)
            )
        )
        if len(rows) < self.policy.circuit_failure_threshold:
            return False
        if any(status == "succeeded" for status, _finished_at in rows):
            return False
        latest_finished = rows[0][1]
        return latest_finished is not None and latest_finished > (
            now.astimezone(UTC) - timedelta(seconds=self.policy.circuit_cooldown_seconds)
        )


@dataclass(slots=True)
class PersistedScanOutcomeSafeguard:
    """Reject implausible complete counts while retaining collected evidence."""

    session_factory: SessionFactory
    policy: CollectionOperationalPolicy

    def incomplete_reason(self, *, source_key: str, items_fetched: int) -> str | None:
        with self.session_factory() as session:
            previous_count = session.scalar(
                select(CollectionRun.items_fetched)
                .join(Source, CollectionRun.source_id == Source.id)
                .where(Source.key == source_key, CollectionRun.status == "succeeded")
                .order_by(CollectionRun.finished_at.desc())
                .limit(1)
            )
        if previous_count is None or previous_count == 0:
            return None
        if items_fetched == 0:
            return f"operational safeguard: complete scan returned zero after {previous_count} jobs"
        drop_ratio = (previous_count - items_fetched) / previous_count
        if drop_ratio > self.policy.max_fetch_drop_ratio:
            return (
                "operational safeguard: fetched count dropped "
                f"from {previous_count} to {items_fetched} ({drop_ratio:.3f})"
            )
        return None


@dataclass(slots=True)
class OperationalStatusRepository:
    """Derive freshness, circuit state, and bounded source metrics from stored facts."""

    session: Session
    policy: CollectionOperationalPolicy

    def list_source_statuses(self, *, now: datetime) -> tuple[SourceOperationalStatus, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return tuple(
            self._status(source, now=now.astimezone(UTC))
            for source in self.session.scalars(select(Source).order_by(Source.key))
        )

    def _status(self, source: Source, *, now: datetime) -> SourceOperationalStatus:
        latest = self.session.scalar(
            select(CollectionRun)
            .where(CollectionRun.source_id == source.id, CollectionRun.status != "running")
            .order_by(CollectionRun.started_at.desc())
            .limit(1)
        )
        last_success_at = self.session.scalar(
            select(CollectionRun.finished_at)
            .where(CollectionRun.source_id == source.id, CollectionRun.status == "succeeded")
            .order_by(CollectionRun.finished_at.desc())
            .limit(1)
        )
        recent_statuses = tuple(
            self.session.scalars(
                select(CollectionRun.status)
                .where(CollectionRun.source_id == source.id, CollectionRun.status != "running")
                .order_by(CollectionRun.started_at.desc())
                .limit(self.policy.circuit_failure_threshold)
            )
        )
        unsafe = 0
        for status in recent_statuses:
            if status == "succeeded":
                break
            unsafe += 1
        age = (
            None
            if last_success_at is None
            else max(0.0, (now - last_success_at.astimezone(UTC)).total_seconds())
        )
        active_jobs = int(
            self.session.scalar(
                select(func.count(JobPosting.id)).where(
                    JobPosting.source_id == source.id, JobPosting.status == "active"
                )
            )
            or 0
        )
        circuit_open = (
            unsafe >= self.policy.circuit_failure_threshold
            and latest is not None
            and latest.finished_at is not None
            and latest.finished_at > now - timedelta(seconds=self.policy.circuit_cooldown_seconds)
        )
        return SourceOperationalStatus(
            source_key=source.key,
            source_kind=source.kind,
            enabled=source.enabled,
            active_jobs=active_jobs,
            latest_status=None if latest is None else latest.status,
            latest_finished_at=None if latest is None else latest.finished_at,
            last_success_at=last_success_at,
            last_success_age_seconds=age,
            stale=source.enabled and (age is None or age > self.policy.freshness_target_seconds),
            consecutive_unsafe_runs=unsafe,
            circuit_open=circuit_open,
            latest_items_fetched=0 if latest is None else latest.items_fetched,
            latest_http_requests=0 if latest is None else latest.http_requests,
            latest_retry_count=0 if latest is None else latest.retry_count,
            latest_rate_limit_events=0 if latest is None else latest.rate_limit_events,
            latest_failed_items=0 if latest is None else latest.failed_items,
            latest_normalization_errors=0 if latest is None else latest.normalization_errors,
        )
