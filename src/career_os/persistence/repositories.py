"""SQLAlchemy repositories for collection state and canonical job versions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, cast
from uuid import UUID

from sqlalchemy import Select, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from career_os.application.collect import CollectionRun, CollectionRunStatus
from career_os.db import session_scope
from career_os.domain.canonical import CANONICAL_CONTENT_FIELDS, CanonicalJobCandidate, JsonValue
from career_os.persistence.models import (
    CollectionRun as CollectionRunModel,
)
from career_os.persistence.models import (
    JobPosting,
    JobVersion,
    Source,
)

type SessionFactory = sessionmaker[Session]

_PERSISTED_RUN_COUNT_FIELDS: Final = (
    "items_fetched",
    "inserted",
    "updated",
    "unchanged",
    "inactivated",
    "failed_items",
    "http_requests",
    "retry_count",
)


class PersistenceError(RuntimeError):
    """Base error for repository failures that should not be silently retried."""


class SourceNotFoundError(PersistenceError):
    """A candidate or run names a source that has not been configured."""


class SourceIdentityConflictError(PersistenceError):
    """A configured source conflicts with the candidate's immutable identity."""


class CollectionRunPersistenceConflictError(PersistenceError):
    """A run transition could not be applied from its required running state."""


class StaleObservationError(PersistenceError):
    """An observation is not strictly newer than stored source evidence."""


class CanonicalVersionConflictError(PersistenceError):
    """A changed current posting would duplicate an immutable content version."""


class LifecycleReconciliationConflictError(PersistenceError):
    """A lifecycle mutation does not match a persisted complete collection run."""


class JobUpsertAction(StrEnum):
    """The canonical-content outcome of a repository upsert."""

    INSERTED = "inserted"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class JobUpsertResult:
    """Stable result data for collection counters and diagnostics."""

    action: JobUpsertAction
    job_id: UUID
    content_hash: str
    changed_fields: tuple[str, ...]
    reactivated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.reactivated, bool):
            raise TypeError("reactivated must be a bool")
        if self.action is JobUpsertAction.INSERTED:
            if self.changed_fields != CANONICAL_CONTENT_FIELDS:
                raise ValueError("an inserted job must report all canonical content fields")
        elif self.action is JobUpsertAction.UNCHANGED:
            if self.changed_fields:
                raise ValueError("an unchanged job cannot report changed fields")
        elif self.action is JobUpsertAction.UPDATED:
            if not self.changed_fields:
                raise ValueError("an updated job must report changed fields")
        else:
            raise ValueError(f"unsupported job upsert action: {self.action!r}")


def _run_id_as_uuid(run_id: str) -> UUID:
    try:
        return UUID(run_id)
    except (TypeError, ValueError) as error:
        raise CollectionRunPersistenceConflictError(
            "collection run IDs must be UUID strings for PostgreSQL persistence"
        ) from error


def _source_for_identity(
    session: Session, *, source_key: str, source_kind: str, company_name: str | None = None
) -> Source:
    source = session.scalar(select(Source).where(Source.key == source_key))
    if source is None:
        raise SourceNotFoundError(f"configured source not found: {source_key}")
    if source.kind != source_kind:
        raise SourceIdentityConflictError(
            f"configured source kind does not match candidate: {source_key}"
        )
    if company_name is not None and source.company_name != company_name:
        raise SourceIdentityConflictError(
            f"configured source company does not match candidate: {source_key}"
        )
    return source


def _plain_json_value(value: JsonValue) -> object:
    """Copy immutable domain JSON values into ordinary JSON container types."""
    if isinstance(value, Mapping):
        return {key: _plain_json_value(nested_value) for key, nested_value in value.items()}
    if isinstance(value, tuple):
        return [_plain_json_value(nested_value) for nested_value in value]
    return value


def _plain_json_object(value: Mapping[str, JsonValue]) -> dict[str, object]:
    return {key: _plain_json_value(nested_value) for key, nested_value in value.items()}


def _timestamp_content_value(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _stored_canonical_content(job: JobPosting) -> dict[str, JsonValue]:
    """Represent model state with the same field values as a candidate hash input."""
    return {
        "company_name": job.company_name,
        "title_raw": job.title_raw,
        "title_normalized": job.title_normalized,
        "description_html": job.description_html,
        "description_text": job.description_text,
        "department_raw": job.department_raw,
        "team_raw": job.team_raw,
        "source_url": job.source_url,
        "apply_url": job.apply_url,
        "published_at": _timestamp_content_value(job.published_at),
        "normalizer_version": job.normalizer_version,
        "taxonomy_version": job.taxonomy_version,
    }


def _persisted_run_counts(run: CollectionRun) -> dict[str, int]:
    count_fields = run.counts.as_fields()
    return {field_name: count_fields[field_name] for field_name in _PERSISTED_RUN_COUNT_FIELDS}


def _changed_fields(job: JobPosting, candidate: CanonicalJobCandidate) -> tuple[str, ...]:
    stored_content = _stored_canonical_content(job)
    candidate_content = candidate.canonical_content()
    return tuple(
        field_name
        for field_name in CANONICAL_CONTENT_FIELDS
        if stored_content[field_name] != candidate_content[field_name]
    )


@dataclass(slots=True)
class SqlAlchemyCollectionRunStore:
    """Durably persist collection-run state through short owned transactions.

    The collection workflow performs network I/O between ``create`` and
    ``update``.  Each call therefore opens and commits its own transaction so a
    running state is durable before adapter work starts.  It maps only counters
    present in the current schema and rejects non-zero newer counters rather
    than silently dropping them.
    """

    session_factory: SessionFactory
    normalizer_version: str
    taxonomy_version: str

    def __post_init__(self) -> None:
        for field_name in ("normalizer_version", "taxonomy_version"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-blank string")
            setattr(self, field_name, value.strip())

    def create(self, run: CollectionRun) -> None:
        """Commit a running record before the adapter performs network I/O."""
        if run.status is not CollectionRunStatus.RUNNING:
            raise CollectionRunPersistenceConflictError("a collection run create must be running")
        self._assert_persistable_counts(run)
        try:
            with session_scope(self.session_factory) as session:
                source = _source_for_identity(
                    session, source_key=run.source_key, source_kind=run.source_kind
                )
                session.add(
                    CollectionRunModel(
                        id=_run_id_as_uuid(run.id),
                        source_id=source.id,
                        status=run.status.value,
                        started_at=run.started_at,
                        finished_at=run.finished_at,
                        **_persisted_run_counts(run),
                        normalizer_version=self.normalizer_version,
                        taxonomy_version=self.taxonomy_version,
                        error_summary=run.error_summary,
                    )
                )
                session.flush()
        except IntegrityError as error:
            raise CollectionRunPersistenceConflictError(
                "collection run already exists or violates a persistence constraint"
            ) from error

    def update(self, run: CollectionRun) -> None:
        """Commit exactly one running-to-terminal transition using a compare-and-set."""
        if run.status is CollectionRunStatus.RUNNING:
            raise CollectionRunPersistenceConflictError("a collection run update must be terminal")
        self._assert_persistable_counts(run)
        with session_scope(self.session_factory) as session:
            source = _source_for_identity(
                session, source_key=run.source_key, source_kind=run.source_kind
            )
            result = cast(
                CursorResult[object],
                session.execute(
                    update(CollectionRunModel)
                    .where(
                        CollectionRunModel.id == _run_id_as_uuid(run.id),
                        CollectionRunModel.source_id == source.id,
                        CollectionRunModel.status == CollectionRunStatus.RUNNING.value,
                    )
                    .values(
                        status=run.status.value,
                        finished_at=run.finished_at,
                        **_persisted_run_counts(run),
                        error_summary=run.error_summary,
                    )
                ),
            )
            if result.rowcount != 1:
                raise CollectionRunPersistenceConflictError(
                    "collection run was not in the expected running state"
                )

    @staticmethod
    def _assert_persistable_counts(run: CollectionRun) -> None:
        if run.counts.rate_limit_events or run.counts.normalization_errors:
            raise PersistenceError(
                "the initial collection_runs schema cannot persist rate-limit or "
                "normalization counts"
            )


@dataclass(slots=True)
class CanonicalJobRepository:
    """Upsert canonical jobs and immutable content versions in a caller transaction."""

    session: Session

    def upsert(self, candidate: CanonicalJobCandidate) -> JobUpsertResult:
        """Persist one candidate without committing the caller-owned transaction."""
        source = _source_for_identity(
            self.session,
            source_key=candidate.source_key,
            source_kind=candidate.source_kind,
            company_name=candidate.company_name,
        )
        job = self._locked_job(source_id=source.id, external_id=candidate.external_id)
        if job is None:
            return self._insert_or_resolve_race(source, candidate)
        return self._update_existing(job, candidate)

    def _locked_job(self, *, source_id: UUID, external_id: str) -> JobPosting | None:
        statement: Select[tuple[JobPosting]] = (
            select(JobPosting)
            .where(JobPosting.source_id == source_id, JobPosting.external_id == external_id)
            .with_for_update()
        )
        return self.session.scalar(statement)

    def _insert_or_resolve_race(
        self, source: Source, candidate: CanonicalJobCandidate
    ) -> JobUpsertResult:
        job: JobPosting | None = None
        try:
            with self.session.begin_nested():
                job = self._new_job(source, candidate)
                self.session.add(job)
                self.session.flush()
                self._add_version(
                    job,
                    candidate,
                    changed_fields=CANONICAL_CONTENT_FIELDS,
                )
                self.session.flush()
        except IntegrityError:
            job = self._locked_job(source_id=source.id, external_id=candidate.external_id)
            if job is None:
                raise
            return self._update_existing(job, candidate)
        assert job is not None
        return JobUpsertResult(
            action=JobUpsertAction.INSERTED,
            job_id=job.id,
            content_hash=candidate.content_hash,
            changed_fields=CANONICAL_CONTENT_FIELDS,
        )

    def _update_existing(
        self, job: JobPosting, candidate: CanonicalJobCandidate
    ) -> JobUpsertResult:
        reactivated = job.status == "inactive"
        if reactivated:
            if job.inactive_at is None:
                raise LifecycleReconciliationConflictError(
                    "an inactive job must retain the timestamp of its absence confirmation"
                )
            if candidate.observed_at <= job.inactive_at:
                raise StaleObservationError(
                    "candidate observation must be strictly newer than the absence confirmation"
                )
        elif candidate.observed_at <= job.last_seen_at:
            raise StaleObservationError(
                "candidate observation must be strictly newer than the latest stored observation"
            )
        if job.content_hash == candidate.content_hash:
            if reactivated:
                job.status = "active"
                job.inactive_at = None
            job.last_seen_at = candidate.observed_at
            job.raw_latest = _plain_json_object(candidate.raw_payload)
            self.session.flush()
            return JobUpsertResult(
                action=JobUpsertAction.UNCHANGED,
                job_id=job.id,
                content_hash=job.content_hash,
                changed_fields=(),
                reactivated=reactivated,
            )

        changed_fields = _changed_fields(job, candidate)
        if not changed_fields:
            raise PersistenceError(
                "candidate content hash differs without a canonical field difference"
            )
        prior_version = self.session.scalar(
            select(JobVersion.id).where(
                JobVersion.job_id == job.id,
                JobVersion.content_hash == candidate.content_hash,
            )
        )
        if prior_version is not None:
            raise CanonicalVersionConflictError(
                "candidate reverts to an already-recorded canonical content version"
            )
        if reactivated:
            job.status = "active"
            job.inactive_at = None
        self._apply_candidate_content(job, candidate)
        self._add_version(job, candidate, changed_fields=changed_fields)
        self.session.flush()
        return JobUpsertResult(
            action=JobUpsertAction.UPDATED,
            job_id=job.id,
            content_hash=candidate.content_hash,
            changed_fields=changed_fields,
            reactivated=reactivated,
        )

    @staticmethod
    def _new_job(source: Source, candidate: CanonicalJobCandidate) -> JobPosting:
        return JobPosting(
            source_id=source.id,
            external_id=candidate.external_id,
            company_name=candidate.company_name,
            title_raw=candidate.title_raw,
            title_normalized=candidate.title_normalized,
            role_family=None,
            seniority=None,
            seniority_confidence=None,
            description_html=candidate.description_html,
            description_text=candidate.description_text,
            department_raw=candidate.department_raw,
            team_raw=candidate.team_raw,
            employment_type=None,
            workplace_type=None,
            remote_scope=None,
            source_url=candidate.source_url,
            apply_url=candidate.apply_url,
            published_at=candidate.published_at,
            first_seen_at=candidate.observed_at,
            last_seen_at=candidate.observed_at,
            inactive_at=None,
            status="active",
            content_hash=candidate.content_hash,
            raw_latest=_plain_json_object(candidate.raw_payload),
            normalizer_version=candidate.normalizer_version,
            taxonomy_version=candidate.taxonomy_version,
        )

    @staticmethod
    def _apply_candidate_content(job: JobPosting, candidate: CanonicalJobCandidate) -> None:
        job.company_name = candidate.company_name
        job.title_raw = candidate.title_raw
        job.title_normalized = candidate.title_normalized
        job.description_html = candidate.description_html
        job.description_text = candidate.description_text
        job.department_raw = candidate.department_raw
        job.team_raw = candidate.team_raw
        job.source_url = candidate.source_url
        job.apply_url = candidate.apply_url
        job.published_at = candidate.published_at
        job.last_seen_at = candidate.observed_at
        job.content_hash = candidate.content_hash
        job.raw_latest = _plain_json_object(candidate.raw_payload)
        job.normalizer_version = candidate.normalizer_version
        job.taxonomy_version = candidate.taxonomy_version

    def _add_version(
        self,
        job: JobPosting,
        candidate: CanonicalJobCandidate,
        *,
        changed_fields: tuple[str, ...],
    ) -> None:
        self.session.add(
            JobVersion(
                job_id=job.id,
                observed_at=candidate.observed_at,
                content_hash=candidate.content_hash,
                canonical_snapshot=_plain_json_object(candidate.canonical_content()),
                raw_payload=_plain_json_object(candidate.raw_payload),
                changed_fields=list(changed_fields),
            )
        )


@dataclass(slots=True)
class SourceLifecycleRepository:
    """Atomically reconcile one persisted complete run in a caller transaction."""

    session: Session

    def inactivate_unseen(self, *, run: CollectionRun, seen_external_ids: tuple[str, ...]) -> int:
        """Mark source-local missing jobs inactive without committing the session."""
        if run.status is not CollectionRunStatus.SUCCEEDED or run.finished_at is None:
            raise LifecycleReconciliationConflictError(
                "lifecycle reconciliation requires a terminal succeeded collection run"
            )
        if any(
            not isinstance(external_id, str) or not external_id for external_id in seen_external_ids
        ):
            raise LifecycleReconciliationConflictError(
                "seen external IDs must be sorted, deduplicated, and non-blank"
            )
        if seen_external_ids != tuple(sorted(set(seen_external_ids))):
            raise LifecycleReconciliationConflictError(
                "seen external IDs must be sorted, deduplicated, and non-blank"
            )
        source = _source_for_identity(
            self.session,
            source_key=run.source_key,
            source_kind=run.source_kind,
        )
        persisted_run = self.session.scalar(
            select(CollectionRunModel)
            .where(
                CollectionRunModel.id == _run_id_as_uuid(run.id),
                CollectionRunModel.source_id == source.id,
            )
            .with_for_update()
        )
        if persisted_run is None:
            raise LifecycleReconciliationConflictError(
                "collection run is not persisted for this source"
            )
        if (
            persisted_run.status != CollectionRunStatus.SUCCEEDED.value
            or persisted_run.started_at != run.started_at
            or persisted_run.finished_at != run.finished_at
        ):
            raise LifecycleReconciliationConflictError(
                "persisted collection run is not the same completed source scan"
            )

        missing_condition = ~JobPosting.external_id.in_(seen_external_ids)
        result = cast(
            CursorResult[object],
            self.session.execute(
                update(JobPosting)
                .where(
                    JobPosting.source_id == source.id,
                    JobPosting.status == "active",
                    JobPosting.last_seen_at <= run.started_at,
                    missing_condition,
                )
                .values(status="inactive", inactive_at=run.finished_at)
            ),
        )
        inactivated_count = result.rowcount
        if inactivated_count < 0:
            raise LifecycleReconciliationConflictError(
                "database did not report lifecycle update count"
            )
        if inactivated_count:
            self.session.execute(
                update(CollectionRunModel)
                .where(CollectionRunModel.id == persisted_run.id)
                .values(inactivated=CollectionRunModel.inactivated + inactivated_count)
            )
        self.session.flush()
        return inactivated_count
