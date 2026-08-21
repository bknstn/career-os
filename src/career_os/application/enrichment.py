"""Transactional application service for deterministic offline skill enrichment."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from career_os.db import session_scope
from career_os.normalization.skills import SkillExtractor
from career_os.persistence.models import JobPosting
from career_os.persistence.repositories import (
    JobNotFoundError,
    JobSkillRepository,
    SkillEvidenceReplaceResult,
)

type SessionFactory = sessionmaker[Session]


@dataclass(slots=True)
class SkillEnrichmentService:
    """Reprocess a stored canonical job without adapter or network access."""

    session_factory: SessionFactory
    extractor: SkillExtractor

    def enrich_job(self, *, job_id: UUID) -> SkillEvidenceReplaceResult:
        """Extract current title/description evidence and replace it atomically."""
        with session_scope(self.session_factory) as session:
            return self.enrich_job_in_session(session=session, job_id=job_id)

    def enrich_job_in_session(
        self, *, session: Session, job_id: UUID
    ) -> SkillEvidenceReplaceResult:
        """Apply enrichment inside a caller-owned transaction for batch workflows."""
        job = session.scalar(select(JobPosting).where(JobPosting.id == job_id).with_for_update())
        if job is None:
            raise JobNotFoundError(f"canonical job not found: {job_id}")
        evidence = self.extractor.extract(title=job.title_raw, description=job.description_text)
        return JobSkillRepository(session).replace_deterministic_evidence(
            job_id=job.id,
            evidence=evidence,
            taxonomy_version=self.extractor.taxonomy_version,
        )
