"""Strict JSON validation for unreviewed research claim imports."""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from career_os.application.research_workflow import (
    ResearchWorkflowPayloadError,
    parse_claim_drafts,
    parse_monthly_review,
)
from career_os.domain.research import ResearchQuestion

_QUOTE = "AI changes tasks"
_VERSION_ID = UUID("00000000-0000-0000-0000-000000000123")


def _payload() -> dict[str, object]:
    return {
        "schema_version": "career-os.research-claim-drafts.v1",
        "claims": [
            {
                "document_version_id": str(_VERSION_ID),
                "question": "developer_tasks",
                "statement": "The source reports a change in developer tasks.",
                "exact_quote": _QUOTE,
                "location": {
                    "media_type": "text/html",
                    "start_offset": 0,
                    "end_offset": len(_QUOTE),
                    "section": "Findings",
                    "page_number": None,
                },
                "evidence_kind": "survey",
                "metric_definition": None,
                "metric_value": None,
                "denominator": None,
                "geography": None,
                "sample_description": None,
                "sample_size": None,
                "data_period": {"start": None, "end": None, "state": "not_reported"},
                "limitations": None,
                "missing_reasons": {
                    "metric_definition": "not_reported",
                    "metric_value": "not_reported",
                    "denominator": "not_reported",
                    "geography": "not_reported",
                    "sample_description": "not_reported",
                    "sample_size": "not_reported",
                    "limitations": "not_reported",
                },
                "lineage_kind": "unknown",
                "upstream_dataset_or_document": None,
            }
        ],
    }


def _encoded(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def test_strict_import_parses_domain_enums_and_preserves_exact_quote() -> None:
    drafts = parse_claim_drafts(_encoded(_payload()))

    assert len(drafts) == 1
    assert drafts[0].document_version_id == _VERSION_ID
    assert drafts[0].question is ResearchQuestion.DEVELOPER_TASKS
    assert drafts[0].exact_quote == _QUOTE
    assert drafts[0].location.end_offset == len(_QUOTE)


def test_import_rejects_unknown_fields_and_blank_reported_strings() -> None:
    unknown = _payload()
    unknown["automatic_acceptance"] = True
    with pytest.raises(ResearchWorkflowPayloadError, match="schema validation"):
        parse_claim_drafts(_encoded(unknown))

    blank_reported = _payload()
    claims = blank_reported["claims"]
    assert isinstance(claims, list)
    claims[0]["geography"] = "  "
    claims[0]["missing_reasons"].pop("geography")
    with pytest.raises(ResearchWorkflowPayloadError, match="schema validation"):
        parse_claim_drafts(_encoded(blank_reported))


def test_import_requires_explicit_missing_reasons_and_rejects_blank_statements() -> None:
    missing_reason = _payload()
    claims = missing_reason["claims"]
    assert isinstance(claims, list)
    claims[0]["missing_reasons"].pop("limitations")
    with pytest.raises(ResearchWorkflowPayloadError, match="domain validation"):
        parse_claim_drafts(_encoded(missing_reason))

    blank_statement = _payload()
    claims = blank_statement["claims"]
    assert isinstance(claims, list)
    claims[0]["statement"] = " \n"
    with pytest.raises(ResearchWorkflowPayloadError, match="schema validation"):
        parse_claim_drafts(_encoded(blank_statement))


def test_import_rejects_duplicate_json_keys() -> None:
    raw = _encoded(_payload()).decode("utf-8")
    duplicate = raw.replace(
        '"schema_version":"career-os.research-claim-drafts.v1",',
        '"schema_version":"career-os.research-claim-drafts.v1","schema_version":"career-os.research-claim-drafts.v1",',
        1,
    )

    with pytest.raises(ResearchWorkflowPayloadError, match="duplicate object key"):
        parse_claim_drafts(duplicate.encode("utf-8"))


def test_import_rejects_empty_and_oversized_batches() -> None:
    empty = {"schema_version": "career-os.research-claim-drafts.v1", "claims": []}
    with pytest.raises(ResearchWorkflowPayloadError, match="schema validation"):
        parse_claim_drafts(_encoded(empty))

    with pytest.raises(ResearchWorkflowPayloadError, match="8 MiB limit"):
        parse_claim_drafts(b" " * (8 * 1024 * 1024 + 1))


def _monthly_review() -> dict[str, object]:
    hypotheses = []
    for hypothesis_id in (
        "developer_task_mix_change",
        "seniority_differentiation",
        "production_ai_skills_shift",
    ):
        hypotheses.append(
            {
                "id": hypothesis_id,
                "status": "insufficient",
                "rationale": "Current accepted claims do not establish this outcome.",
                "accepted_claim_ids": [],
                "alternative_explanations": ["The sampled population may differ."],
                "limitations": ["The evidence base is limited."],
            }
        )
    return {"period_id": "2026-11", "reviewer": "codex:parent", "hypotheses": hypotheses}


def test_monthly_review_parser_requires_all_explicit_hypotheses_and_rejects_unknown_fields() -> (
    None
):
    parsed = parse_monthly_review(_encoded(_monthly_review()))
    assert parsed["period_id"] == "2026-11"
    assert len(parsed["hypotheses"]) == 3

    unknown = _monthly_review()
    unknown["conclusion"] = "automatic"
    with pytest.raises(ResearchWorkflowPayloadError, match="schema validation"):
        parse_monthly_review(_encoded(unknown))

    duplicate = _monthly_review()
    duplicate_hypotheses = duplicate["hypotheses"]
    assert isinstance(duplicate_hypotheses, list)
    duplicate_hypotheses[2] = duplicate_hypotheses[0]
    with pytest.raises(ResearchWorkflowPayloadError, match="schema validation"):
        parse_monthly_review(_encoded(duplicate))


def test_monthly_review_parser_rejects_blank_notes_and_duplicate_keys() -> None:
    blank = _monthly_review()
    hypotheses = blank["hypotheses"]
    assert isinstance(hypotheses, list)
    hypotheses[0]["limitations"] = [" "]
    with pytest.raises(ResearchWorkflowPayloadError, match="schema validation"):
        parse_monthly_review(_encoded(blank))

    raw = _encoded(_monthly_review()).decode("utf-8")
    duplicate_key = raw.replace(
        '"reviewer":"codex:parent",', '"reviewer":"codex:parent","reviewer":"codex:parent",', 1
    )
    with pytest.raises(ResearchWorkflowPayloadError, match="duplicate object key"):
        parse_monthly_review(duplicate_key.encode("utf-8"))
