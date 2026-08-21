"""Opaque, versioned, filter-bound keyset cursors."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime
from uuid import UUID

from career_os.api.schemas import JobFilters
from career_os.persistence.api_queries import JobCursorKey

_CURSOR_VERSION = 1


class InvalidCursorError(ValueError):
    """A cursor is malformed, altered, unsupported, or belongs to other filters."""


def filters_fingerprint(filters: JobFilters) -> str:
    normalized = filters.model_dump(mode="json", exclude_none=True)
    packed = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(packed).hexdigest()


def encode_job_cursor(key: JobCursorKey, *, filters: JobFilters) -> str:
    if key.last_seen_at.tzinfo is None or key.last_seen_at.utcoffset() is None:
        raise ValueError("cursor timestamp must be timezone-aware")
    body = {
        "f": filters_fingerprint(filters),
        "i": str(key.job_id),
        "t": key.last_seen_at.isoformat(),
        "v": _CURSOR_VERSION,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    envelope = {"b": body, "c": hashlib.sha256(canonical).hexdigest()}
    packed = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(packed).rstrip(b"=").decode("ascii")


def decode_job_cursor(value: str, *, filters: JobFilters) -> JobCursorKey:
    try:
        padding = "=" * (-len(value) % 4)
        packed = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(packed).rstrip(b"=").decode("ascii") != value:
            raise ValueError
        envelope = json.loads(packed)
        if not isinstance(envelope, dict) or set(envelope) != {"b", "c"}:
            raise ValueError
        body = envelope["b"]
        checksum = envelope["c"]
        if not isinstance(body, dict) or set(body) != {"f", "i", "t", "v"}:
            raise ValueError
        if (
            type(body["v"]) is not int
            or not isinstance(body["f"], str)
            or not isinstance(body["i"], str)
            or not isinstance(body["t"], str)
        ):
            raise ValueError
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        if not isinstance(checksum, str) or not hmac.compare_digest(
            checksum, hashlib.sha256(canonical).hexdigest()
        ):
            raise ValueError
        if body["v"] != _CURSOR_VERSION or body["f"] != filters_fingerprint(filters):
            raise ValueError
        timestamp = datetime.fromisoformat(body["t"])
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError
        return JobCursorKey(last_seen_at=timestamp, job_id=UUID(body["i"]))
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidCursorError("invalid or filter-incompatible cursor") from error
