# ADR 0001: Market Explorer foundation

- **Status:** Accepted
- **Date:** 2026-08-17
- **Decision owners:** Career OS maintainers

## Context

Career OS begins with a Job Market Explorer vertical slice. It must collect
public job postings from multiple applicant-tracking systems, retain source
evidence and history, normalize records deterministically, and produce
reproducible market and profile-fit reports.

The initial scale is tens of explicitly configured company boards and a local
developer workflow. Correct lifecycle history, explainability, and ease of
iteration matter more than independent service scaling.

## Decision

Build the MVP as a modular Python 3.12+ monolith with these internal
boundaries:

- source adapters fetch source-native postings without persistence concerns;
- application services own workflows and transaction boundaries;
- domain contracts remain independent of FastAPI, SQLAlchemy, and ATS-specific
  models;
- deterministic, versioned normalization remains independently testable;
- persistence uses PostgreSQL 16+, SQLAlchemy 2, and Alembic;
- Typer provides the primary interface, followed by a small read-only FastAPI
  API that reuses the application services.

PostgreSQL stores raw source payloads, canonical postings, collection runs,
versions, and extracted evidence. A posting is identified within a source by
`(source_id, external_id)`. Ingestion creates a new version only when canonical
content changes and inactivates missing jobs only after a complete successful
source scan.

Implement source adapters in this order: Greenhouse, Lever, Ashby, then
Workable public. Stop after the Greenhouse vertical slice for a design and data
review before expanding the adapter set.

## Consequences

### Positive

- One deployable process and one database keep local setup and debugging
  straightforward.
- Database constraints and transactions can enforce ingestion invariants.
- Raw evidence and versioned deterministic enrichment make reports auditable
  and jobs reprocessable without refetching.
- Internal boundaries preserve a path to split components later if observed
  load requires it.

### Costs and constraints

- Long-running collection and reporting share one codebase and operational
  lifecycle.
- PostgreSQL is required even for integration tests; SQLite is not a supported
  behavioral substitute.
- Taxonomy and normalization changes require explicit versions and regression
  tests.
- The configured company sample limits every market conclusion and must remain
  visible in reports.

## Deferred decisions

This ADR does not select a scheduler, hosted deployment platform, geocoder,
currency conversion policy, cross-source deduplication strategy, or LLM-based
enrichment. Those require separate evidence and ADRs when they enter scope.

## Revisit when

- ingestion volume or latency demonstrates that one process is insufficient;
- a hosted multi-user product introduces authentication or tenancy;
- background scheduling or independent scaling becomes a concrete need;
- measured duplicate postings justify cross-source clustering;
- the Phase 1 architecture/data review changes a foundational boundary.
