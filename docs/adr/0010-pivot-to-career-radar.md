# ADR 0010: Pivot to Career Radar with immutable observation history

- **Effective date:** 2026-10-11
- **Status:** Accepted
- **Recorded:** 2026-10-22, retrospectively from the implementation history

## Context

The next question concerns changes in technical roles over time. Mutable current posting rows and selected content versions cannot reconstruct historical board membership or supply a missing baseline.

## Decision

Reuse the collection engine for Career Radar. Save per-run membership and reproducible classification inputs, pin panel/rule versions, and require matched windows and explicit coverage gates before reporting a comparison.

## Consequences

A successful full scan can support an observation snapshot; failed or partial scans cannot establish absence. Missing history yields insufficient with reasons. The pivot does not retroactively create historical evidence.
