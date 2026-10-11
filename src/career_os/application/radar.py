"""Build and retrieve immutable roles-only Career Radar results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from career_os.config.radar_panel import RadarPanelManifest
from career_os.persistence.radar_builds import RadarBuildRepository
from career_os.persistence.repositories import PersistenceError
from career_os.radar.aggregate import build_radar_result, idempotence_key, normalize_cutoff

_MAX_BUILD_ATTEMPTS = 3


class RadarBuildPersistenceError(PersistenceError):
    """Radar build persistence failed after its transaction was rolled back."""


@dataclass(slots=True)
class RadarBuildService:
    """Persist one complete result from a repeatable-read evidence snapshot."""

    session_factory: sessionmaker[Session]

    def build(self, manifest_path: str, cutoff: datetime) -> dict[str, object]:
        """Build or return the immutable result identified by these frozen inputs."""
        identity = RadarPanelManifest.load_identity(manifest_path)
        normalized_cutoff = normalize_cutoff(cutoff)
        for attempt in range(_MAX_BUILD_ATTEMPTS):
            session = self.session_factory()
            try:
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                repository = RadarBuildRepository(session)
                panel = repository.require_panel(identity)
                scans = repository.load_scans(panel, normalized_cutoff)
                payload = build_radar_result(panel, normalized_cutoff, scans)
                key = idempotence_key(payload)
                existing = repository.find_by_idempotence_key(key)
                if existing is not None:
                    session.commit()
                    return dict(existing.payload)

                run_id = uuid4()
                payload["run_id"] = str(run_id)
                payload["idempotence_key"] = key
                repository.insert(
                    key=key,
                    panel=panel,
                    cutoff=normalized_cutoff,
                    input_digest=str(payload["input_digest"]),
                    payload=payload,
                )
                session.commit()
                return payload
            except DBAPIError as error:
                session.rollback()
                if _is_idempotence_race(error) and attempt + 1 < _MAX_BUILD_ATTEMPTS:
                    continue
                raise RadarBuildPersistenceError(
                    "Radar build transaction failed; no partial result was committed"
                ) from error
            except BaseException:
                session.rollback()
                raise
            finally:
                session.close()
        raise RadarBuildPersistenceError("Radar build could not resolve a concurrent insert")

    def show(self, run_id: UUID) -> dict[str, object] | None:
        """Return only the saved JSON result; never read mutable posting projections."""
        session = self.session_factory()
        try:
            record = RadarBuildRepository(session).find_by_id(run_id)
            return dict(record.payload) if record is not None else None
        finally:
            session.close()


def _is_idempotence_race(error: DBAPIError) -> bool:
    """Retry a serialization conflict or this table's idempotence unique conflict."""
    original = error.orig
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if sqlstate == "40001":
        return True
    diagnostic = getattr(original, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    return sqlstate == "23505" and constraint_name == "uq_radar_builds_idempotence_key"
