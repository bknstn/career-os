# ADR 0015: Add a reviewed research stream beside job observations

- **Effective date:** 2026-10-19
- **Status:** Accepted; runtime and monitoring completed 2026-10-20
- **Recorded:** 2026-10-22, retrospectively from the implementation history

## Context

Questions about AI and work also need publisher research. Surveys, experiments, and job postings have different populations and cannot share a denominator.

## Decision

Collect a bounded publisher catalog, archive immutable document versions, and extract text deterministically. Store annotations as candidates until explicit claim review. Bind accepted evidence to exact current text and locators. Use durable local monitor state for pending versions and weekly/monthly products.

## Consequences

Text changes reopen review. Hypothesis status requires explicit review; collection cannot auto-accept a claim. Monitoring can be scheduled externally, while local state and products stay out of Git.
