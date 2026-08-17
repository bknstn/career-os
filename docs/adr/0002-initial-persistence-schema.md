# ADR 0002: Initial persistence schema

- **Status:** Accepted
- **Date:** 2026-08-17
- **Decision owners:** Career OS maintainers

## Context

Market Explorer must rerun collection without duplicating jobs, distinguish
changed postings from unchanged observations, retain source evidence, and
inactivate missing jobs only after a complete source scan. Later enrichment
and reporting must remain reproducible from stored raw and canonical data.

These invariants need a concrete persistence boundary before source adapters
or collection workflows are implemented.

## Decision

Use synchronous SQLAlchemy 2 with psycopg 3 for the CLI/application boundary
and Alembic for explicit PostgreSQL migrations. Engine creation is lazy, and
session scopes commit on success and roll back on failure.

The initial revision creates seven tables:

- `sources` and `collection_runs` record configured boards and scan outcomes;
- `job_postings` stores the latest canonical state and lifecycle timestamps;
- `job_versions` stores immutable changed snapshots and raw evidence;
- `job_locations`, `job_skills`, and `job_compensation` store repeatable,
  evidence-bearing normalized components.

A job is unique by `(source_id, external_id)`. A version is unique by
`(job_id, content_hash)`. Skill evidence has its own UUID identity so multiple
evidence rows can represent the same job and canonical skill without forcing
evidence text into a primary key.

Use PostgreSQL-native UUID, JSONB, and text-array types. Closed vocabularies,
confidence ranges, non-negative counters, temporal ordering, country/currency
shapes, and compensation bounds are enforced with named check constraints.
Foreign keys do not cascade deletes: ingestion retains history and does not
physically delete canonical records.

Default tests inspect metadata without a database. The opt-in migration test
runs upgrade, schema inspection, and downgrade only against an empty,
disposable PostgreSQL database whose name ends in `_test`; it refuses an
existing Career OS or Alembic schema.

## Consequences

- PostgreSQL, rather than SQLite, defines persistence behavior and integration
  semantics.
- Application services must supply UUIDs; the schema does not require a UUID
  extension.
- Raw payloads and changed snapshots support offline reprocessing and audit.
- Parent deletion is intentionally blocked while dependent history exists.
- Schema changes require reviewed Alembic revisions with reversible downgrade
  logic during early development.

## Revisit when

- measured query plans justify GIN, full-text, or additional foreign-key
  indexes;
- cross-source deduplication requires a separate job-cluster identity;
- hosted retention policy introduces explicit archival or deletion workflows;
- collection concurrency requires a lease table rather than PostgreSQL
  advisory locks.
