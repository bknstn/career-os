"""Disposable PostgreSQL tests for research storage, revisions, and concurrency."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from career_os.config.research_sources import (
    ResearchSource,
    ResearchSourceRegistry,
    ResearchSourceRegistryDocument,
)
from career_os.db import create_database_engine, database_url_from_settings
from career_os.domain.research import (
    ResearchClaimDraft,
    ResearchClaimReviewState,
    ResearchDataPeriod,
    ResearchDateState,
    ResearchEvidenceKind,
    ResearchLineageKind,
    ResearchMediaType,
    ResearchMissingReason,
    ResearchOriginalVersion,
    ResearchQuestion,
    ResearchTextLocation,
    ResearchTextRegion,
    ResearchVersionRelation,
)
from career_os.persistence.integration_safety import (
    CAREER_OS_TABLE_NAMES,
    assert_disposable_test_database,
    assert_initial_schema_is_absent,
)
from career_os.persistence.models import ResearchClaimReview, ResearchDocumentVersion
from career_os.persistence.research import ResearchPersistenceError, ResearchRepository
from career_os.settings import load_settings

pytestmark = pytest.mark.integration


def test_research_repository_serializes_versions_and_preserves_revision_history() -> None:
    if os.environ.get("RUN_DB_INTEGRATION") != "1":
        pytest.skip("set RUN_DB_INTEGRATION=1 to run PostgreSQL integration tests")
    settings = load_settings()
    if settings.database_url is None:
        pytest.skip("set CAREER_OS_DATABASE_URL to run PostgreSQL integration tests")

    project_root = Path(__file__).resolve().parents[2]
    alembic_config = Config(str(project_root / "alembic.ini"))
    registry = ResearchSourceRegistry.load(project_root / "config/radar/research.sources.v1.yaml")
    engine = create_database_engine(settings)
    upgraded = False
    try:
        assert_disposable_test_database(database_url_from_settings(settings))
        assert_initial_schema_is_absent(inspect(engine).get_table_names())
        command.upgrade(alembic_config, "head")
        upgraded = True

        session_factory = sessionmaker(engine, class_=Session, expire_on_commit=False)
        small_limit_registry = ResearchSourceRegistry(
            ResearchSourceRegistryDocument(
                version="test-limits-v1",
                sources=[
                    ResearchSource(
                        key="metr",
                        publisher="METR",
                        enabled=True,
                        allowed_hosts=["metr.org"],
                        discovery_methods=["html_index"],
                        discovery_urls=["https://metr.org/blog/"],
                        max_response_bytes=1024,
                    )
                ],
            )
        )
        too_large = _version(
            raw_body=b"x" * 1025,
            normalized_text="First finding.",
            relation=ResearchVersionRelation.INITIAL,
            previous_version_id=None,
        )
        with (
            session_factory.begin() as session,
            pytest.raises(ResearchPersistenceError, match="configured source byte limit"),
        ):
            ResearchRepository(session, small_limit_registry).append_version(too_large)

        first = _version(
            raw_body=b"<html><h2>Results</h2>First finding.</html>",
            normalized_text="First finding.",
            relation=ResearchVersionRelation.INITIAL,
            previous_version_id=None,
        )
        barrier = Barrier(2)

        def concurrent_import() -> tuple[UUID, bool]:
            barrier.wait(timeout=10)
            with session_factory.begin() as session:
                result = ResearchRepository(session, registry).append_version(first)
                return result.version.id, result.inserted

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(concurrent_import) for _ in range(2)]
            concurrent_results = [future.result(timeout=30) for future in futures]
        assert {version_id for version_id, _ in concurrent_results} == {first.id}
        assert sorted(inserted for _, inserted in concurrent_results) == [False, True]

        with session_factory.begin() as session:
            repository = ResearchRepository(session, registry)
            candidate1 = repository.upsert_candidate(
                source_key="metr",
                discovered_url=first.canonical_url,
                canonical_url=first.canonical_url,
                title="First report",
            )
            candidate2 = repository.upsert_candidate(
                source_key="metr",
                discovered_url=first.canonical_url,
                canonical_url=first.canonical_url,
                title=None,
            )
            assert candidate1.id == candidate2.id
            claim1 = repository.insert_claim_candidate(_draft(first.id))
            repository.append_claim_review(
                claim_id=claim1.id,
                state=ResearchClaimReviewState.ACCEPTED,
                reviewer="reviewer",
                reason="Quote and locator verified.",
            )
            repository.append_claim_review(
                claim_id=claim1.id,
                state=ResearchClaimReviewState.RETRACTED,
                reviewer="reviewer",
                reason="Withdrawn after source correction.",
            )
            claim_id = claim1.id
            stable_claim = repository.insert_claim_candidate(
                _draft(first.id, statement="This claim remains valid across equivalent text.")
            )
            repository.append_claim_review(
                claim_id=stable_claim.id,
                state=ResearchClaimReviewState.ACCEPTED,
                reviewer="reviewer",
                reason="Quote and locator verified.",
            )
            stable_claim_id = stable_claim.id

        with session_factory.begin() as session:
            stored_first = session.get(ResearchDocumentVersion, first.id)
            assert stored_first is not None
            assert stored_first.response_etag == 'W/"candidate-v1"'
            assert stored_first.response_last_modified == "Mon, 19 Oct 2026 12:00:00 GMT"

        with session_factory.begin() as session:
            states = session.scalars(
                select(ResearchClaimReview.state)
                .where(ResearchClaimReview.claim_id == claim_id)
                .order_by(ResearchClaimReview.event_sequence)
            ).all()
            assert states == ["candidate", "accepted", "retracted"]

        second = _version(
            raw_body=b"<div>First finding.</div>",
            normalized_text="First finding.",
            relation=ResearchVersionRelation.EQUIVALENT_TEXT,
            previous_version_id=first.id,
        )
        with session_factory.begin() as session:
            second_result = ResearchRepository(session, registry).append_version(second)
            assert second_result.inserted
            assert second_result.version.version_number == 2
            assert second_result.version.version_relation == "equivalent_text"

        third = _version(
            raw_body=first.original_bytes,
            normalized_text="First finding.",
            relation=ResearchVersionRelation.EQUIVALENT_TEXT,
            previous_version_id=second.id,
        )
        with session_factory.begin() as session:
            third_result = ResearchRepository(session, registry).append_version(third)
            assert third_result.inserted
            assert third_result.version.id != first.id
            assert third_result.version.version_number == 3
            stable_state = session.scalar(
                select(ResearchClaimReview.state)
                .where(ResearchClaimReview.claim_id == stable_claim_id)
                .order_by(ResearchClaimReview.event_sequence.desc())
                .limit(1)
            )
            assert stable_state == "accepted"

        changed = _version(
            raw_body=b"<html><h2>Results</h2>Second finding.</html>",
            normalized_text="Second finding.",
            relation=ResearchVersionRelation.REVISION,
            previous_version_id=third.id,
        )
        with session_factory.begin() as session:
            repository = ResearchRepository(session, registry)
            repository.append_claim_review(
                claim_id=stable_claim_id,
                state=ResearchClaimReviewState.ACCEPTED,
                reviewer="reviewer",
                reason="Equivalent text and current quote verified.",
            )
            changed_result = repository.append_version(changed)
            assert changed_result.inserted
            assert changed_result.version.version_number == 4
            assert changed_result.version.version_relation == "revision"

            raced_claim = repository.insert_claim_candidate(
                _draft(changed.id, quote="Second finding.")
            )
            raced_claim_id = raced_claim.id

        next_revision = _version(
            raw_body=b"<html><h2>Results</h2>Third finding.</html>",
            normalized_text="Third finding.",
            relation=ResearchVersionRelation.REVISION,
            previous_version_id=changed.id,
        )
        accepted_written = Event()
        allow_accept_commit = Event()
        revision_started = Event()
        revision_committed = Event()

        def accept_then_hold_document() -> None:
            with session_factory.begin() as session:
                ResearchRepository(session, registry).append_claim_review(
                    claim_id=raced_claim_id,
                    state=ResearchClaimReviewState.ACCEPTED,
                    reviewer="reviewer",
                    reason="Quote and current version verified.",
                )
                accepted_written.set()
                if not allow_accept_commit.wait(timeout=10):
                    raise TimeoutError("test did not release concurrent claim acceptance")

        def revise_after_accept_started() -> None:
            if not accepted_written.wait(timeout=10):
                raise TimeoutError("claim acceptance did not start")
            revision_started.set()
            with session_factory.begin() as session:
                result = ResearchRepository(session, registry).append_version(next_revision)
                assert result.inserted
                assert result.version.version_number == 5
            revision_committed.set()

        with ThreadPoolExecutor(max_workers=2) as executor:
            acceptance_future = executor.submit(accept_then_hold_document)
            assert accepted_written.wait(timeout=10)
            revision_future = executor.submit(revise_after_accept_started)
            try:
                assert revision_started.wait(timeout=10)
                assert not revision_committed.wait(timeout=0.2)
            finally:
                allow_accept_commit.set()
            acceptance_future.result(timeout=10)
            revision_future.result(timeout=10)

        with session_factory.begin() as session:
            latest_state = session.scalar(
                select(ResearchClaimReview.state)
                .where(ResearchClaimReview.claim_id == stable_claim_id)
                .order_by(ResearchClaimReview.event_sequence.desc())
                .limit(1)
            )
            assert latest_state == "revision_review_required"
            raced_latest_state = session.scalar(
                select(ResearchClaimReview.state)
                .where(ResearchClaimReview.claim_id == raced_claim_id)
                .order_by(ResearchClaimReview.event_sequence.desc())
                .limit(1)
            )
            assert raced_latest_state == "revision_review_required"
            count = session.scalar(select(func.count(ResearchDocumentVersion.id)))
            assert count == 5

        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE research_document_versions SET title = 'mutated' WHERE id = :version_id"
                ),
                {"version_id": first.id},
            )

        command.downgrade(alembic_config, "20261011_0005")
        assert not (
            {name for name in CAREER_OS_TABLE_NAMES if name.startswith("research_")}
            & set(inspect(engine).get_table_names())
        )
    finally:
        try:
            if upgraded or "alembic_version" in inspect(engine).get_table_names():
                command.downgrade(alembic_config, "base")
                with engine.begin() as connection:
                    connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
                assert not (
                    set(inspect(engine).get_table_names())
                    & (CAREER_OS_TABLE_NAMES | {"alembic_version"})
                )
        finally:
            engine.dispose()


def _version(
    *,
    raw_body: bytes,
    normalized_text: str,
    relation: ResearchVersionRelation,
    previous_version_id: UUID | None,
) -> ResearchOriginalVersion:
    return ResearchOriginalVersion(
        id=uuid4(),
        source_key="metr",
        canonical_url="https://metr.org/research/candidate-report",
        discovered_url="https://metr.org/research/candidate-report",
        retrieved_url="https://metr.org/research/candidate-report",
        title="Candidate report",
        retrieved_at=datetime.now(UTC),
        media_type=ResearchMediaType.HTML,
        original_bytes=raw_body,
        normalized_text=normalized_text,
        extraction_regions=(
            ResearchTextRegion(
                ResearchMediaType.HTML,
                0,
                len(normalized_text),
                section="Results",
            ),
        ),
        extraction_version="html-text-v1",
        response_etag='W/"candidate-v1"',
        response_last_modified="Mon, 19 Oct 2026 12:00:00 GMT",
        relation=relation,
        previous_version_id=previous_version_id,
    )


def _draft(
    version_id: UUID,
    *,
    quote: str = "First finding.",
    statement: str = "The report describes a task-level finding.",
) -> ResearchClaimDraft:
    return ResearchClaimDraft(
        document_version_id=version_id,
        question=ResearchQuestion.DEVELOPER_TASKS,
        statement=statement,
        exact_quote=quote,
        location=ResearchTextLocation(
            ResearchMediaType.HTML,
            0,
            len(quote),
            section="Results",
        ),
        evidence_kind=ResearchEvidenceKind.OBSERVED,
        metric_definition=None,
        metric_value=None,
        denominator=None,
        geography=None,
        sample_description=None,
        sample_size=None,
        data_period=ResearchDataPeriod(None, None, ResearchDateState.NOT_REPORTED),
        limitations=None,
        missing_reasons={
            "metric_definition": ResearchMissingReason.NOT_REPORTED,
            "metric_value": ResearchMissingReason.NOT_REPORTED,
            "denominator": ResearchMissingReason.NOT_REPORTED,
            "geography": ResearchMissingReason.NOT_REPORTED,
            "sample_description": ResearchMissingReason.NOT_REPORTED,
            "sample_size": ResearchMissingReason.NOT_REPORTED,
            "limitations": ResearchMissingReason.UNCLEAR,
        },
        lineage_kind=ResearchLineageKind.UNKNOWN,
    )
