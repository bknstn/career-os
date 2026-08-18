"""Pure contracts at the persistence repository boundary."""

from __future__ import annotations

from types import MappingProxyType
from uuid import uuid4

import pytest

from career_os.domain import CANONICAL_CONTENT_FIELDS
from career_os.persistence.repositories import (
    JobUpsertAction,
    JobUpsertResult,
    _plain_json_object,
)


def test_json_evidence_is_copied_into_ordinary_json_container_types() -> None:
    original = MappingProxyType(
        {
            "nested": MappingProxyType({"values": ("one", "two")}),
            "flag": True,
        }
    )

    copied = _plain_json_object(original)

    assert copied == {"nested": {"values": ["one", "two"]}, "flag": True}
    nested = copied["nested"]
    assert isinstance(nested, dict)
    assert isinstance(nested["values"], list)


def test_upsert_result_rejects_inconsistent_action_and_changed_fields() -> None:
    job_id = uuid4()

    assert (
        JobUpsertResult(
            action=JobUpsertAction.INSERTED,
            job_id=job_id,
            content_hash="hash",
            changed_fields=CANONICAL_CONTENT_FIELDS,
        ).action
        is JobUpsertAction.INSERTED
    )

    with pytest.raises(ValueError, match="unchanged"):
        JobUpsertResult(
            action=JobUpsertAction.UNCHANGED,
            job_id=job_id,
            content_hash="hash",
            changed_fields=("title_raw",),
        )
