"""Pure, source-neutral contracts shared across Career OS boundaries."""

from career_os.domain.canonical import (
    CANONICAL_CONTENT_FIELDS,
    CANONICAL_HASH_FIELDS,
    CanonicalJobCandidate,
    content_hash,
    stable_json_dumps,
)
from career_os.domain.jobs import RawJobPosting
from career_os.domain.normalization import (
    JobNormalization,
    JobNormalizationInput,
    NormalizedCompensation,
    NormalizedField,
    NormalizedLocation,
    RoleFamily,
    SourceCompensationEvidence,
    SourceLocationEvidence,
)
from career_os.domain.runs import RunCounts, ScanDiagnostics
from career_os.domain.skills import SkillEvidence, SkillEvidenceMethod, SkillEvidenceSection
from career_os.domain.sources import (
    FetchContext,
    FetchEvent,
    ScanFailedError,
    ScanOutcome,
    ScanStatus,
    SourceAdapter,
)

__all__ = [
    "CANONICAL_CONTENT_FIELDS",
    "CANONICAL_HASH_FIELDS",
    "CanonicalJobCandidate",
    "FetchContext",
    "FetchEvent",
    "JobNormalization",
    "JobNormalizationInput",
    "NormalizedCompensation",
    "NormalizedField",
    "NormalizedLocation",
    "RawJobPosting",
    "RoleFamily",
    "RunCounts",
    "ScanDiagnostics",
    "ScanFailedError",
    "ScanOutcome",
    "ScanStatus",
    "SkillEvidence",
    "SkillEvidenceMethod",
    "SkillEvidenceSection",
    "SourceAdapter",
    "SourceCompensationEvidence",
    "SourceLocationEvidence",
    "content_hash",
    "stable_json_dumps",
]
