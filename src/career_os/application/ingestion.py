"""Source-neutral ingestion from raw evidence to durable canonical job state."""

from __future__ import annotations

import logging as stdlib_logging
from dataclasses import dataclass, replace
from typing import Protocol, TypeVar
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from career_os.application.collect import (
    CollectionFailedError,
    CollectionProcessingError,
    CollectionResult,
    CollectionRun,
    CollectionRunStatus,
    CollectionService,
)
from career_os.application.lifecycle import LifecycleAction, LifecycleResult
from career_os.config.sources import GreenhouseSourceConfig, SourceConfig
from career_os.db import session_scope
from career_os.domain import CanonicalJobCandidate, RawJobPosting, SourceAdapter
from career_os.domain.runs import RunCounts
from career_os.normalization import html_to_text
from career_os.persistence.repositories import (
    CanonicalJobRepository,
    JobUpsertAction,
    SourceLifecycleRepository,
    SqlAlchemyCollectionRunStore,
)

type SessionFactory = sessionmaker[Session]
ConfigT = TypeVar("ConfigT", bound=SourceConfig, contravariant=True)

GREENHOUSE_NORMALIZER_VERSION = "greenhouse-v1"
NO_TAXONOMY_VERSION = "none-v1"


class IngestionError(RuntimeError):
    """A raw source record cannot safely become a canonical job candidate."""


class CandidateFactory(Protocol[ConfigT]):
    """Convert source-native evidence into a validated canonical candidate."""

    def __call__(self, source_config: ConfigT, posting: RawJobPosting) -> CanonicalJobCandidate:
        """Create one candidate or fail without mutating persistence."""


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Collection state plus durable job and lifecycle effects of one scan."""

    collection: CollectionResult
    counts: RunCounts
    lifecycle: LifecycleResult
    collection_failed: bool


def _counts_with_processing(
    base: RunCounts, *, inserted: int, updated: int, unchanged: int, inactivated: int = 0
) -> RunCounts:
    """Preserve source telemetry while adding canonical and lifecycle counters."""
    return RunCounts(
        items_fetched=base.items_fetched,
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        inactivated=inactivated,
        failed_items=base.failed_items,
        http_requests=base.http_requests,
        retry_count=base.retry_count,
        rate_limit_events=base.rate_limit_events,
        normalization_errors=base.normalization_errors,
    )


@dataclass(slots=True)
class _TerminalIngestionProcessor[ProcessorConfigT: SourceConfig]:
    """Shared transaction policy for complete and unsafe scan results.

    Source modules provide configuration and a candidate mapper only. Partial
    runs retain useful newer evidence but do not infer absence; complete runs
    add source-local reconciliation.
    """

    session_factory: SessionFactory
    run_store: SqlAlchemyCollectionRunStore
    source_config: ProcessorConfigT
    candidate_factory: CandidateFactory[ProcessorConfigT]

    def __call__(self, run: CollectionRun, postings: tuple[RawJobPosting, ...]) -> CollectionRun:
        inserted = updated = unchanged = 0
        with session_scope(self.session_factory) as session:
            jobs = CanonicalJobRepository(session)
            for posting in postings:
                try:
                    candidate = self.candidate_factory(self.source_config, posting)
                except Exception as error:
                    raise CollectionProcessingError(
                        "canonical candidate mapping failed", normalization_errors=1
                    ) from error
                result = jobs.upsert(candidate)
                if result.action is JobUpsertAction.INSERTED:
                    inserted += 1
                elif result.action is JobUpsertAction.UPDATED:
                    updated += 1
                else:
                    unchanged += 1
            counted_run = replace(
                run,
                counts=_counts_with_processing(
                    run.counts, inserted=inserted, updated=updated, unchanged=unchanged
                ),
            )
            self.run_store.update_in_session(session, counted_run)
            inactivated = 0
            if run.status is CollectionRunStatus.SUCCEEDED:
                inactivated = SourceLifecycleRepository(session).inactivate_unseen(
                    run=counted_run,
                    seen_external_ids=tuple(sorted({p.external_id for p in postings})),
                )
        return replace(
            counted_run,
            counts=_counts_with_processing(
                counted_run.counts,
                inserted=counted_run.counts.inserted,
                updated=counted_run.counts.updated,
                unchanged=counted_run.counts.unchanged,
                inactivated=inactivated,
            ),
        )


@dataclass(slots=True)
class IngestionService[ServiceConfigT: SourceConfig]:
    """Compose source-neutral collection, transaction, and lifecycle policy."""

    session_factory: SessionFactory
    logger: stdlib_logging.Logger
    request_timeout_seconds: float
    normalizer_version: str
    taxonomy_version: str
    candidate_factory: CandidateFactory[ServiceConfigT]

    async def collect(
        self, *, source_config: ServiceConfigT, adapter: SourceAdapter
    ) -> IngestionResult:
        run_store = SqlAlchemyCollectionRunStore(
            session_factory=self.session_factory,
            normalizer_version=self.normalizer_version,
            taxonomy_version=self.taxonomy_version,
        )
        collector = CollectionService(
            run_store=run_store,
            logger=self.logger,
            run_id_factory=lambda: str(uuid4()),
            timeout_seconds=self.request_timeout_seconds,
            terminal_run_processor=_TerminalIngestionProcessor(
                session_factory=self.session_factory,
                run_store=run_store,
                source_config=source_config,
                candidate_factory=self.candidate_factory,
            ),
        )
        try:
            collection = await collector.collect(source_key=source_config.key, adapter=adapter)
            collection_failed = False
        except CollectionFailedError as error:
            collection = error.result
            collection_failed = True
        seen_external_ids = tuple(sorted({posting.external_id for posting in collection.postings}))
        if collection.is_complete_success:
            lifecycle = LifecycleResult(
                action=LifecycleAction.INACTIVATED,
                source_key=collection.run.source_key,
                seen_external_ids=seen_external_ids,
                inactivated_count=collection.run.counts.inactivated,
            )
        else:
            lifecycle = LifecycleResult(
                action=LifecycleAction.SKIPPED_UNSAFE,
                source_key=collection.run.source_key,
                seen_external_ids=seen_external_ids,
                inactivated_count=0,
            )
        return IngestionResult(
            collection=collection,
            counts=collection.run.counts,
            lifecycle=lifecycle,
            collection_failed=collection_failed,
        )


def greenhouse_candidate(
    source_config: GreenhouseSourceConfig, posting: RawJobPosting
) -> CanonicalJobCandidate:
    """Map only Greenhouse-supported fields; retain unsupported evidence raw."""
    if posting.source_kind != "greenhouse" or posting.source_key != source_config.key:
        raise IngestionError("raw posting identity does not match the configured Greenhouse source")
    title = posting.payload.get("title")
    content = posting.payload.get("content")
    if not isinstance(title, str) or not title.strip():
        raise IngestionError("Greenhouse raw posting has no non-blank title")
    if not isinstance(content, str) or not html_to_text(content):
        raise IngestionError("Greenhouse raw posting has no non-blank description content")
    return CanonicalJobCandidate(
        source_key=source_config.key,
        source_kind=source_config.kind,
        external_id=posting.external_id,
        company_name=source_config.company_name,
        title_raw=title,
        title_normalized=title,
        description_html=content,
        description_text=html_to_text(content),
        department_raw=_first_name(posting.payload.get("departments")),
        team_raw=None,
        source_url=posting.source_url,
        apply_url=posting.apply_url,
        observed_at=posting.fetched_at,
        raw_payload=posting.payload,
        normalizer_version=GREENHOUSE_NORMALIZER_VERSION,
        taxonomy_version=NO_TAXONOMY_VERSION,
    )


def _first_name(value: object) -> str | None:
    if not isinstance(value, list) or not value:
        return None
    first = value[0]
    if not isinstance(first, dict):
        return None
    name = first.get("name")
    return name if isinstance(name, str) and name.strip() else None


@dataclass(slots=True)
class GreenhouseIngestionService:
    """Compatibility composition; no Greenhouse transaction policy lives here."""

    session_factory: SessionFactory
    logger: stdlib_logging.Logger
    request_timeout_seconds: float
    candidate_factory: CandidateFactory[GreenhouseSourceConfig] = greenhouse_candidate
    normalizer_version: str = GREENHOUSE_NORMALIZER_VERSION
    taxonomy_version: str = NO_TAXONOMY_VERSION

    async def collect(
        self, *, source_config: GreenhouseSourceConfig, adapter: SourceAdapter
    ) -> IngestionResult:
        return await IngestionService(
            session_factory=self.session_factory,
            logger=self.logger,
            request_timeout_seconds=self.request_timeout_seconds,
            normalizer_version=self.normalizer_version,
            taxonomy_version=self.taxonomy_version,
            candidate_factory=self.candidate_factory,
        ).collect(source_config=source_config, adapter=adapter)
