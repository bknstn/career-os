# ADR 0004: Keep source and enrichment version markers separate

**Status:** Accepted
**Date:** 2026-08-21

## Context

`job_postings.normalizer_version` identifies the source mapper and participates
in canonical source-content identity. Deterministic job normalization is an
offline post-ingestion process and must not change that marker or make an
identical source observation look changed.

## Decision

Persist `job_postings.enrichment_normalizer_version` separately. New and
source-changed postings carry `none-v1`, while canonical `normalizer_version`
continues to hold the Greenhouse/Lever/Ashby/Workable mapper version. Bulk
enrichment selects jobs whose enrichment-normalizer or taxonomy marker is
stale, then atomically replaces normalized fields/managed rows and records both
current enrichment markers.

## Consequences

Identical reingestion preserves enrichment facts and markers. A true source
content change resets both post-ingestion markers and is eligible for a later
offline `career-os enrich`; no refetch is part of this workflow. A changed
retained raw payload also resets the markers even when the canonical text hash
is unchanged, because structured location, workplace, or compensation facts
can change enrichment output outside that hash.
