"""Pure, source-neutral contracts shared across Career OS boundaries."""

from career_os.domain.canonical import (
    CANONICAL_CONTENT_FIELDS,
    CanonicalJobCandidate,
    content_hash,
    stable_json_dumps,
)
from career_os.domain.jobs import RawJobPosting
from career_os.domain.runs import RunCounts
from career_os.domain.sources import (
    FetchContext,
    FetchEvent,
    ScanOutcome,
    ScanStatus,
    SourceAdapter,
)

__all__ = [
    "CANONICAL_CONTENT_FIELDS",
    "CanonicalJobCandidate",
    "FetchContext",
    "FetchEvent",
    "RawJobPosting",
    "RunCounts",
    "ScanOutcome",
    "ScanStatus",
    "SourceAdapter",
    "content_hash",
    "stable_json_dumps",
]
