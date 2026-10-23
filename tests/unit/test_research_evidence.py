from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker

from career_os.api.research import create_research_router
from career_os.application.research_evidence import (
    ResearchEvidenceReadStore,
    ResearchEvidenceService,
    SqlResearchEvidenceReadStore,
    _assess_current_evidence,
)
from career_os.domain.research import ResearchQuestion

Row = Mapping[str, object]


def test_current_claim_requires_latest_acceptance_and_matching_latest_text() -> None:
    accepted = {"event_sequence": 2, "state": "accepted"}
    assert _assess_current_evidence(
        claim_version_digest="same",
        latest_document_digest="same",
        reviews=({"event_sequence": 1, "state": "candidate"}, accepted),
    ) == (True, "accepted", None)

    for state in ("candidate", "revision_review_required", "rejected"):
        current, review_state, reason = _assess_current_evidence(
            claim_version_digest="same",
            latest_document_digest="same",
            reviews=({"event_sequence": 3, "state": state},),
        )
        assert not current
        assert review_state == state
        assert reason == state

    # A later forged acceptance cannot erase a terminal audit event.
    terminal_then_forged_acceptance = (
        {"event_sequence": 4, "state": "retracted"},
        {"event_sequence": 5, "state": "accepted"},
    )
    assert _assess_current_evidence(
        claim_version_digest="same",
        latest_document_digest="same",
        reviews=terminal_then_forged_acceptance,
    ) == (False, "accepted", "retracted")

    assert _assess_current_evidence(
        claim_version_digest="old",
        latest_document_digest="new",
        reviews=(accepted,),
    ) == (False, "accepted", "normalized_text_changed")
    assert _assess_current_evidence(
        claim_version_digest="same",
        latest_document_digest=None,
        reviews=(accepted,),
    ) == (False, "accepted", "normalized_text_unavailable")


def test_highlights_include_only_current_claims_and_keep_unknown_dates_explicit() -> None:
    document_id = UUID(int=41)
    current = _claim_row(41, document_id, lineage="primary", upstream="Study A")
    current["accepted_at"] = datetime(2026, 10, 20, tzinfo=UTC)
    current["evidence_details"] = {
        "limitations": None,
        "missing_reasons": {"limitations": "not_reported"},
        "data_period": {"start": None, "end": None, "state": "not_reported"},
    }
    changed = _claim_row(42, document_id, lineage="primary", digest="old")
    changed["accepted_at"] = datetime(2026, 10, 21, tzinfo=UTC)
    terminal = _claim_row(43, document_id, lineage="primary")
    terminal["accepted_at"] = datetime(2026, 10, 22, tzinfo=UTC)
    candidate = _claim_row(44, document_id, lineage="primary")
    candidate["accepted_at"] = datetime(2026, 10, 23, tzinfo=UTC)
    store = FakeStore(
        claims=(current, changed, terminal, candidate),
        reviews={
            UUID(int=41): (_review(1, "accepted"),),
            UUID(int=42): (_review(2, "accepted"),),
            UUID(int=43): (_review(3, "accepted"), _review(4, "retracted")),
            UUID(int=44): (_review(5, "candidate"),),
        },
        latest_digests={document_id: "same"},
    )

    result = cast(Any, ResearchEvidenceService(store).highlights(limit=5))

    assert result["schema_version"] == "career-radar.research.highlights.v1"
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["id"] == str(UUID(int=41))
    assert item["accepted_at"] == "2026-10-20T00:00:00+00:00"
    assert item["document"]["version_id"] == str(UUID(int=1041))
    assert item["document"]["publication_date"] == {
        "value": None,
        "state": "not_reported",
    }
    assert item["data_period"] == {"start": None, "end": None, "state": "not_reported"}
    assert item["limitation"] is None
    assert item["limitation_state"] == "not_reported"


def test_highlight_limit_is_small_and_validated() -> None:
    store = FakeStore()
    service = ResearchEvidenceService(store)
    assert service.highlights(limit=5)["items"] == []
    for limit in (0, 6):
        try:
            service.highlights(limit=limit)
        except ValueError as error:
            assert "between 1 and 5" in str(error)
        else:
            raise AssertionError("out-of-range highlight limit should fail")


def test_question_projection_keeps_lineage_and_job_search_context_separate() -> None:
    doc_a = UUID(int=1)
    doc_b = UUID(int=2)
    rows = (
        _claim_row(1, doc_a, lineage="primary", upstream="Dataset Alpha"),
        _claim_row(
            2,
            doc_b,
            lineage="primary",
            upstream=" dataset   alpha ",
            evidence_kind="survey",
        ),
        _claim_row(
            3,
            doc_b,
            lineage="primary",
            evidence_kind="secondary_synthesis",
        ),
        _claim_row(4, doc_a, lineage="primary", upstream="Other study", digest="old"),
        _claim_row(5, doc_a, lineage="primary", upstream="Candidate study"),
    )
    store = FakeStore(
        claims=rows,
        reviews={
            UUID(int=1): (_review(1, "accepted"),),
            UUID(int=2): (_review(2, "accepted"),),
            UUID(int=3): (_review(3, "accepted"),),
            UUID(int=4): (_review(4, "accepted"),),
            UUID(int=5): (_review(5, "candidate"),),
        },
        latest_digests={doc_a: "same", doc_b: "same"},
    )

    items = cast(Any, ResearchEvidenceService(store).questions())["items"]
    developer_tasks = next(
        item for item in items if item["id"] == ResearchQuestion.DEVELOPER_TASKS.value
    )
    assert developer_tasks["assessment"] == "insufficient"
    assert developer_tasks["current_evidence_claim_count"] == 3
    assert developer_tasks["all_claim_count"] == 5
    assert developer_tasks["lineage"]["independent_primary_group_count"] == 1
    assert developer_tasks["lineage"]["secondary_synthesis_claims_mislabeled_primary"] == 1
    assert developer_tasks["lineage"]["secondary_synthesis_is_independent"] is False
    assert "denominator" in developer_tasks["context"]["denominator_note"]
    assert developer_tasks["context"]["job_radar_searches"]
    assert developer_tasks["context"]["skill_search_terms"]
    assert "counts alone do not determine" in developer_tasks["assessment_note"].lower()
    assert "/radar/api/research/products" in developer_tasks["assessment_note"]
    assert (
        "methods, populations, periods, and denominators remain separate"
        in developer_tasks["assessment_note"].lower()
    )


def test_primary_lineage_groups_union_shared_documents_and_upstream_names() -> None:
    document_a = UUID(int=21)
    document_b = UUID(int=22)
    document_c = UUID(int=23)
    rows = (
        _claim_row(21, document_a, lineage="primary", upstream="Claude Code sessions"),
        _claim_row(22, document_a, lineage="primary", upstream="Anthropic telemetry"),
        _claim_row(23, document_b, lineage="primary", upstream=" anthropic   telemetry "),
        _claim_row(24, document_c, lineage="primary", upstream="Independent trial"),
    )
    store = FakeStore(
        claims=rows,
        reviews={UUID(int=index): (_review(index, "accepted"),) for index in range(21, 25)},
        latest_digests={document_a: "same", document_b: "same", document_c: "same"},
    )

    questions = cast(Any, ResearchEvidenceService(store).questions())["items"]
    developer_tasks = next(
        item for item in questions if item["id"] == ResearchQuestion.DEVELOPER_TASKS.value
    )

    assert developer_tasks["lineage"]["independent_primary_group_count"] == 2
    assert "grouped conservatively" in developer_tasks["lineage"]["note"]
    assert "share a source document" in developer_tasks["lineage"]["note"]


def test_version_projection_escapes_untrusted_text_and_preserves_provenance() -> None:
    document_id = UUID(int=101)
    version_id = UUID(int=102)
    store = FakeStore(
        version={
            "document_id": document_id,
            "version_id": version_id,
            "source_key": "sample",
            "canonical_url": "https://example.org/report",
            "document_created_at": None,
            "version_number": 2,
            "previous_version_id": UUID(int=100),
            "title": "Report & findings",
            "publication_date": None,
            "publication_date_state": "not_reported",
            "data_period_start": None,
            "data_period_end": None,
            "data_period_state": "not_reported",
            "discovered_url": "https://example.org/index",
            "retrieved_url": "https://example.org/report?print=1",
            "retrieved_at": None,
            "response_etag": 'W/"body-v2"',
            "response_last_modified": None,
            "media_type": "text/html",
            "body_bytes": 200,
            "raw_sha256": "a" * 64,
            "normalized_text": "<script>alert('body')</script> & more",
            "normalized_text_sha256": "b" * 64,
            "extraction_version": "test-normalizer-v1",
            "extraction_regions": [
                {
                    "media_type": "text/html",
                    "start_offset": 0,
                    "end_offset": 10,
                    "section": "Findings",
                    "page_number": None,
                }
            ],
            "extraction_error": None,
            "version_relation": "revision",
        },
        attempts=(
            {
                "id": UUID(int=103),
                "requested_url": "https://example.org/report?print=1",
                "outcome": "stored",
                "bytes_received": 200,
                "http_status": 200,
            },
        ),
    )

    result = cast(Any, ResearchEvidenceService(store).version_evidence(document_id, version_id))
    assert result is not None
    assert result["normalized_text"] == "<script>alert('body')</script> & more"
    assert result["escaped_normalized_text"] == (
        "&lt;script&gt;alert('body')&lt;/script&gt; &amp; more"
    )
    assert result["regions"][0]["section"] == "Findings"
    assert result["version"]["response_etag"] == 'W/"body-v2"'
    assert result["acquisition_attempts"][0]["requested_url"] == (
        "https://example.org/report?print=1"
    )
    assert "raw_body" not in result


def test_claim_projection_includes_review_history_and_current_exclusion() -> None:
    document_id = UUID(int=201)
    claim_id = UUID(int=202)
    version_id = UUID(int=203)
    store = FakeStore(
        claims=(
            _claim_row(
                claim_id.int,
                document_id,
                lineage="primary",
                upstream="Study 1",
                version_id=version_id,
            ),
        ),
        reviews={claim_id: (_review(1, "candidate"),)},
        latest_digests={document_id: "same"},
    )

    claims = cast(Any, ResearchEvidenceService(store).claims(question=None, page=1, page_size=25))
    claim = claims["items"][0]
    assert claim["current_evidence"] is False
    assert claim["review_state"] == "candidate"
    assert claim["excluded_reason"] == "candidate"
    assert [review["state"] for review in claim["review_history"]] == ["candidate"]
    assert claim["version"]["id"] == str(version_id)


def test_sql_projections_measure_body_bytes_without_selecting_body_content() -> None:
    session = UnusedSession()
    store = SqlResearchEvidenceReadStore(cast(Session, session))
    statement = store._claim_select()
    sql = str(statement)

    assert "octet_length(research_document_versions.raw_body)" in sql
    assert "SELECT research_document_versions.raw_body" not in sql
    assert "research_document_versions.normalized_text," not in sql


def test_highlight_sql_filters_current_reviews_and_sorts_by_review_time() -> None:
    class Result:
        def mappings(self) -> Result:
            return self

        def all(self) -> list[Row]:
            return []

    class CaptureSession:
        statement: Any

        def execute(self, statement: Any) -> Result:
            self.statement = statement
            return Result()

    session = CaptureSession()
    store = SqlResearchEvidenceReadStore(cast(Session, session))
    assert store.list_current_highlights(limit=3) == []
    sql = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "research_document_versions.normalized_text_sha256 =" in sql
    assert "research_claim_reviews.state IN ('superseded', 'retracted')" in sql
    assert "ORDER BY anon_" in sql
    assert "reviewed_at DESC, anon_" in sql
    assert "event_sequence DESC, research_claims.id" in sql
    assert sql.endswith("LIMIT 3")
    assert "SELECT research_document_versions.raw_body" not in sql


def test_router_paths_and_query_validation_do_not_open_a_database() -> None:
    calls: list[str] = []

    def provider() -> sessionmaker[Session]:
        calls.append("provider")
        raise AssertionError("read provider should not be touched during construction/validation")

    app = FastAPI()
    app.include_router(create_research_router(session_factory_provider=provider))
    paths = set(app.openapi()["paths"])
    assert "/radar/api/research/overview" in paths
    assert "/radar/api/research/questions" in paths
    assert "/radar/api/research/documents" in paths
    assert "/radar/api/research/documents/{document_id}/versions/{version_id}" in paths
    assert "/radar/api/research/claims" in paths
    assert "/radar/api/research/highlights" in paths

    client = TestClient(app)
    response = client.get("/radar/api/research/documents?page_size=101")
    assert response.status_code == 422
    assert client.get("/radar/api/research/highlights?limit=6").status_code == 422
    assert calls == []


def test_overview_includes_enabled_sources_with_no_documents_and_latest_failures() -> None:
    store = FakeStore(
        source_snapshot={
            "run_id": UUID(int=301),
            "registry_version": "pilot-v1",
            "registry_digest": "d" * 64,
            "registry_snapshot": {
                "sources": [
                    {"key": "alpha", "publisher": "Alpha Institute", "enabled": True},
                    {"key": "mckinsey", "publisher": "McKinsey", "enabled": True},
                    {"key": "disabled", "publisher": "Disabled", "enabled": False},
                ]
            },
            "trigger": "backfill",
            "run_status": "partial",
        },
        document_counts=(
            {
                "source_key": "alpha",
                "document_count": 1,
                "extractable_count": 1,
                "extraction_error_count": 0,
            },
        ),
        source_attempts=(
            {
                "source_key": "mckinsey",
                "outcome": "network_error",
                "occurred_at": None,
                "http_status": None,
                "bytes_received": 0,
                "error_code": "connect_timeout",
                "run_id": UUID(int=302),
                "run_status": "partial",
                "last_success_at": None,
            },
        ),
    )

    overview = cast(Any, ResearchEvidenceService(store).overview())
    by_source = {row["source_key"]: row for row in overview["documents"]["by_source"]}
    assert set(by_source) == {"alpha", "mckinsey"}
    assert by_source["alpha"]["document_count"] == 1
    assert by_source["mckinsey"]["document_count"] == 0
    assert by_source["mckinsey"]["publisher"] == "McKinsey"
    assert by_source["mckinsey"]["latest_attempt"]["run_status"] == "partial"
    assert by_source["mckinsey"]["latest_attempt"]["error_code"] == "connect_timeout"
    assert by_source["mckinsey"]["last_success_at"] is None
    assert overview["source_registry"]["version"] == "pilot-v1"


class FakeStore(ResearchEvidenceReadStore):
    def __init__(
        self,
        *,
        claims: Sequence[Row] = (),
        reviews: Mapping[UUID, Sequence[Row]] | None = None,
        latest_digests: Mapping[UUID, str | None] | None = None,
        version: Row | None = None,
        attempts: Sequence[Row] = (),
        source_snapshot: Row | None = None,
        document_counts: Sequence[Row] = (),
        source_attempts: Sequence[Row] = (),
    ) -> None:
        self._claims = tuple(claims)
        self._reviews = reviews or {}
        self._latest_digests = latest_digests or {}
        self._version = version
        self._attempts = tuple(attempts)
        self._source_snapshot = source_snapshot
        self._document_counts = tuple(document_counts)
        self._source_attempts = tuple(source_attempts)

    def list_documents(self, *, page: int, page_size: int) -> tuple[Sequence[Row], int]:
        return (), 0

    def get_version(self, document_id: UUID, version_id: UUID) -> Row | None:
        if (
            self._version is None
            or self._version.get("document_id") != document_id
            or self._version.get("version_id") != version_id
        ):
            return None
        return self._version

    def list_claims(
        self,
        *,
        question: ResearchQuestion | None,
        version_id: UUID | None,
        page: int,
        page_size: int,
    ) -> tuple[Sequence[Row], int]:
        filtered = tuple(
            row
            for row in self._claims
            if (question is None or row.get("question") == question.value)
            and (version_id is None or row.get("document_version_id") == version_id)
        )
        start = (page - 1) * page_size
        return filtered[start : start + page_size], len(filtered)

    def list_current_highlights(self, *, limit: int) -> Sequence[Row]:
        return self._claims[:limit]

    def list_claim_summaries(self) -> Sequence[Row]:
        return self._claims

    def review_history(self, claim_ids: Sequence[UUID]) -> Mapping[UUID, Sequence[Row]]:
        return {claim_id: self._reviews.get(claim_id, ()) for claim_id in claim_ids}

    def latest_text_digests(self, document_ids: Sequence[UUID]) -> Mapping[UUID, str | None]:
        return {document_id: self._latest_digests.get(document_id) for document_id in document_ids}

    def document_counts_by_source(self) -> Sequence[Row]:
        return self._document_counts

    def latest_registry_snapshot(self) -> Row | None:
        return self._source_snapshot

    def source_attempt_statuses(self) -> Sequence[Row]:
        return self._source_attempts

    def acquisition_attempts(self, *, source_key: str, urls: Sequence[str]) -> Sequence[Row]:
        return self._attempts


class UnusedSession:
    def execute(self, _statement: object) -> Any:
        raise AssertionError("SQL compilation test must not execute SQL")


def _claim_row(
    claim_id: int,
    document_id: UUID,
    *,
    lineage: str,
    upstream: str | None = None,
    evidence_kind: str = "observed",
    digest: str = "same",
    version_id: UUID | None = None,
) -> dict[str, object]:
    actual_version_id = version_id or UUID(int=claim_id + 1000)
    return {
        "claim_id": UUID(int=claim_id),
        "document_version_id": actual_version_id,
        "version_id": actual_version_id,
        "question": ResearchQuestion.DEVELOPER_TASKS.value,
        "statement": f"Statement {claim_id}",
        "exact_quote": f"Quote {claim_id}",
        "location": {"media_type": "text/html", "start_offset": 0, "end_offset": 7},
        "evidence_kind": evidence_kind,
        "evidence_details": {"sample_size": 12},
        "lineage_kind": lineage,
        "upstream_dataset_or_document": upstream,
        "claim_created_at": None,
        "document_id": document_id,
        "source_key": "sample",
        "canonical_url": "https://example.org/report",
        "version_number": 1,
        "title": "Report",
        "publication_date": None,
        "publication_date_state": "not_reported",
        "data_period_start": None,
        "data_period_end": None,
        "data_period_state": "not_reported",
        "discovered_url": "https://example.org/index",
        "retrieved_url": "https://example.org/report",
        "retrieved_at": None,
        "media_type": "text/html",
        "body_bytes": 500,
        "raw_sha256": "a" * 64,
        "claim_text_digest": digest,
        "extraction_version": "test-v1",
        "extraction_error": None,
        "version_relation": "initial",
    }


def _review(sequence: int, state: str) -> dict[str, object]:
    return {
        "id": UUID(int=9000 + sequence),
        "event_sequence": sequence,
        "state": state,
        "reviewer": "test-reviewer",
        "reason": "Test review event",
    }
