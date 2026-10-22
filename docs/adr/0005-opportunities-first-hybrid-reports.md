# ADR 0005: Prioritize opportunities and bound aggregate claims

- **Effective date:** 2026-08-25
- **Status:** Accepted; product focus later evolved in ADR 0010
- **Recorded:** 2026-10-22, retrospectively from the implementation history

## Context

The first live source-panel iterations produced useful concrete postings, but the eligible sample was small and concentrated. A technically correct aggregate could still suggest more market coverage than the data supported.

## Decision

Present concrete opportunities first and panel observations second. Retain exact evidence IDs, source coverage, denominators, and explicit quality gates in one hybrid result. Expose the same immutable application result through Markdown and the read API; the H1 runtime followed on 2026-08-31.

## Consequences

Broad conclusions stay evidence-limited when the gates fail. Report transports share semantics, while source selection and personal intent need explicit contracts.
