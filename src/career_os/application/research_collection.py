"""Bounded research-document discovery, acquisition, and provenance recording."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from career_os.adapters.research_http import (
    RESEARCH_USER_AGENT,
    ResearchFetcher,
    ResearchFetchError,
    ResearchFetchResult,
    ResearchRequestBudget,
)
from career_os.application.research_discovery import parse_visible_index_links, relevant_candidates
from career_os.config.research_sources import ResearchSource, ResearchSourceRegistry
from career_os.domain.research import (
    ResearchDataPeriod,
    ResearchDateState,
    ResearchMediaType,
    ResearchOriginalVersion,
    ResearchVersionRelation,
    normalize_research_url,
)
from career_os.persistence.models import (
    ResearchAcquisitionAttempt,
    ResearchCandidate,
    ResearchCollectionRun,
    ResearchDocument,
    ResearchDocumentVersion,
)
from career_os.persistence.research import ResearchRepository
from career_os.research.extraction import ResearchExtraction, extract_research_document

ResearchTrigger = Literal["daily", "manual", "backfill"]
MAX_RUN_BODY_BYTES = 64 * 1024 * 1024
MAX_WIRE_REQUESTS = 96
MAX_DOCUMENTS_PER_RUN = 32
MAX_DAILY_DOCUMENTS_PER_RUN = 12
MAX_INDEX_CANDIDATES_PER_SOURCE = 8
RUN_DEADLINE_SECONDS = 15 * 60
UNFETCHED_RETRY_INTERVAL = timedelta(days=1)
ACQUIRED_REVISIT_INTERVAL = timedelta(days=7)
ROBOTS_USER_AGENT = RESEARCH_USER_AGENT.partition("/")[0]
EXTRACTOR_FAILURE_TITLE = "Title not reported in source"


@dataclass(frozen=True, slots=True)
class ResearchCollectionResult:
    """Compact run summary suitable for the CLI and durable checkpoint."""

    run_id: UUID
    status: str
    candidate_urls_seen: int
    new_candidates: int
    acquired: int
    new_documents: int
    new_versions: int
    equivalent_versions: int
    material_revisions: int
    unchanged: int
    failed: int
    body_bytes: int
    wire_requests: int


@dataclass(slots=True)
class _RunCounts:
    candidate_urls_seen: int = 0
    new_candidates: int = 0
    acquired: int = 0
    new_documents: int = 0
    new_versions: int = 0
    equivalent_versions: int = 0
    material_revisions: int = 0
    unchanged: int = 0
    failed: int = 0
    body_bytes: int = 0
    documents_considered: int = 0
    seen_candidates: set[tuple[str, str]] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class ScheduledResearchCandidate:
    source_key: str
    canonical_url: str
    discovered_url: str
    title: str | None
    has_version: bool
    last_attempt_at: datetime | None


@dataclass(frozen=True, slots=True)
class _RobotsRule:
    allow: bool
    pattern: str
    specificity: int


@dataclass(frozen=True, slots=True)
class ResearchRobotsPolicy:
    rules: tuple[_RobotsRule, ...] | None
    allow_all: bool
    outcome: str
    error_code: str | None
    http_status: int | None

    def decision(self, target_url: str) -> tuple[bool, str, str | None, int | None]:
        if self.allow_all:
            return True, "discovered", self.error_code, self.http_status
        if self.rules is None:
            return False, self.outcome, self.error_code, self.http_status
        try:
            allowed = _robots_path_allowed(self.rules, target_url)
        except (ValueError, UnicodeError):
            return False, "parse_error", "robots_policy_evaluation_error", self.http_status
        if allowed:
            return True, "discovered", "robots_policy_allowed", self.http_status
        return False, "blocked", "robots_disallowed", self.http_status


def fair_due_candidates(
    candidates: Iterable[ScheduledResearchCandidate],
    source_order: tuple[str, ...],
    *,
    now: datetime,
    limit: int,
) -> tuple[ScheduledResearchCandidate, ...]:
    """Choose due work without allowing one publisher's backlog to starve others."""
    if limit < 1 or not source_order:
        return ()
    queues: dict[str, list[ScheduledResearchCandidate]] = {key: [] for key in source_order}
    for candidate in candidates:
        if candidate.source_key not in queues:
            continue
        interval = ACQUIRED_REVISIT_INTERVAL if candidate.has_version else UNFETCHED_RETRY_INTERVAL
        if candidate.last_attempt_at is not None and now - candidate.last_attempt_at < interval:
            continue
        queues[candidate.source_key].append(candidate)
    for queue in queues.values():
        queue.sort(
            key=lambda item: (
                item.has_version,
                item.last_attempt_at is not None,
                item.last_attempt_at or datetime.min.replace(tzinfo=UTC),
                item.canonical_url,
            )
        )
    offset = now.date().toordinal() % len(source_order)
    rotated = source_order[offset:] + source_order[:offset]
    selected: list[ScheduledResearchCandidate] = []
    while len(selected) < limit:
        found = False
        for source_key in rotated:
            if queues[source_key]:
                selected.append(queues[source_key].pop(0))
                found = True
                if len(selected) == limit:
                    break
        if not found:
            break
    return tuple(selected)


class ResearchCollectionService:
    """Run allowlisted discovery with short DB transactions around network I/O."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        registry: ResearchSourceRegistry,
        *,
        fetcher: ResearchFetcher | None = None,
        maximum_body_bytes: int = MAX_RUN_BODY_BYTES,
        maximum_wire_requests: int = MAX_WIRE_REQUESTS,
        maximum_documents: int = MAX_DOCUMENTS_PER_RUN,
        deadline_seconds: float = RUN_DEADLINE_SECONDS,
    ) -> None:
        if maximum_body_bytes < 1 or maximum_wire_requests < 1 or maximum_documents < 1:
            raise ValueError("research collection limits must be positive")
        if deadline_seconds <= 0:
            raise ValueError("research collection deadline must be positive")
        self.session_factory = session_factory
        self.registry = registry
        self.request_budget = (
            fetcher.request_budget
            if fetcher is not None
            else ResearchRequestBudget(maximum_wire_requests)
        )
        if fetcher is not None and fetcher.request_budget.maximum > maximum_wire_requests:
            raise ValueError("injected fetcher exceeds the configured run request limit")
        self.fetcher = fetcher or ResearchFetcher(request_budget=self.request_budget)
        self.maximum_body_bytes = maximum_body_bytes
        self.maximum_documents = maximum_documents
        self.deadline_seconds = deadline_seconds
        self._robots_cache: dict[str, ResearchRobotsPolicy] = {}

    def collect(
        self,
        *,
        trigger: ResearchTrigger = "manual",
        source_keys: Iterable[str] | None = None,
    ) -> ResearchCollectionResult:
        """Run a one-time fixed-seed backfill or fair due-candidate discovery."""
        if trigger not in {"daily", "manual", "backfill"}:
            raise ValueError("unsupported research collection trigger")
        sources = self._selected_sources(source_keys)
        # One service object may be reused by a scheduler process; each durable
        # collection run receives a fresh wire-request budget.
        self.request_budget.used = 0
        run_id = self._start_run(trigger)
        counts = _RunCounts()
        self._robots_cache.clear()
        deadline = time.monotonic() + self.deadline_seconds
        seen_urls: set[tuple[str, str]] = set()
        fetched_html: dict[tuple[str, str], tuple[ResearchFetchResult, ResearchExtraction]] = {}
        document_limit = min(
            self.maximum_documents,
            MAX_DOCUMENTS_PER_RUN if trigger == "backfill" else MAX_DAILY_DOCUMENTS_PER_RUN,
        )
        try:
            # A bounded one-time backfill seeds the verified corpus. Normal daily
            # runs discover first and revisit due candidates; they do not redownload
            # all twenty fixed seed documents every day.
            if trigger == "backfill":
                for source in sources:
                    for seed_url in source.candidate_document_urls:
                        self._acquire_document(
                            run_id,
                            source,
                            seed_url,
                            discovered_url=seed_url,
                            title=None,
                            deadline=deadline,
                            counts=counts,
                            seen_urls=seen_urls,
                            fetched_html=fetched_html,
                            robots_cache=self._robots_cache,
                            document_limit=document_limit,
                        )
            for source in sources:
                self._discover_source(
                    run_id,
                    source,
                    deadline=deadline,
                    counts=counts,
                    seen_urls=seen_urls,
                    fetched_html=fetched_html,
                    robots_cache=self._robots_cache,
                )
            self._retry_stored_candidates(
                run_id,
                sources,
                deadline=deadline,
                counts=counts,
                seen_urls=seen_urls,
                fetched_html=fetched_html,
                robots_cache=self._robots_cache,
                document_limit=document_limit,
            )
            status = (
                "failed"
                if counts.failed and not counts.acquired
                else ("partial" if counts.failed else "succeeded")
            )
            self._finish_run(run_id, counts, status)
        except BaseException:
            self._finish_run(run_id, counts, "failed")
            raise
        return ResearchCollectionResult(
            run_id=run_id,
            status=status,
            candidate_urls_seen=counts.candidate_urls_seen,
            new_candidates=counts.new_candidates,
            acquired=counts.acquired,
            new_documents=counts.new_documents,
            new_versions=counts.new_versions,
            equivalent_versions=counts.equivalent_versions,
            material_revisions=counts.material_revisions,
            unchanged=counts.unchanged,
            failed=counts.failed,
            body_bytes=counts.body_bytes,
            wire_requests=self.request_budget.used,
        )

    def add_manual_candidate(self, source_key: str, value: str, title: str | None = None) -> UUID:
        """Persist one operator-selected official URL without fetching it."""
        source = self.registry.get(source_key)
        if not source.enabled:
            raise ValueError("disabled sources cannot be manually seeded")
        discovered = normalize_research_url(value, allowed_hosts=tuple(source.allowed_hosts))
        canonical = self.registry.canonical_document_url(source_key, discovered)
        with self.session_factory.begin() as session:
            candidate = ResearchRepository(session, self.registry).upsert_candidate(
                source_key=source_key,
                discovered_url=discovered,
                canonical_url=canonical,
                title=title,
            )
            return candidate.id

    def _selected_sources(self, source_keys: Iterable[str] | None) -> tuple[ResearchSource, ...]:
        if source_keys is None:
            requested_sources = self.registry.enabled_sources
        else:
            requested = tuple(dict.fromkeys(source_keys))
            if not requested:
                raise ValueError("at least one source must be selected")
            requested_sources = tuple(self.registry.get(key) for key in requested)
        if not requested_sources:
            raise ValueError("at least one source must be selected")
        selected: list[ResearchSource] = []
        for source in requested_sources:
            if not source.enabled:
                raise ValueError(f"disabled research source cannot be collected: {source.key}")
            unsupported_methods = set(source.discovery_methods) - {"html_index", "single_document"}
            if unsupported_methods:
                raise ValueError(
                    f"research discovery methods are not implemented for {source.key}: "
                    f"{', '.join(sorted(unsupported_methods))}"
                )
            selected.append(source)
        return tuple(selected)

    def _start_run(self, trigger: ResearchTrigger) -> UUID:
        row = ResearchCollectionRun(
            registry_version=self.registry.version,
            registry_digest=self.registry.digest,
            registry_snapshot=self.registry.document.model_dump(mode="json"),
            trigger=trigger,
            status="running",
        )
        with self.session_factory.begin() as session:
            session.add(row)
            session.flush()
            return row.id

    def _finish_run(self, run_id: UUID, counts: _RunCounts, status: str) -> None:
        with self.session_factory.begin() as session:
            session.execute(
                update(ResearchCollectionRun)
                .where(ResearchCollectionRun.id == run_id)
                .values(
                    status=status,
                    finished_at=datetime.now(UTC),
                    discovered=counts.new_candidates,
                    acquired=counts.acquired,
                    failed=counts.failed,
                )
            )

    def _retry_stored_candidates(
        self,
        run_id: UUID,
        sources: tuple[ResearchSource, ...],
        *,
        deadline: float,
        counts: _RunCounts,
        seen_urls: set[tuple[str, str]],
        fetched_html: dict[tuple[str, str], tuple[ResearchFetchResult, ResearchExtraction]],
        robots_cache: dict[str, ResearchRobotsPolicy],
        document_limit: int,
    ) -> None:
        source_by_key = {source.key: source for source in sources}
        with self.session_factory() as session:
            latest_versions = (
                select(
                    ResearchDocumentVersion.document_id.label("document_id"),
                    func.max(ResearchDocumentVersion.version_number).label("latest_version"),
                )
                .group_by(ResearchDocumentVersion.document_id)
                .subquery()
            )
            latest_attempts = (
                select(
                    ResearchAcquisitionAttempt.source_key.label("source_key"),
                    ResearchAcquisitionAttempt.requested_url.label("requested_url"),
                    func.max(ResearchAcquisitionAttempt.occurred_at).label("last_attempt_at"),
                )
                .where(ResearchAcquisitionAttempt.outcome != "discovered")
                .group_by(
                    ResearchAcquisitionAttempt.source_key,
                    ResearchAcquisitionAttempt.requested_url,
                )
                .subquery()
            )
            statement = (
                select(
                    ResearchCandidate.source_key,
                    ResearchCandidate.canonical_url,
                    ResearchCandidate.discovered_url,
                    ResearchCandidate.title,
                    latest_versions.c.latest_version,
                    latest_attempts.c.last_attempt_at,
                )
                .outerjoin(
                    ResearchDocument,
                    (ResearchDocument.source_key == ResearchCandidate.source_key)
                    & (ResearchDocument.canonical_url == ResearchCandidate.canonical_url),
                )
                .outerjoin(
                    latest_versions,
                    latest_versions.c.document_id == ResearchDocument.id,
                )
                .outerjoin(
                    latest_attempts,
                    (latest_attempts.c.source_key == ResearchCandidate.source_key)
                    & (latest_attempts.c.requested_url == ResearchCandidate.canonical_url),
                )
                .where(
                    ResearchCandidate.source_key.in_(tuple(source_by_key)),
                    ResearchCandidate.state != "ignored",
                )
            )
            rows = session.execute(statement).all()
        candidates = tuple(
            ScheduledResearchCandidate(
                source_key=row.source_key,
                canonical_url=row.canonical_url,
                discovered_url=row.discovered_url,
                title=row.title,
                has_version=row.latest_version is not None,
                last_attempt_at=row.last_attempt_at,
            )
            for row in rows
            if (row.source_key, row.canonical_url) not in seen_urls
        )
        due = fair_due_candidates(
            candidates,
            tuple(source_by_key),
            now=datetime.now(UTC),
            limit=max(0, document_limit - counts.documents_considered),
        )
        for candidate in due:
            source = source_by_key[candidate.source_key]
            self._acquire_document(
                run_id,
                source,
                candidate.canonical_url,
                discovered_url=candidate.discovered_url,
                title=candidate.title,
                deadline=deadline,
                counts=counts,
                seen_urls=seen_urls,
                fetched_html=fetched_html,
                robots_cache=robots_cache,
                document_limit=document_limit,
            )
            if counts.documents_considered >= document_limit or time.monotonic() >= deadline:
                return

    def _acquire_document(
        self,
        run_id: UUID,
        source: ResearchSource,
        value: str,
        *,
        discovered_url: str,
        title: str | None,
        deadline: float,
        counts: _RunCounts,
        seen_urls: set[tuple[str, str]],
        fetched_html: dict[tuple[str, str], tuple[ResearchFetchResult, ResearchExtraction]],
        robots_cache: dict[str, ResearchRobotsPolicy],
        document_limit: int,
    ) -> None:
        canonical = self.registry.canonical_document_url(source.key, value)
        identity = (source.key, canonical)
        if identity in seen_urls:
            return
        seen_urls.add(identity)
        self._mark_candidate_seen(identity, counts)
        if counts.documents_considered >= document_limit:
            self._record_attempt(
                run_id,
                source.key,
                canonical,
                outcome="blocked",
                error_code="run_document_limit",
            )
            counts.failed += 1
            return
        counts.documents_considered += 1
        candidate_id, is_new_candidate = self._upsert_candidate(
            source.key, discovered_url, canonical, title
        )
        if is_new_candidate:
            counts.new_candidates += 1
        if not self._robots_allowed(
            run_id,
            source,
            canonical,
            deadline=deadline,
            counts=counts,
            cache=robots_cache,
        ):
            _, outcome, error_code, http_status = robots_cache[
                urlsplit(canonical).hostname or ""
            ].decision(canonical)
            counts.failed += 1
            self._record_attempt(
                run_id,
                source.key,
                canonical,
                outcome=outcome,
                error_code=error_code,
                http_status=http_status,
            )
            return
        remaining_bytes = self.maximum_body_bytes - counts.body_bytes
        maximum_bytes = min(source.max_response_bytes, 50 * 1024 * 1024, remaining_bytes)
        try:
            response = self.fetcher.fetch(
                source,
                canonical,
                maximum_bytes=maximum_bytes,
                deadline=deadline,
            )
        except ResearchFetchError as error:
            counts.body_bytes += error.bytes_received
            counts.failed += 1
            self._record_attempt(
                run_id,
                source.key,
                canonical,
                outcome=error.outcome,
                error_code=error.error_code,
                http_status=error.http_status,
                bytes_received=error.bytes_received,
            )
            return
        counts.body_bytes += response.bytes_received
        if response.media_type is None:
            counts.failed += 1
            self._record_attempt(
                run_id,
                source.key,
                canonical,
                outcome="unsupported_content",
                error_code="document_not_html_or_pdf",
                http_status=response.status,
                bytes_received=response.bytes_received,
            )
            return
        extraction = extract_research_document(
            response.body, response.media_type, charset=response.charset
        )
        fetched_html[identity] = (response, extraction)
        version = self._original_version(
            source,
            canonical,
            discovered_url,
            response,
            extraction,
        )
        with self.session_factory.begin() as session:
            result = ResearchRepository(session, self.registry).append_version(version)
            session.execute(
                update(ResearchCandidate)
                .where(ResearchCandidate.id == candidate_id)
                .values(state="acquired")
            )
            self._record_attempt_in_session(
                session,
                run_id,
                source.key,
                canonical,
                outcome="parse_error" if extraction.error_code else "acquired",
                http_status=response.status,
                bytes_received=response.bytes_received,
                error_code=extraction.error_code,
            )
        counts.acquired += 1
        if result.inserted:
            counts.new_versions += 1
            if result.version.version_number == 1:
                counts.new_documents += 1
            if result.version.version_relation == ResearchVersionRelation.EQUIVALENT_TEXT.value:
                counts.equivalent_versions += 1
            elif (
                result.version.version_relation == ResearchVersionRelation.REVISION.value
                and result.version.normalized_text_sha256 is not None
            ):
                counts.material_revisions += 1
        else:
            counts.unchanged += 1
        if extraction.error_code:
            counts.failed += 1

    def _upsert_candidate(
        self, source_key: str, discovered_url: str, canonical_url: str, title: str | None
    ) -> tuple[UUID, bool]:
        with self.session_factory.begin() as session:
            existing = session.scalar(
                select(ResearchCandidate.id).where(
                    ResearchCandidate.source_key == source_key,
                    ResearchCandidate.canonical_url == canonical_url,
                )
            )
            candidate = ResearchRepository(session, self.registry).upsert_candidate(
                source_key=source_key,
                discovered_url=discovered_url,
                canonical_url=canonical_url,
                title=title,
            )
            return candidate.id, existing is None

    @staticmethod
    def _mark_candidate_seen(identity: tuple[str, str], counts: _RunCounts) -> None:
        if identity not in counts.seen_candidates:
            counts.seen_candidates.add(identity)
            counts.candidate_urls_seen += 1

    def _original_version(
        self,
        source: ResearchSource,
        canonical: str,
        discovered_url: str,
        response: ResearchFetchResult,
        extraction: ResearchExtraction,
    ) -> ResearchOriginalVersion:
        # Determine version relation under the same locked document row used by
        # append_version; the repository will validate this value transactionally.
        if response.media_type is None:
            raise ValueError("a document version requires HTML or PDF media type")
        with self.session_factory() as session:
            document = session.scalar(
                select(ResearchDocument).where(
                    ResearchDocument.source_key == source.key,
                    ResearchDocument.canonical_url == canonical,
                )
            )
            latest = None
            if document is not None:
                latest = session.scalar(
                    select(ResearchDocumentVersion)
                    .where(ResearchDocumentVersion.document_id == document.id)
                    .order_by(ResearchDocumentVersion.version_number.desc())
                    .limit(1)
                )
            text_digest = _text_digest(extraction.normalized_text)
            if latest is None:
                relation = ResearchVersionRelation.INITIAL
            elif text_digest is not None and latest.normalized_text_sha256 == text_digest:
                relation = ResearchVersionRelation.EQUIVALENT_TEXT
            else:
                relation = ResearchVersionRelation.REVISION
            previous_id = latest.id if latest is not None else None
        return ResearchOriginalVersion(
            id=uuid4(),
            source_key=source.key,
            canonical_url=canonical,
            discovered_url=discovered_url,
            retrieved_url=response.retrieved_url,
            title=extraction.title or EXTRACTOR_FAILURE_TITLE,
            retrieved_at=datetime.now(UTC),
            media_type=response.media_type,
            original_bytes=response.body,
            response_etag=response.etag,
            response_last_modified=response.last_modified,
            publication_date=extraction.publication_date,
            data_period=ResearchDataPeriod(None, None, ResearchDateState.NOT_REPORTED),
            normalized_text=extraction.normalized_text,
            extraction_regions=extraction.regions,
            extraction_version=extraction.extraction_version,
            extraction_error=extraction.error_code,
            relation=relation,
            previous_version_id=previous_id,
        )

    def _discover_source(
        self,
        run_id: UUID,
        source: ResearchSource,
        *,
        deadline: float,
        counts: _RunCounts,
        seen_urls: set[tuple[str, str]],
        fetched_html: dict[tuple[str, str], tuple[ResearchFetchResult, ResearchExtraction]],
        robots_cache: dict[str, ResearchRobotsPolicy],
    ) -> None:
        if "html_index" not in source.discovery_methods:
            return
        remaining_candidate_capacity = MAX_INDEX_CANDIDATES_PER_SOURCE
        for index_url in source.discovery_urls:
            if remaining_candidate_capacity <= 0:
                break
            if (
                time.monotonic() >= deadline
                or self.request_budget.used >= self.request_budget.maximum
            ):
                break
            canonical_index = normalize_research_url(
                index_url, allowed_hosts=tuple(source.allowed_hosts)
            )
            document_identity = (
                source.key,
                self.registry.canonical_document_url(source.key, canonical_index),
            )
            if document_identity in fetched_html and document_identity[1] == canonical_index:
                response, extraction = fetched_html[document_identity]
            else:
                if not self._robots_allowed(
                    run_id,
                    source,
                    canonical_index,
                    deadline=deadline,
                    counts=counts,
                    cache=robots_cache,
                ):
                    _, outcome, error_code, http_status = robots_cache[
                        urlsplit(canonical_index).hostname or ""
                    ].decision(canonical_index)
                    counts.failed += 1
                    self._record_attempt(
                        run_id,
                        source.key,
                        canonical_index,
                        outcome=outcome,
                        error_code=error_code,
                        http_status=http_status,
                    )
                    continue
                remaining_bytes = self.maximum_body_bytes - counts.body_bytes
                if remaining_bytes <= 0:
                    break
                try:
                    response = self.fetcher.fetch(
                        source,
                        canonical_index,
                        maximum_bytes=min(source.max_response_bytes, remaining_bytes),
                        deadline=deadline,
                    )
                except ResearchFetchError as error:
                    counts.body_bytes += error.bytes_received
                    counts.failed += 1
                    self._record_attempt(
                        run_id,
                        source.key,
                        canonical_index,
                        outcome=error.outcome,
                        error_code=error.error_code,
                        http_status=error.http_status,
                        bytes_received=error.bytes_received,
                    )
                    continue
                counts.body_bytes += response.bytes_received
                if response.media_type is not ResearchMediaType.HTML:
                    counts.failed += 1
                    self._record_attempt(
                        run_id,
                        source.key,
                        canonical_index,
                        outcome="unsupported_content",
                        error_code="index_not_html",
                        http_status=response.status,
                        bytes_received=response.bytes_received,
                    )
                    continue
                extraction = extract_research_document(
                    response.body, response.media_type, charset=response.charset
                )
                self._record_attempt(
                    run_id,
                    source.key,
                    canonical_index,
                    outcome="discovered",
                    http_status=response.status,
                    bytes_received=response.bytes_received,
                    error_code=extraction.error_code,
                )
            if response.media_type is not ResearchMediaType.HTML:
                continue
            try:
                html = response.body.decode(response.charset or "utf-8", errors="replace")
            except LookupError:
                html = response.body.decode("utf-8", errors="replace")
            links = parse_visible_index_links(html, base_url=response.retrieved_url)
            with self.session_factory() as session:
                existing_urls = frozenset(
                    session.scalars(
                        select(ResearchCandidate.canonical_url).where(
                            ResearchCandidate.source_key == source.key
                        )
                    ).all()
                )
            candidates = relevant_candidates(
                source,
                links,
                self.registry,
                limit=remaining_candidate_capacity,
                excluded_canonical_urls=existing_urls,
            )
            for candidate in candidates:
                identity = (source.key, candidate.canonical_url)
                if identity in counts.seen_candidates:
                    continue
                self._mark_candidate_seen(identity, counts)
                _, is_new_candidate = self._upsert_candidate(
                    source.key,
                    candidate.discovered_url,
                    candidate.canonical_url,
                    candidate.title,
                )
                if is_new_candidate:
                    counts.new_candidates += 1
            remaining_candidate_capacity -= len(candidates)

    def _robots_allowed(
        self,
        run_id: UUID,
        source: ResearchSource,
        value: str,
        *,
        deadline: float,
        counts: _RunCounts,
        cache: dict[str, ResearchRobotsPolicy],
    ) -> bool:
        """Fetch each exact host policy once, then evaluate every target path separately."""
        hostname = urlsplit(value).hostname
        if hostname is None:
            return False
        cached = cache.get(hostname)
        if cached is not None:
            return cached.decision(value)[0]
        robots_url = f"https://{hostname}/robots.txt"
        remaining_bytes = self.maximum_body_bytes - counts.body_bytes
        policy: ResearchRobotsPolicy
        try:
            response = self.fetcher.fetch(
                source,
                robots_url,
                maximum_bytes=min(64 * 1024, remaining_bytes),
                deadline=deadline,
                allowed_hosts_override=(hostname,),
                accept_header="text/plain",
                allow_text_plain=True,
            )
        except ResearchFetchError as error:
            counts.body_bytes += error.bytes_received
            if error.http_status in {404, 410}:
                policy = ResearchRobotsPolicy(
                    rules=(),
                    allow_all=True,
                    outcome="discovered",
                    error_code="robots_not_found",
                    http_status=error.http_status,
                )
            elif error.http_status == 403:
                policy = ResearchRobotsPolicy(
                    rules=None,
                    allow_all=False,
                    outcome="blocked",
                    error_code="robots_http_403",
                    http_status=error.http_status,
                )
            else:
                policy = ResearchRobotsPolicy(
                    rules=None,
                    allow_all=False,
                    outcome=error.outcome,
                    error_code=f"robots_unavailable_{error.error_code}",
                    http_status=error.http_status,
                )
            cache[hostname] = policy
            self._record_attempt(
                run_id,
                source.key,
                robots_url,
                outcome=policy.outcome,
                error_code=policy.error_code,
                http_status=policy.http_status,
                bytes_received=error.bytes_received,
            )
            return policy.decision(value)[0]
        counts.body_bytes += response.bytes_received
        if response.media_type is not None:
            policy = ResearchRobotsPolicy(
                rules=None,
                allow_all=False,
                outcome="unsupported_content",
                error_code="robots_not_plain_text",
                http_status=response.status,
            )
        else:
            try:
                text = response.body.decode(response.charset or "utf-8")
                rules = parse_robots_rules(text)
                policy = ResearchRobotsPolicy(
                    rules=rules,
                    allow_all=False,
                    outcome="discovered",
                    error_code="robots_policy_loaded",
                    http_status=response.status,
                )
            except (LookupError, ValueError, UnicodeError):
                policy = ResearchRobotsPolicy(
                    rules=None,
                    allow_all=False,
                    outcome="parse_error",
                    error_code="robots_policy_parse_error",
                    http_status=response.status,
                )
        cache[hostname] = policy
        self._record_attempt(
            run_id,
            source.key,
            robots_url,
            outcome=policy.outcome,
            error_code=policy.error_code,
            http_status=policy.http_status,
            bytes_received=response.bytes_received,
        )
        return policy.decision(value)[0]

    def _record_attempt(
        self,
        run_id: UUID,
        source_key: str,
        requested_url: str,
        *,
        outcome: str,
        error_code: str | None = None,
        http_status: int | None = None,
        bytes_received: int = 0,
    ) -> None:
        with self.session_factory.begin() as session:
            self._record_attempt_in_session(
                session,
                run_id,
                source_key,
                requested_url,
                outcome=outcome,
                error_code=error_code,
                http_status=http_status,
                bytes_received=bytes_received,
            )

    @staticmethod
    def _record_attempt_in_session(
        session: Session,
        run_id: UUID,
        source_key: str,
        requested_url: str,
        *,
        outcome: str,
        error_code: str | None = None,
        http_status: int | None = None,
        bytes_received: int = 0,
    ) -> None:
        session.add(
            ResearchAcquisitionAttempt(
                run_id=run_id,
                source_key=source_key,
                requested_url=requested_url,
                outcome=outcome,
                http_status=http_status,
                bytes_received=bytes_received,
                error_code=error_code,
            )
        )


def _text_digest(value: str | None) -> str | None:
    if value is None:
        return None
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_robots_rules(text: str) -> tuple[_RobotsRule, ...]:
    """Parse applicable REP groups and fail closed on unsupported path syntax."""
    groups: list[tuple[tuple[str, ...], tuple[tuple[bool, str], ...]]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []

    def finish_group() -> None:
        nonlocal agents, rules
        if agents:
            groups.append((tuple(agents), tuple(rules)))
        agents = []
        rules = []

    for raw_line in text.splitlines():
        line = raw_line.split("#", maxsplit=1)[0].strip()
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator:
            continue
        directive = name.strip().lower()
        argument = value.strip()
        if directive == "user-agent":
            if rules:
                finish_group()
            if argument:
                agents.append(argument.lower())
            continue
        if directive not in {"allow", "disallow"} or not agents:
            continue
        if directive == "disallow" and not argument:
            continue
        rules.append((directive == "allow", argument))
    finish_group()

    matching: list[tuple[int, tuple[tuple[bool, str], ...]]] = []
    agent_name = ROBOTS_USER_AGENT.lower()
    for group_agents, group_rules in groups:
        for group_agent in group_agents:
            if group_agent == "*":
                matching.append((0, group_rules))
            elif group_agent in agent_name:
                matching.append((len(group_agent), group_rules))
    if not matching:
        return ()
    most_specific = max(score for score, _ in matching)
    raw_rules = tuple(
        rule for score, group_rules in matching if score == most_specific for rule in group_rules
    )
    normalized: list[_RobotsRule] = []
    for allow, argument in raw_rules:
        if not argument or "$" in argument[:-1]:
            raise ValueError("unsupported robots path pattern")
        terminal_anchor = argument.endswith("$")
        raw_pattern = argument[:-1] if terminal_anchor else argument
        if not raw_pattern or not raw_pattern.startswith(("/", "*")):
            raise ValueError("unsupported robots path pattern")
        pattern = _normalize_robots_octets(raw_pattern, allow_wildcards=True)
        if terminal_anchor:
            pattern += "$"
        specificity = _robots_pattern_specificity(pattern.rstrip("$"))
        normalized.append(_RobotsRule(allow=allow, pattern=pattern, specificity=specificity))
    return tuple(normalized)


def _robots_path_allowed(rules: tuple[_RobotsRule, ...], target_url: str) -> bool:
    parsed = urlsplit(target_url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    path = _normalize_robots_octets(path)
    matching = [rule for rule in rules if _robots_glob_matches(rule.pattern, path)]
    if not matching:
        return True
    longest = max(rule.specificity for rule in matching)
    return any(rule.allow for rule in matching if rule.specificity == longest)


def _robots_glob_matches(pattern: str, path: str) -> bool:
    """Match a bounded robots glob; an unanchored rule is a prefix."""
    anchored = pattern.endswith("$")
    value = pattern[:-1] if anchored else pattern
    glob = value if anchored else f"{value}*"
    pattern_index = 0
    path_index = 0
    star_index = -1
    retry_path_index = -1
    while path_index < len(path):
        if pattern_index < len(glob) and (
            glob[pattern_index] == path[path_index] or glob[pattern_index] == "*"
        ):
            if glob[pattern_index] == "*":
                star_index = pattern_index
                retry_path_index = path_index
                pattern_index += 1
            else:
                pattern_index += 1
                path_index += 1
        elif star_index >= 0:
            pattern_index = star_index + 1
            retry_path_index += 1
            path_index = retry_path_index
        else:
            return False
    while pattern_index < len(glob) and glob[pattern_index] == "*":
        pattern_index += 1
    return pattern_index == len(glob)


def _normalize_robots_octets(value: str, *, allow_wildcards: bool = False) -> str:
    """Normalize URI octets for REP matching, retaining reserved escapes."""
    unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    result: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if allow_wildcards and character == "*":
            result.append(character)
            index += 1
            continue
        if character == "%":
            if index + 2 >= len(value):
                raise ValueError("malformed percent escape in robots path")
            try:
                octet = int(value[index + 1 : index + 3], 16)
            except ValueError as error:
                raise ValueError("malformed percent escape in robots path") from error
            decoded = chr(octet)
            result.append(decoded if decoded in unreserved else f"%{octet:02X}")
            index += 3
            continue
        if ord(character) < 128:
            result.append(character)
        else:
            result.extend(f"%{octet:02X}" for octet in character.encode("utf-8"))
        index += 1
    return "".join(result)


def _robots_pattern_specificity(pattern: str) -> int:
    """Count matched octets, excluding wildcard metacharacters."""
    specificity = 0
    index = 0
    while index < len(pattern):
        if pattern[index] == "*":
            index += 1
        elif pattern[index] == "%":
            specificity += 1
            index += 3
        else:
            specificity += 1
            index += 1
    return specificity


def robots_policy_allows(text: str, target_url: str) -> bool:
    """Evaluate exact target path under the collector's declared robots user agent."""
    return _robots_path_allowed(parse_robots_rules(text), target_url)
