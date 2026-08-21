"""Read-only projections and keyset queries for the local HTTP API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from career_os.domain.normalization import RoleFamily, Seniority
from career_os.persistence.models import (
    CollectionRun,
    JobCompensation,
    JobLocation,
    JobPosting,
    JobSkill,
    JobVersion,
    Source,
)
from career_os.persistence.repositories import JobInspection, RunInspection


@dataclass(frozen=True, slots=True)
class JobCursorKey:
    last_seen_at: datetime
    job_id: UUID


@dataclass(frozen=True, slots=True)
class JobQueryFilters:
    """Transport-neutral canonical job-list filters."""

    status: Literal["active", "inactive"] | None = None
    company: str | None = None
    role_family: RoleFamily | None = None
    seniority: Seniority | None = None
    country_code: str | None = None
    skill: str | None = None


@dataclass(frozen=True, slots=True)
class JobDetail:
    inspection: JobInspection
    source_id: UUID
    locations: tuple[JobLocation, ...]
    skills: tuple[JobSkill, ...]
    compensation: tuple[JobCompensation, ...]
    versions: tuple[JobVersion, ...]


@dataclass(slots=True)
class ApiReadRepository:
    session: Session

    def list_sources(self) -> tuple[Source, ...]:
        return tuple(self.session.scalars(select(Source).order_by(Source.key, Source.id)))

    def list_runs(self) -> tuple[RunInspection, ...]:
        rows = self.session.execute(
            select(CollectionRun, Source.key, Source.kind)
            .join(Source, CollectionRun.source_id == Source.id)
            .order_by(CollectionRun.started_at.desc(), CollectionRun.id)
        )
        return tuple(
            RunInspection(run=row[0], source_key=row[1], source_kind=row[2]) for row in rows
        )

    def get_run(self, run_id: UUID) -> RunInspection | None:
        row = self.session.execute(
            select(CollectionRun, Source.key, Source.kind)
            .join(Source, CollectionRun.source_id == Source.id)
            .where(CollectionRun.id == run_id)
        ).one_or_none()
        return (
            None
            if row is None
            else RunInspection(run=row[0], source_key=row[1], source_kind=row[2])
        )

    def list_jobs(
        self,
        *,
        filters: JobQueryFilters,
        after: JobCursorKey | None,
        limit: int,
    ) -> tuple[JobInspection, ...]:
        statement = select(JobPosting, Source.key, Source.kind).join(
            Source, JobPosting.source_id == Source.id
        )
        if filters.status is not None:
            statement = statement.where(JobPosting.status == filters.status)
        if filters.company is not None:
            statement = statement.where(JobPosting.company_name == filters.company)
        if filters.role_family is not None:
            statement = statement.where(JobPosting.role_family == filters.role_family)
        if filters.seniority is not None:
            statement = statement.where(JobPosting.seniority == filters.seniority)
        if filters.country_code is not None:
            statement = statement.where(
                select(JobLocation.job_id)
                .where(
                    JobLocation.job_id == JobPosting.id,
                    JobLocation.country_code == filters.country_code,
                )
                .exists()
            )
        if filters.skill is not None:
            statement = statement.where(
                select(JobSkill.job_id)
                .where(JobSkill.job_id == JobPosting.id, JobSkill.skill_id == filters.skill)
                .exists()
            )
        if after is not None:
            statement = statement.where(
                or_(
                    JobPosting.last_seen_at < after.last_seen_at,
                    and_(
                        JobPosting.last_seen_at == after.last_seen_at,
                        JobPosting.id > after.job_id,
                    ),
                )
            )
        rows = self.session.execute(
            statement.order_by(JobPosting.last_seen_at.desc(), JobPosting.id).limit(limit)
        )
        return tuple(
            JobInspection(job=row[0], source_key=row[1], source_kind=row[2]) for row in rows
        )

    def get_job(self, job_id: UUID) -> JobDetail | None:
        row = self.session.execute(
            select(JobPosting, Source.id, Source.key, Source.kind)
            .join(Source, JobPosting.source_id == Source.id)
            .where(JobPosting.id == job_id)
        ).one_or_none()
        if row is None:
            return None
        return JobDetail(
            inspection=JobInspection(job=row[0], source_key=row[2], source_kind=row[3]),
            source_id=row[1],
            locations=tuple(
                self.session.scalars(
                    select(JobLocation)
                    .where(JobLocation.job_id == job_id)
                    .order_by(JobLocation.ordinal)
                )
            ),
            skills=tuple(
                self.session.scalars(
                    select(JobSkill)
                    .where(JobSkill.job_id == job_id)
                    .order_by(
                        JobSkill.skill_id,
                        JobSkill.section.nulls_first(),
                        JobSkill.matched_text,
                        JobSkill.method,
                        JobSkill.id,
                    )
                )
            ),
            compensation=tuple(
                self.session.scalars(
                    select(JobCompensation)
                    .where(JobCompensation.job_id == job_id)
                    .order_by(
                        JobCompensation.component_type,
                        JobCompensation.currency.nulls_first(),
                        JobCompensation.interval.nulls_first(),
                        JobCompensation.location_tier.nulls_first(),
                        JobCompensation.id,
                    )
                )
            ),
            versions=tuple(
                self.session.scalars(
                    select(JobVersion)
                    .where(JobVersion.job_id == job_id)
                    .order_by(JobVersion.observed_at, JobVersion.id)
                )
            ),
        )
