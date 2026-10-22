# H4/H5 — bounded single-vacancy review

Status: implemented local vertical slice, 2026-09-17. This is a manually mapped
example with deterministic validation/report assembly, not an automatic career
analyst, full H4 classification engine or accepted H5 product gate.

## Fictional review example

The H3 fictional profile and a retained public job posting exercise full
responsibility/qualification coverage, relevant/partial/unknown evidence,
and digest-bound manual judgments. The public source body is retained at
`tests/fixtures/career/vacancy.raw.json`. This fixture does not establish
current vacancy availability or facts about a real candidate.

## H4 evidence contract

`tests/fixtures/career/vacancy.yaml` uses `h4.vacancy-evidence.v1` with a sole
`vacancy` root. It carries identity, source URL, capture date, retained reference,
source text, SHA-256 and manually assigned evidence blocks.

The text is the response title, location and normalized HTML description;
`html.unescape` followed by the existing `html_to_text` preserves the provider's
content as normalized text. A regression reconstructs it from the retained raw
response. Runtime validation uses the embedded text; it does not dereference
local paths or URLs. The text hash detects accidental source drift; it is not
cryptographic proof of external authenticity.

Every item includes an exact quote and zero-based, end-exclusive character span
in that text, a section, a short paraphrase and a manually assigned signal
(skill/experience/education/scope/leadership/domain). All bullet points in the
three explicit sections must be represented exactly once. Whole compound
clauses are retained: languages listed as alternatives are not split into
independent hard requirements, and a years requirement is not dropped when
one language matches.

The first parser only supports `You will:`, `You have:` and `We prefer:` headings
and bullet lines produced by the existing normalizer. It assigns section
semantics directly from those headings. Unexpected text after these sections
fails closed (apart from the retained `#LI-Hybrid` marker). It does not support
arbitrary ATS layouts, other languages or inferred headings. Introductory
company boilerplate is retained but excluded from qualification comparisons;
robotics/ML terms in that introduction do not become requirements.

The contract rejects changed quotes, wrong spans, section promotion, omitted or
duplicate bullets, duplicate IDs and snapshot digest mismatch. Semantic signal
and paraphrase quality remain human-reviewed annotation responsibilities.

## H5 mapping contract and behavior

`tests/fixtures/career/review-plan.yaml` uses `h5.review-plan.v1` with a sole `review`
root. It records a review date, canonical-model digests of both inputs,
assessments, five personal constraint checks, and up to three explicitly chosen
unresolved required items to prioritize. Canonical digests are computed with
`fingerprint`; if either input changes, the mapping must be reviewed again.
Regenerating hashes without reviewing the mapping is not an update workflow.

Every vacancy evidence ID must have exactly one assessment, with:

- `relevant`, `partial` or `not_established` relation;
- namespaced references to H3 claims, skills or employment;
- manual rationale and a concrete next check.

The builder resolves references, preserves statement and provenance status,
and checks dates, complete coverage and the digest binding. Relevant/partial
relations require at least one asserted profile fact. Unknowns and
interpretations cannot independently justify those relations; a `relevant`
relation cannot include either as supporting evidence. Document assertions
remain `document_unreviewed` in the result. Even `relevant` means evidence to
discuss, not proven fulfillment of a requirement or independent skill assessment.

Five checks must be present exactly once: role, location, work mode, employment
model and base pay. They explicitly compare the embedded H3 target with the
snapshot. Compatible/conflict judgments require a literal source excerpt;
unknown checks can lack one when the snapshot is silent. The application
validates the excerpt, not the semantic truth of a human compatibility judgment.
There is no automatic salary/geographic inference. Semantic review is required
before applying a mapping to a decision, even when all structural checks pass.

Conflict dominates unknown; otherwise unresolved conditions remain unresolved.
All compatible conditions are named `compatible_by_manual_review`, never
"candidate qualifies". The output does not declare full fit or recommend an
application solely from constraints or evidence counts.

## Reproduction and outputs

```bash
uv run career-os career review \
  --profile config/career-profile.example.yaml \
  --vacancy tests/fixtures/career/vacancy.yaml \
  --review-plan tests/fixtures/career/review-plan.yaml \
  --output .local/reports/career-review.md
```

Add `--format json` and choose a `.json` output to retain the full reconciled
inputs and result. Both projections use the same builder and source order.
Invalid configuration exits with code 2 and preserves an existing output;
output paths matching input paths are rejected. Files are written atomically.
No network or PostgreSQL connection is needed to reproduce the report.

Published input schemas are `h4-vacancy-evidence.schema.json` and
`h5-review-plan.schema.json`. Runtime cross-record invariants supplement JSON
Schema. Tests cover schema drift, exact raw-response reconstruction, quote
anchoring, full coverage, stale input, evidence-status boundaries, output parity
and safe failures. Full offline regression remains required.

## Decision history

See [ADR 0007](adr/0007-provenance-and-manual-career-review.md) for the
provenance and manual-review boundaries.
