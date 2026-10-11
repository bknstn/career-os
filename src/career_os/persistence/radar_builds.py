"""Read frozen Radar inputs and persist immutable aggregate results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from career_os.config.radar_panel import RadarPanelManifestIdentity
from career_os.persistence.models import (
    CollectionRun,
    RadarBuild,
    RadarPanelVersion,
    RadarScanObservation,
    Source,
)
from career_os.persistence.repositories import RadarPanelVersionConflictError
from career_os.radar.aggregate import (
    RadarObservationInput,
    RadarPanelInput,
    RadarScanInput,
)


@dataclass(slots=True)
class RadarBuildRepository:
    """Persistence access for one snapshot-consistent Radar build transaction."""

    session: Session

    def require_panel(self, identity: RadarPanelManifestIdentity) -> RadarPanelInput:
        """Use the registered manifest and panel digest without current source config."""
        record = self.session.get(RadarPanelVersion, identity.panel_version)
        if record is None:
            raise RadarPanelVersionConflictError(
                "Radar panel manifest must be registered by a collection before build"
            )
        if (
            record.manifest_digest != identity.manifest_digest
            or record.canonical_manifest != identity.canonical_manifest
        ):
            raise RadarPanelVersionConflictError(
                "Radar panel manifest does not match its registered semantic version"
            )
        return RadarPanelInput(
            panel_version=record.panel_version,
            manifest_digest=record.manifest_digest,
            panel_digest=record.panel_digest,
            source_config_digests=dict(record.source_config_digests),
            canonical_manifest=record.canonical_manifest,
            sources=identity.sources,
        )

    def load_scans(self, panel: RadarPanelInput, cutoff: datetime) -> tuple[RadarScanInput, ...]:
        """Load only eligible complete capture runs and immutable observation rows."""
        panel_by_key = {source.source_key: source for source in panel.sources}
        starts_at = cutoff - timedelta(days=180)
        rows = tuple(
            self.session.execute(
                select(CollectionRun, Source.key, Source.kind)
                .join(Source, CollectionRun.source_id == Source.id)
                .where(
                    CollectionRun.radar_panel_version == panel.panel_version,
                    CollectionRun.radar_manifest_digest == panel.manifest_digest,
                    CollectionRun.radar_panel_digest == panel.panel_digest,
                    CollectionRun.status == "succeeded",
                    CollectionRun.finished_at.is_not(None),
                    CollectionRun.finished_at >= starts_at,
                    CollectionRun.finished_at < cutoff,
                    Source.key.in_(tuple(panel_by_key)),
                )
                .order_by(CollectionRun.finished_at, CollectionRun.id)
            ).all()
        )
        run_headers: dict[UUID, tuple[CollectionRun, str, str]] = {}
        for run, source_key, source_kind in rows:
            panel_source = panel_by_key.get(source_key)
            if panel_source is None or panel_source.source_kind != source_kind:
                raise RadarPanelVersionConflictError(
                    f"captured scan source identity differs from registered panel: {source_key}"
                )
            if run.finished_at is None:
                continue
            run_headers[run.id] = (run, source_key, source_kind)

        observation_rows: dict[UUID, list[RadarObservationInput]] = {
            run_id: [] for run_id in run_headers
        }
        if run_headers:
            for observation in self.session.scalars(
                select(RadarScanObservation)
                .where(RadarScanObservation.run_id.in_(tuple(run_headers)))
                .order_by(RadarScanObservation.run_id, RadarScanObservation.external_id)
            ):
                header = run_headers.get(observation.run_id)
                if header is None:
                    continue
                run = header[0]
                if run.finished_at is None or observation.observed_at != run.finished_at:
                    raise RadarPanelVersionConflictError(
                        "captured observation time does not match its completed scan"
                    )
                observation_rows[observation.run_id].append(
                    RadarObservationInput(
                        external_id=observation.external_id,
                        observed_at=observation.observed_at,
                        title=observation.title,
                        description=observation.description,
                    )
                )
        scans: list[RadarScanInput] = []
        for run_id, (run, source_key, source_kind) in sorted(
            run_headers.items(),
            key=lambda item: (item[1][0].finished_at or cutoff, str(item[0])),
        ):
            if run.finished_at is None:
                continue
            scans.append(
                RadarScanInput(
                    run_id=run_id,
                    source_key=source_key,
                    source_kind=source_kind,
                    finished_at=run.finished_at,
                    observations=tuple(observation_rows[run_id]),
                )
            )
        return tuple(scans)

    def find_by_idempotence_key(self, key: str) -> RadarBuild | None:
        """Return the immutable result for an exact key, if already committed."""
        return self.session.scalar(select(RadarBuild).where(RadarBuild.idempotence_key == key))

    def insert(
        self,
        *,
        key: str,
        panel: RadarPanelInput,
        cutoff: datetime,
        input_digest: str,
        payload: dict[str, object],
    ) -> RadarBuild:
        """Stage one complete JSON result for the caller's atomic transaction."""
        run_id = UUID(str(payload["run_id"]))
        record = RadarBuild(
            id=run_id,
            idempotence_key=key,
            panel_version=panel.panel_version,
            panel_digest=panel.panel_digest,
            input_digest=input_digest,
            cutoff_utc=cutoff,
            payload=payload,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def find_by_id(self, run_id: UUID) -> RadarBuild | None:
        """Read a saved JSON result without touching current source or job rows."""
        return self.session.get(RadarBuild, run_id)
