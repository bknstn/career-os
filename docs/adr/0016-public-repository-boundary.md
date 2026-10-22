# ADR 0016: Separate public source code from private operating data

- **Effective date:** 2026-10-22
- **Status:** Accepted; history rewritten and public branches updated
- **Recorded:** 2026-10-22, retrospectively from the implementation history

## Context

The repository became public after accumulating personal profiles, compensation records, generated reports, and installation-specific session notes. Removing current files alone leaves earlier committed content reachable.

## Decision

Publish application code, fictional examples, public regression evidence, usage guides, and chronological ADRs. Keep actual profiles, operational reports, and session logs local and ignored. Add secret/privacy checks to development and CI. Remove the shared development password and bind Compose PostgreSQL to loopback.

## Consequences

The README describes the project and its usage. The maintainer approved history rewriting and publication on 2026-10-22. Historical private versions were removed, commit identities use a public handle and noreply email, and both branches share the prepared public tip. A private recovery copy remains local. Cached old objects held by GitHub require a separate provider-side cleanup.
