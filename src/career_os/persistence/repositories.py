"""SQLAlchemy repositories for collection state and canonical job versions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final, cast
from uuid import UUID

from sqlalchemy import Select, delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from career_os.application.collect import CollectionRun, CollectionRunStatus
from career_os.config.radar_panel import RadarPanelManifest
from career_os.config.sources import SourceConfig, SourcesConfig
from career_os.db import session_scope
from career_os.domain.canonical import (
    CANONICAL_CONTENT_FIELDS,
    CanonicalJobCandidate,
    JsonValue,
    content_hash,
)
from career_os.domain.skills import SkillEvidence
from career_os.domain.versions import NO_ENRICHMENT_NORMALIZER_VERSION
from career_os.persistence.models import (
    CollectionRun as CollectionRunModel,
)
from career_os.persistence.models import (
    JobPosting,
    JobSkill,
    JobVersion,
    RadarPanelVersion,
    RadarScanObservation,
    Source,
)

type SessionFactory = sessionmaker[Session]
type EvidenceFact = tuple[str, str, str | None, str, Decimal, str]

_PERSISTED_RUN_COUNT_FIELDS: Final = (
    "items_fetched",
    "inserted",
    "updated",
    "unchanged",
    "inactivated",
    "failed_items",
    "http_requests",
    "retry_count",
    "rate_limit_events",
    "normalization_errors",
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


class SourceConfigurationConflictError(PersistenceError):
    """A source key would rewrite an immutable persisted source identity."""


class RadarPanelVersionConflictError(PersistenceError):
    """A panel version was reused with different immutable manifest/config inputs."""


class JobNotFoundError(PersistenceError):
    """A requested canonical job no longer exists."""


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


@dataclass(frozen=True, slots=True)
class SkillEvidenceReplaceResult:
    """The durable outcome of replacing deterministic evidence for one job."""

    job_id: UUID
    taxonomy_version: str
    evidence_count: int
    replaced: bool

    def __post_init__(self) -> None:
        if not isinstance(self.taxonomy_version, str) or not self.taxonomy_version.strip():
            raise ValueError("taxonomy_version must be non-blank")
        if self.evidence_count < 0:
            raise ValueError("evidence_count must be non-negative")


@dataclass(slots=True)
class SourceConfigurationRepository:
    """Register validated non-secret source configuration before collection."""

    session: Session

    def sync(self, sources_config: SourcesConfig) -> None:
        """Insert unseen source keys and refresh mutable settings for known keys.

        A key is an operational identity: changing its kind or company would
        make its existing job history ambiguous, so those changes fail closed.
        """
        for source_config in sources_config.sources:
            self._sync_one(source_config)
        self.session.flush()

    def _sync_one(self, source_config: SourceConfig) -> None:
        source = self.session.scalar(select(Source).where(Source.key == source_config.key))
        source_config_json = cast(dict[str, object], source_config.model_dump(mode="json"))
        if source is None:
            self.session.add(
                Source(
                    key=source_config.key,
                    kind=source_config.kind,
                    company_name=source_config.company_name,
                    config=source_config_json,
                    enabled=source_config.enabled,
                )
            )
            return
        if source.kind != source_config.kind or source.company_name != source_config.company_name:
            raise SourceConfigurationConflictError(
                f"configured source key cannot change kind or company name: {source_config.key}"
            )
        source.config = source_config_json
        source.enabled = source_config.enabled


@dataclass(slots=True)
class RadarPanelVersionRepository:
    """Register an immutable panel identity, comparing concurrent inserts safely."""

    session: Session

    def assert_frozen(self, manifest: RadarPanelManifest) -> RadarPanelVersion:
        """Require a previously registered version with identical immutable inputs."""
        existing = self.session.scalar(
            select(RadarPanelVersion)
            .where(RadarPanelVersion.panel_version == manifest.panel_version)
            .with_for_update()
        )
        if existing is None:
            raise RadarPanelVersionConflictError(
                "Radar panel version must be registered before collection starts"
            )
        return self._assert_identical(existing, manifest)

    def register(self, manifest: RadarPanelManifest) -> RadarPanelVersion:
        """Insert one panel version or prove the existing version is byte-stable."""
        statement = (
            select(RadarPanelVersion)
            .where(RadarPanelVersion.panel_version == manifest.panel_version)
            .with_for_update()
        )
        existing = self.session.scalar(statement)
        if existing is not None:
            return self._assert_identical(existing, manifest)

        record = RadarPanelVersion(
            panel_version=manifest.panel_version,
            manifest_digest=manifest.manifest_digest,
            panel_digest=manifest.panel_digest,
            canonical_manifest=manifest.canonical_manifest,
            source_config_digests=dict(manifest.source_config_digests),
        )
        try:
            with self.session.begin_nested():
                self.session.add(record)
                self.session.flush()
        except IntegrityError:
            existing = self.session.scalar(statement)
            if existing is None:
                raise
            return self._assert_identical(existing, manifest)
        return record

    @staticmethod
    def _assert_identical(
        existing: RadarPanelVersion, manifest: RadarPanelManifest
    ) -> RadarPanelVersion:
        if (
            existing.manifest_digest != manifest.manifest_digest
            or existing.panel_digest != manifest.panel_digest
            or existing.canonical_manifest != manifest.canonical_manifest
            or existing.source_config_digests != dict(manifest.source_config_digests)
        ):
            raise RadarPanelVersionConflictError(
                "Radar panel version is already frozen with different manifest or source config"
            )
        return existing


@dataclass(slots=True)
class RadarScanObservationRepository:
    """Persist only complete scan inputs against their verified frozen panel header."""

    session: Session

    def capture_complete(
        self, *, run: CollectionRun, candidates: tuple[CanonicalJobCandidate, ...]
    ) -> None:
        """Insert exact title/description inputs for one complete panel scan."""
        if run.radar_panel_version is None:
            if any(
                value is not None for value in (run.radar_manifest_digest, run.radar_panel_digest)
            ):
                raise CollectionRunPersistenceConflictError(
                    "Radar scan has incomplete panel provenance"
                )
            return
        if run.status is not CollectionRunStatus.SUCCEEDED:
            return
        if run.finished_at is None:
            raise CollectionRunPersistenceConflictError(
                "a complete Radar scan requires a finished_at timestamp"
            )

        header_statement = (
            select(CollectionRunModel, Source)
            .join(Source, CollectionRunModel.source_id == Source.id)
            .where(CollectionRunModel.id == _run_id_as_uuid(run.id))
            .with_for_update(of=CollectionRunModel)
        )
        header_row = self.session.execute(header_statement).one_or_none()
        if header_row is None:
            raise CollectionRunPersistenceConflictError("Radar scan run header was not found")
        persisted_run, source = header_row
        if (
            persisted_run.status != CollectionRunStatus.RUNNING.value
            or source.key != run.source_key
            or source.kind != run.source_kind
            or persisted_run.radar_panel_version != run.radar_panel_version
            or persisted_run.radar_manifest_digest != run.radar_manifest_digest
            or persisted_run.radar_panel_digest != run.radar_panel_digest
        ):
            raise CollectionRunPersistenceConflictError(
                "Radar observations do not match their running panel source header"
            )

        frozen_panel = self.session.get(RadarPanelVersion, run.radar_panel_version)
        if frozen_panel is None or (
            frozen_panel.manifest_digest != run.radar_manifest_digest
            or frozen_panel.panel_digest != run.radar_panel_digest
        ):
            raise RadarPanelVersionConflictError(
                "Radar scan provenance does not match its frozen panel version"
            )
        panel_sources = frozen_panel.canonical_manifest.get("sources")
        if not isinstance(panel_sources, list) or not any(
            isinstance(entry, dict)
            and entry.get("source_key") == run.source_key
            and entry.get("source_kind") == run.source_kind
            for entry in panel_sources
        ):
            raise SourceIdentityConflictError(
                f"Radar scan source is outside its frozen panel: {run.source_key}"
            )
        if run.source_key not in frozen_panel.source_config_digests:
            raise RadarPanelVersionConflictError(
                "Radar panel has no source config digest for the scan source"
            )

        seen_external_ids: set[str] = set()
        observations: list[RadarScanObservation] = []
        for candidate in candidates:
            if candidate.source_key != run.source_key or candidate.source_kind != run.source_kind:
                raise SourceIdentityConflictError(
                    "Radar candidate source identity does not match its scan header"
                )
            if candidate.external_id in seen_external_ids:
                raise PersistenceError("complete Radar scan contains duplicate source posting IDs")
            seen_external_ids.add(candidate.external_id)
            observations.append(
                RadarScanObservation(
                    run_id=_run_id_as_uuid(run.id),
                    external_id=candidate.external_id,
                    observed_at=run.finished_at,
                    title=candidate.title_raw,
                    description=candidate.description_text,
                )
            )
        self.session.add_all(observations)


@dataclass(slots=True)
class InspectionRepository:
    """Read-only persistence queries used by the thin operational CLI."""

    session: Session

    def list_runs(self, *, failed_only: bool = False) -> list[RunInspection]:
        statement = (
            select(CollectionRunModel, Source.key, Source.kind)
            .join(Source, CollectionRunModel.source_id == Source.id)
            .order_by(CollectionRunModel.started_at.desc())
        )
        if failed_only:
            statement = statement.where(
                CollectionRunModel.status == CollectionRunStatus.FAILED.value
            )
        return [
            RunInspection(run=run, source_key=key, source_kind=kind)
            for run, key, kind in self.session.execute(statement)
        ]

    def get_run(self, run_id: UUID) -> RunInspection | None:
        row = self.session.execute(
            select(CollectionRunModel, Source.key, Source.kind)
            .join(Source, CollectionRunModel.source_id == Source.id)
            .where(CollectionRunModel.id == run_id)
        ).one_or_none()
        return (
            None
            if row is None
            else RunInspection(run=row[0], source_key=row[1], source_kind=row[2])
        )

    def list_jobs(self, *, active_only: bool, company_name: str | None) -> list[JobInspection]:
        statement = (
            select(JobPosting, Source.key, Source.kind)
            .join(Source, JobPosting.source_id == Source.id)
            .order_by(JobPosting.last_seen_at.desc(), JobPosting.id)
        )
        if active_only:
            statement = statement.where(JobPosting.status == "active")
        if company_name is not None:
            statement = statement.where(JobPosting.company_name == company_name)
        return [
            JobInspection(job=job, source_key=key, source_kind=kind)
            for job, key, kind in self.session.execute(statement)
        ]


@dataclass(frozen=True, slots=True)
class RunInspection:
    """A collection run paired with its immutable source identity."""

    run: CollectionRunModel
    source_key: str
    source_kind: str


@dataclass(frozen=True, slots=True)
class JobInspection:
    """A canonical job paired with its immutable source identity."""

    job: JobPosting
    source_key: str
    source_kind: str


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


def _evidence_facts(
    evidence: tuple[SkillEvidence, ...],
) -> tuple[EvidenceFact, ...]:
    return _sorted_evidence_facts(
        (
            item.skill_id,
            item.matched_text,
            item.section,
            item.method,
            item.confidence,
            item.taxonomy_version,
        )
        for item in evidence
    )


def _stored_evidence_facts(
    evidence: tuple[JobSkill, ...],
) -> tuple[EvidenceFact, ...]:
    return _sorted_evidence_facts(
        (
            item.skill_id,
            item.matched_text,
            item.section,
            item.method,
            item.confidence,
            item.taxonomy_version,
        )
        for item in evidence
    )


def _sorted_evidence_facts(facts: Iterable[EvidenceFact]) -> tuple[EvidenceFact, ...]:
    return tuple(
        sorted(
            facts,
            key=lambda fact: (
                fact[0],
                fact[1],
                fact[2] is not None,
                fact[2] or "",
                fact[3],
                fact[4],
                fact[5],
            ),
        )
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
    radar_panel: RadarPanelManifest | None = None
    source_config: SourceConfig | None = None

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
        self._assert_panel_start_identity(run)
        try:
            with session_scope(self.session_factory) as session:
                source = _source_for_identity(
                    session, source_key=run.source_key, source_kind=run.source_kind
                )
                if self.source_config is not None and source.company_name != (
                    self.source_config.company_name
                ):
                    raise SourceIdentityConflictError(
                        f"configured source company does not match run: {run.source_key}"
                    )
                if self.radar_panel is not None:
                    RadarPanelVersionRepository(session).assert_frozen(self.radar_panel)
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
                        radar_panel_version=run.radar_panel_version,
                        radar_manifest_digest=run.radar_manifest_digest,
                        radar_panel_digest=run.radar_panel_digest,
                        error_summary=run.error_summary,
                        incomplete_reason=run.incomplete_reason,
                    )
                )
                session.flush()
        except IntegrityError as error:
            raise CollectionRunPersistenceConflictError(
                "collection run already exists or violates a persistence constraint"
            ) from error

    def _assert_panel_start_identity(self, run: CollectionRun) -> None:
        """Validate panel membership and the exact typed source config before fetch."""
        if self.radar_panel is None:
            if any(
                value is not None
                for value in (
                    run.radar_panel_version,
                    run.radar_manifest_digest,
                    run.radar_panel_digest,
                )
            ):
                raise RadarPanelVersionConflictError(
                    "collection run panel provenance has no validated manifest"
                )
            return
        if self.source_config is None:
            raise RadarPanelVersionConflictError(
                "panel collection requires its typed source configuration"
            )
        if (
            run.radar_panel_version != self.radar_panel.panel_version
            or run.radar_manifest_digest != self.radar_panel.manifest_digest
            or run.radar_panel_digest != self.radar_panel.panel_digest
        ):
            raise RadarPanelVersionConflictError(
                "collection run provenance does not match its validated panel manifest"
            )
        entry = next(
            (source for source in self.radar_panel.sources if source.source_key == run.source_key),
            None,
        )
        if (
            entry is None
            or entry.source_kind != run.source_kind
            or self.source_config.key != run.source_key
            or self.source_config.kind != run.source_kind
        ):
            raise SourceIdentityConflictError(
                f"configured source does not match frozen Radar panel: {run.source_key}"
            )
        expected_digest = self.radar_panel.source_config_digests.get(run.source_key)
        actual_digest = content_hash(self.source_config.model_dump(mode="json"))
        if expected_digest is None or actual_digest != expected_digest:
            raise RadarPanelVersionConflictError(
                f"source config drifted from frozen Radar panel: {run.source_key}"
            )

    def update(self, run: CollectionRun) -> None:
        """Commit exactly one running-to-terminal transition using a compare-and-set."""
        if run.status is CollectionRunStatus.RUNNING:
            raise CollectionRunPersistenceConflictError("a collection run update must be terminal")
        self._assert_persistable_counts(run)
        with session_scope(self.session_factory) as session:
            self.update_in_session(session, run)

    def update_in_session(self, session: Session, run: CollectionRun) -> None:
        """Apply the terminal compare-and-set inside a caller-owned transaction."""
        if run.status is CollectionRunStatus.RUNNING:
            raise CollectionRunPersistenceConflictError("a collection run update must be terminal")
        self._assert_persistable_counts(run)
        source = _source_for_identity(
            session,
            source_key=run.source_key,
            source_kind=run.source_kind,
        )
        result = cast(
            CursorResult[object],
            session.execute(
                update(CollectionRunModel)
                .where(
                    CollectionRunModel.id == _run_id_as_uuid(run.id),
                    CollectionRunModel.source_id == source.id,
                    CollectionRunModel.status == CollectionRunStatus.RUNNING.value,
                    CollectionRunModel.radar_panel_version == run.radar_panel_version,
                    CollectionRunModel.radar_manifest_digest == run.radar_manifest_digest,
                    CollectionRunModel.radar_panel_digest == run.radar_panel_digest,
                )
                .values(
                    status=run.status.value,
                    finished_at=run.finished_at,
                    **_persisted_run_counts(run),
                    error_summary=run.error_summary,
                    incomplete_reason=run.incomplete_reason,
                )
            ),
        )
        if result.rowcount != 1:
            raise CollectionRunPersistenceConflictError(
                "collection run was not in the expected running state"
            )

    @staticmethod
    def _assert_persistable_counts(run: CollectionRun) -> None:
        """All source-neutral counters have durable columns in the current schema."""
        del run


@dataclass(slots=True)
class CanonicalJobRepository:
    """Upsert canonical jobs and immutable content versions in a caller transaction."""

    session: Session

    def upsert(
        self,
        candidate: CanonicalJobCandidate,
        *,
        allow_reappearing_version: bool = False,
    ) -> JobUpsertResult:
        """Persist one candidate without committing the caller-owned transaction.

        Legacy callers retain the strict no-reversion rule. Radar capture may
        opt in because its independent scan-observation table records each new
        occurrence while ``job_versions`` remains unique by content hash.
        """
        source = _source_for_identity(
            self.session,
            source_key=candidate.source_key,
            source_kind=candidate.source_kind,
            company_name=candidate.company_name,
        )
        job = self._locked_job(source_id=source.id, external_id=candidate.external_id)
        if job is None:
            return self._insert_or_resolve_race(
                source, candidate, allow_reappearing_version=allow_reappearing_version
            )
        return self._update_existing(
            job, candidate, allow_reappearing_version=allow_reappearing_version
        )

    def _locked_job(self, *, source_id: UUID, external_id: str) -> JobPosting | None:
        statement: Select[tuple[JobPosting]] = (
            select(JobPosting)
            .where(JobPosting.source_id == source_id, JobPosting.external_id == external_id)
            .with_for_update()
        )
        return self.session.scalar(statement)

    def _insert_or_resolve_race(
        self,
        source: Source,
        candidate: CanonicalJobCandidate,
        *,
        allow_reappearing_version: bool,
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
            return self._update_existing(
                job, candidate, allow_reappearing_version=allow_reappearing_version
            )
        assert job is not None
        return JobUpsertResult(
            action=JobUpsertAction.INSERTED,
            job_id=job.id,
            content_hash=candidate.content_hash,
            changed_fields=CANONICAL_CONTENT_FIELDS,
        )

    def _update_existing(
        self,
        job: JobPosting,
        candidate: CanonicalJobCandidate,
        *,
        allow_reappearing_version: bool = False,
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
            raw_latest = _plain_json_object(candidate.raw_payload)
            if job.raw_latest != raw_latest:
                # Structured source facts intentionally do not all participate in the
                # canonical text hash, but may change offline enrichment output.
                job.enrichment_normalizer_version = NO_ENRICHMENT_NORMALIZER_VERSION
                job.taxonomy_version = candidate.taxonomy_version
            job.raw_latest = raw_latest
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
        if prior_version is not None and not allow_reappearing_version:
            raise CanonicalVersionConflictError(
                "candidate reverts to an already-recorded canonical content version"
            )
        if reactivated:
            job.status = "active"
            job.inactive_at = None
        self._apply_candidate_content(job, candidate)
        if prior_version is None:
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
            enrichment_normalizer_version=NO_ENRICHMENT_NORMALIZER_VERSION,
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
        job.enrichment_normalizer_version = NO_ENRICHMENT_NORMALIZER_VERSION
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
class JobSkillRepository:
    """Replace one job's deterministic skill evidence in a caller transaction.

    Manual and LLM rows are deliberately outside this repository's ownership:
    re-enrichment may replace deterministic output from any taxonomy version,
    but it must not erase evidence supplied by a later human-review workflow.
    """

    session: Session

    def replace_deterministic_evidence(
        self,
        *,
        job_id: UUID,
        evidence: tuple[SkillEvidence, ...],
        taxonomy_version: str,
    ) -> SkillEvidenceReplaceResult:
        """Atomically make deterministic evidence equal to ``evidence``.

        Repeating a call with the same complete evidence set does no database
        mutation.  A taxonomy change or any missing/stale deterministic row
        replaces the whole managed set, so no stale aliases survive.
        """
        if not isinstance(taxonomy_version, str) or not taxonomy_version.strip():
            raise ValueError("taxonomy_version must be non-blank")
        if any(item.taxonomy_version != taxonomy_version for item in evidence):
            raise ValueError("all evidence must use the replacement taxonomy_version")

        job = self.session.scalar(
            select(JobPosting).where(JobPosting.id == job_id).with_for_update()
        )
        if job is None:
            raise JobNotFoundError(f"canonical job not found: {job_id}")
        current = tuple(
            self.session.scalars(
                select(JobSkill)
                .where(
                    JobSkill.job_id == job_id,
                    JobSkill.method.in_(("alias", "exact", "pattern")),
                )
                .with_for_update()
            )
        )
        expected_facts = _evidence_facts(evidence)
        current_facts = _stored_evidence_facts(current)
        replacement_required = (
            current_facts != expected_facts or job.taxonomy_version != taxonomy_version
        )
        if not replacement_required:
            return SkillEvidenceReplaceResult(
                job_id=job_id,
                taxonomy_version=taxonomy_version,
                evidence_count=len(evidence),
                replaced=False,
            )

        self.session.execute(
            delete(JobSkill).where(
                JobSkill.job_id == job_id,
                JobSkill.method.in_(("alias", "exact", "pattern")),
            )
        )
        self.session.add_all(
            [
                JobSkill(
                    job_id=job_id,
                    skill_id=item.skill_id,
                    matched_text=item.matched_text,
                    section=item.section,
                    method=item.method,
                    confidence=item.confidence,
                    taxonomy_version=item.taxonomy_version,
                )
                for item in evidence
            ]
        )
        job.taxonomy_version = taxonomy_version
        self.session.flush()
        return SkillEvidenceReplaceResult(
            job_id=job_id,
            taxonomy_version=taxonomy_version,
            evidence_count=len(evidence),
            replaced=True,
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
