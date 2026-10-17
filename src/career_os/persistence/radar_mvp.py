"""Read-only persistence boundary for the Career Radar MVP."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from career_os.config.radar_panel import RadarPanelManifestIdentity
from career_os.persistence.models import (
    CollectionRun,
    JobPosting,
    RadarPanelVersion,
    RadarScanObservation,
    Source,
)
from career_os.radar.aggregate import RadarObservationInput, RadarPanelInput, RadarScanInput


@dataclass(frozen=True, slots=True)
class RadarMvpSource:
    key: str
    kind: str
    company_name: str
    enabled: bool
    status: str | None
    last_success_at: datetime | None
    stale: bool


@dataclass(frozen=True, slots=True)
class RadarMvpJob:
    id: UUID
    source_key: str
    source_kind: str
    company_name: str
    company_key: str
    source_enabled: bool
    external_id: str
    title: str
    description: str
    source_url: str | None
    apply_url: str | None
    last_seen_at: datetime
    status: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class RadarMvpHistoryInputs:
    panel: RadarPanelInput | None
    scans: tuple[RadarScanInput, ...]
    reason: str | None


class RadarMvpReadRepository:
    """Select only safe current columns and immutable, version-matched scan text."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_sources(
        self, *, now: datetime, freshness_target_seconds: int
    ) -> tuple[RadarMvpSource, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        sources = tuple(
            self._session.execute(
                select(
                    Source.id, Source.key, Source.kind, Source.company_name, Source.enabled
                ).order_by(Source.key)
            ).all()
        )
        latest_runs = (
            select(
                CollectionRun.source_id.label("source_id"),
                CollectionRun.status.label("status"),
                func.row_number()
                .over(
                    partition_by=CollectionRun.source_id,
                    order_by=(CollectionRun.started_at.desc(), CollectionRun.id.desc()),
                )
                .label("rank"),
            )
            .where(CollectionRun.status != "running")
            .subquery()
        )
        latest_by_source = {
            source_id: status
            for source_id, status in self._session.execute(
                select(latest_runs.c.source_id, latest_runs.c.status).where(latest_runs.c.rank == 1)
            )
        }
        success_by_source: dict[UUID, datetime | None] = {
            source_id: finished_at
            for source_id, finished_at in self._session.execute(
                select(CollectionRun.source_id, func.max(CollectionRun.finished_at))
                .where(CollectionRun.status == "succeeded")
                .group_by(CollectionRun.source_id)
            )
        }
        now_utc = now.astimezone(UTC)
        result: list[RadarMvpSource] = []
        for source_id, key, kind, company_name, enabled in sources:
            last_success = success_by_source.get(source_id)
            if last_success is not None and (
                last_success.tzinfo is None or last_success.utcoffset() is None
            ):
                raise ValueError("source success timestamp must be timezone-aware")
            stale = bool(
                last_success is None
                or (now_utc - last_success.astimezone(UTC)).total_seconds()
                > freshness_target_seconds
            )
            result.append(
                RadarMvpSource(
                    key=key,
                    kind=kind,
                    company_name=company_name,
                    enabled=enabled,
                    status=latest_by_source.get(source_id),
                    last_success_at=last_success,
                    stale=stale,
                )
            )
        return tuple(result)

    def list_jobs(
        self,
        *,
        enabled_only: bool,
        include_inactive: bool,
        source_keys: tuple[str, ...],
        company_key_by_source: dict[str, str],
    ) -> tuple[RadarMvpJob, ...]:
        statement = (
            select(
                JobPosting.id,
                Source.key,
                Source.kind,
                Source.company_name,
                Source.enabled,
                JobPosting.external_id,
                JobPosting.title_raw,
                JobPosting.description_text,
                JobPosting.source_url,
                JobPosting.apply_url,
                JobPosting.last_seen_at,
                JobPosting.status,
                JobPosting.content_hash,
            )
            .join(Source, JobPosting.source_id == Source.id)
            .where(Source.key.in_(source_keys))
            .order_by(Source.key, JobPosting.external_id, JobPosting.id)
        )
        if enabled_only:
            statement = statement.where(Source.enabled.is_(True))
        if not include_inactive:
            statement = statement.where(JobPosting.status == "active")
        result: list[RadarMvpJob] = []
        for row in self._session.execute(statement):
            (
                job_id,
                source_key,
                source_kind,
                company_name,
                source_enabled,
                external_id,
                title,
                description,
                source_url,
                apply_url,
                last_seen_at,
                status,
                content_hash,
            ) = row
            result.append(
                RadarMvpJob(
                    id=job_id,
                    source_key=source_key,
                    source_kind=source_kind,
                    company_name=company_name,
                    company_key=company_key_by_source.get(
                        source_key, " ".join(company_name.casefold().split())
                    ),
                    source_enabled=source_enabled,
                    external_id=external_id,
                    title=title,
                    description=description,
                    source_url=source_url,
                    apply_url=apply_url,
                    last_seen_at=last_seen_at,
                    status=status,
                    content_hash=content_hash,
                )
            )
        return tuple(result)

    def get_job(
        self,
        job_id: UUID,
        *,
        company_key_by_source: dict[str, str],
    ) -> RadarMvpJob | None:
        row = self._session.execute(
            select(
                JobPosting.id,
                Source.key,
                Source.kind,
                Source.company_name,
                Source.enabled,
                JobPosting.external_id,
                JobPosting.title_raw,
                JobPosting.description_text,
                JobPosting.source_url,
                JobPosting.apply_url,
                JobPosting.last_seen_at,
                JobPosting.status,
                JobPosting.content_hash,
            )
            .join(Source, JobPosting.source_id == Source.id)
            .where(JobPosting.id == job_id)
        ).one_or_none()
        if row is None:
            return None
        (
            found_id,
            source_key,
            source_kind,
            company_name,
            source_enabled,
            external_id,
            title,
            description,
            source_url,
            apply_url,
            last_seen_at,
            status,
            content_hash,
        ) = row
        return RadarMvpJob(
            id=found_id,
            source_key=source_key,
            source_kind=source_kind,
            company_name=company_name,
            company_key=company_key_by_source.get(
                source_key, " ".join(company_name.casefold().split())
            ),
            source_enabled=source_enabled,
            external_id=external_id,
            title=title,
            description=description,
            source_url=source_url,
            apply_url=apply_url,
            last_seen_at=last_seen_at,
            status=status,
            content_hash=content_hash,
        )

    def load_history_inputs(
        self,
        *,
        identity: RadarPanelManifestIdentity,
        cutoff: datetime,
        lookback_days: int,
    ) -> RadarMvpHistoryInputs:
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("history cutoff must be timezone-aware")
        inspector = inspect(self._session.connection())
        required = {"radar_panel_versions", "radar_scan_observations"}
        if not required.issubset(inspector.get_table_names()):
            return RadarMvpHistoryInputs(None, (), "missing_history_tables")
        run_columns = {column["name"] for column in inspector.get_columns("collection_runs")}
        if not {
            "radar_panel_version",
            "radar_manifest_digest",
            "radar_panel_digest",
        }.issubset(run_columns):
            return RadarMvpHistoryInputs(None, (), "missing_history_columns")

        record = self._session.get(RadarPanelVersion, identity.panel_version)
        if record is None:
            return RadarMvpHistoryInputs(None, (), "panel_not_registered")
        if (
            record.manifest_digest != identity.manifest_digest
            or record.canonical_manifest != identity.canonical_manifest
        ):
            return RadarMvpHistoryInputs(None, (), "panel_identity_mismatch")

        source_ids = {
            source_key: source_kind
            for source_key, source_kind in self._session.execute(
                select(Source.key, Source.kind).where(
                    Source.key.in_(tuple(source.source_key for source in identity.sources))
                )
            )
        }
        if any(
            source_ids.get(source.source_key) != source.source_kind for source in identity.sources
        ):
            return RadarMvpHistoryInputs(None, (), "panel_source_mismatch")

        panel = RadarPanelInput(
            panel_version=record.panel_version,
            manifest_digest=record.manifest_digest,
            panel_digest=record.panel_digest,
            source_config_digests=dict(record.source_config_digests),
            canonical_manifest=record.canonical_manifest,
            sources=identity.sources,
        )
        rows = self._session.execute(
            select(
                CollectionRun.id,
                CollectionRun.finished_at,
                Source.key,
                Source.kind,
            )
            .join(Source, CollectionRun.source_id == Source.id)
            .where(
                CollectionRun.radar_panel_version == panel.panel_version,
                CollectionRun.radar_manifest_digest == panel.manifest_digest,
                CollectionRun.radar_panel_digest == panel.panel_digest,
                CollectionRun.status == "succeeded",
                CollectionRun.finished_at.is_not(None),
                CollectionRun.finished_at >= cutoff.astimezone(UTC) - timedelta(days=lookback_days),
                CollectionRun.finished_at < cutoff.astimezone(UTC),
                Source.key.in_(tuple(source.source_key for source in identity.sources)),
            )
            .order_by(CollectionRun.finished_at, CollectionRun.id)
        )
        headers = tuple(rows)
        if not headers:
            return RadarMvpHistoryInputs(panel, (), "missing_history")
        run_ids = tuple(row.id for row in headers)
        observations: dict[UUID, list[RadarObservationInput]] = {run_id: [] for run_id in run_ids}
        for run_id, external_id, observed_at, title, description in self._session.execute(
            select(
                RadarScanObservation.run_id,
                RadarScanObservation.external_id,
                RadarScanObservation.observed_at,
                RadarScanObservation.title,
                RadarScanObservation.description,
            )
            .where(RadarScanObservation.run_id.in_(run_ids))
            .order_by(RadarScanObservation.run_id, RadarScanObservation.external_id)
        ):
            observations[run_id].append(
                RadarObservationInput(
                    external_id=external_id,
                    observed_at=observed_at,
                    title=title,
                    description=description,
                )
            )
        scans: list[RadarScanInput] = []
        panel_by_key = {source.source_key: source for source in identity.sources}
        for run_id, finished_at, source_key, source_kind in headers:
            panel_source = panel_by_key[source_key]
            if source_kind != panel_source.source_kind:
                return RadarMvpHistoryInputs(None, (), "panel_source_mismatch")
            if finished_at is None:
                continue
            scans.append(
                RadarScanInput(
                    run_id=run_id,
                    source_key=source_key,
                    source_kind=source_kind,
                    finished_at=finished_at,
                    observations=tuple(observations[run_id]),
                )
            )
        return RadarMvpHistoryInputs(panel, tuple(scans), None)
