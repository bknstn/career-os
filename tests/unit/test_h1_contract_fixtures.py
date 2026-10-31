"""Published H1 examples remain readable by the published schema.

Behavior and evidence reconciliation belong to test_h1_runtime.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = PROJECT_ROOT / "tests/fixtures/contracts"


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(
        (PROJECT_ROOT / "docs/h1-hybrid-result-contract.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.mark.parametrize("state", ["accepted", "evidence_limited"])
def test_published_result_and_review_examples_are_consistent(
    state: str, validator: Draft202012Validator
) -> None:
    result_name = f"h1_{state}_result.json"
    artifact_name = f"h1_{state}_review_artifact.json"
    result = json.loads((FIXTURE_DIR / result_name).read_text(encoding="utf-8"))
    artifact = json.loads((FIXTURE_DIR / artifact_name).read_text(encoding="utf-8"))

    errors = list(validator.iter_errors(result))
    assert errors == [], [(list(error.path), error.message) for error in errors]
    assert result["quality"]["state"] == state
    assert artifact["result_fixture"] == result_name
    manual = [
        evaluation
        for evaluation in result["quality"]["evaluation_provenance"].values()
        if evaluation["basis"] == "manual_review"
    ]
    assert manual
    for evaluation in manual:
        assert evaluation["provenance"]["ref"] == f"tests/fixtures/contracts/{artifact_name}"
        assert evaluation["provenance"]["reviewed_job_ids"] == artifact["reviewed_job_ids"]
