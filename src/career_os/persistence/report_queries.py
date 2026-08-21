"""Read-only canonical-job evidence used by reconciled reports."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from career_os.persistence.models import JobPosting


class ReportEvidenceConsistencyError(RuntimeError):
    """The requested report selection cannot be reconciled to stored jobs."""


@dataclass(frozen=True, slots=True)
class ReportJobEvidence:
    """The small stored-job projection needed for representative links."""

    job_id: UUID
    company_name: str
    title: str
    source_url: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, UUID):
            raise ValueError("job_id must be a UUID")
        for value, field in ((self.company_name, "company_name"), (self.title, "title")):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be non-blank")
        if self.source_url is not None:
            if not isinstance(self.source_url, str):
                raise ValueError("source_url must be a string or None")
            try:
                parsed = urlsplit(self.source_url)
                hostname = parsed.hostname
                _ = parsed.port
            except ValueError as error:
                raise ValueError(
                    "source_url must be a safe absolute HTTP(S) URL or None"
                ) from error
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.netloc
                or hostname is None
                or parsed.username is not None
                or parsed.password is not None
                or "\\" in self.source_url
                or any(
                    character.isspace() or ord(character) < 32 or ord(character) == 127
                    for character in self.source_url
                )
            ):
                raise ValueError("source_url must be a safe absolute HTTP(S) URL or None")


@dataclass(slots=True)
class ReportEvidenceRepository:
    """Fetch a stable projection and fail closed if the selection changes."""

    session: Session

    def get_jobs(self, job_ids: tuple[UUID, ...]) -> tuple[ReportJobEvidence, ...]:
        if not job_ids:
            return ()
        rows = tuple(
            self.session.execute(
                select(
                    JobPosting.id,
                    JobPosting.company_name,
                    JobPosting.title_raw,
                    JobPosting.source_url,
                )
                .where(JobPosting.id.in_(job_ids))
                .order_by(JobPosting.id)
            )
        )
        evidence = tuple(
            ReportJobEvidence(
                job_id=job_id,
                company_name=company_name,
                title=title,
                source_url=source_url,
            )
            for job_id, company_name, title, source_url in rows
        )
        if tuple(item.job_id for item in evidence) != job_ids:
            raise ReportEvidenceConsistencyError(
                "market selection changed while report evidence was read"
            )
        return evidence
