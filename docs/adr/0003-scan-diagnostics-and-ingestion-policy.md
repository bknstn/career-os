# ADR 0003: Persist source-neutral scan diagnostics and centralize ingestion policy

- Status: Accepted
- Date: 2026-08-19

## Context

A collection run must remain interpretable after the collecting process exits.
The initial schema retained failed-run errors but not partial reasons, and its
transport counters could not distinguish an actual zero from an unreported
adapter request. The first Greenhouse vertical slice also owned transaction and
lifecycle policy alongside its candidate mapper, which would invite copy/paste
for the next adapter.

## Decision

Adapters return an immutable `ScanDiagnostics` with their terminal outcome and
raise `ScanFailedError` with the telemetry observed after any attempted request.
The contract records HTTP requests, retries, rate-limit events, and rejected
source items. Collection explicitly maps rejected source items to persisted
`failed_items`; candidate-mapping errors are counted independently as
`normalization_errors`.

`collection_runs` persists `incomplete_reason` separately from failed-run
`error_summary`, plus rate-limit and normalization counters. Only partial runs
may have an incomplete reason; only failed runs may have an error summary.

One generic terminal ingestion processor owns canonical upserts, terminal run
state, and complete-scan-only lifecycle reconciliation in a single transaction.
Source modules supply only their validated configuration and candidate mapper.
Partial and failed scans may preserve safe positive evidence, but never infer
absence.

## Consequences

CLI and repository inspection return source identity and durable diagnostics.
New adapters must implement the diagnostics/failure contract and reuse the
generic ingestion service. The migration is reversible, but downgrading removes
the new diagnostic fields by design.
