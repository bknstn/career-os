# ADR 0012: Ship a local exploration MVP with visible unknowns

- **Effective date:** 2026-10-17
- **Status:** Accepted
- **Recorded:** 2026-10-22, retrospectively from the implementation history

## Context

A useful interface for saved postings can be delivered before the project has enough historical observations for longitudinal conclusions.

## Decision

Provide a loopback-only Radar interface over the saved panel: role/skill exploration, posting evidence, filters, source quality, and a browser-local shortlist. Preserve insufficient/null comparison states when history gates fail.

## Consequences

Users can inspect present observations without implying validated trends. The interface has no accounts or authentication, and its refresh button rereads saved data rather than fetching boards.
