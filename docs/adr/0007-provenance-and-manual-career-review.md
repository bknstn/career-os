# ADR 0007: Separate profile provenance from manual vacancy judgments

- **Effective date:** 2026-09-17
- **Status:** Accepted
- **Recorded:** 2026-10-22, retrospectively from the implementation history

## Context

Skill keywords alone do not capture ownership, architecture work, leadership, or career intent. Supplied documents, explicit statements, interpretations, and unknowns have different evidential status.

## Decision

Use the strict H3 file-backed profile with per-record provenance. Represent a selected public vacancy with exact quote spans in H4. Bind the manual H5 mapping to profile and vacancy fingerprints, and require review again after either input changes.

## Consequences

The model validates provenance structure and freshness, while semantic judgments remain manual. It emits no automatic career verdict or hiring score. Public regression examples must be fictional.
