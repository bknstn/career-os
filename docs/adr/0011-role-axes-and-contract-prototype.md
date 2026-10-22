# ADR 0011: Separate occupation, seniority, and work context

- **Effective date:** 2026-10-17
- **Status:** Accepted design; broader contract retained as an offline prototype
- **Recorded:** 2026-10-22, retrospectively from the implementation history

## Context

Residual classification audits showed that title scope, occupation, seniority, managerial function, and context could not safely be collapsed into a single role label. Broader coverage alone would not prove correctness.

## Decision

Define occupation/scope independently from seniority and context. Keep literal title evidence and unknown coverage visible. Evaluate the broader versioned contract offline before treating it as a replacement for runtime classification.

## Consequences

The diagnostic prototype is distinct from the usable MVP classifier. Admission to a public comparison still requires reproducible inputs and sufficient history.
