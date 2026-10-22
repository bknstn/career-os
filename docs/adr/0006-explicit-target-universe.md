# ADR 0006: Make the target universe explicit

- **Effective date:** 2026-09-01
- **Status:** Accepted
- **Recorded:** 2026-10-22, retrospectively from the implementation history

## Context

A report needs a visible definition of which role families, levels, companies, locations, and work modes are in scope. Hidden or inferred preferences make opportunity selection difficult to audit.

## Decision

Validate a strict target-universe YAML input and expose a separate H2 result contract. Match declared company and source identities against persisted enabled sources. Keep this input mutually exclusive with the legacy market filter.

## Consequences

Reports can explain their selection boundary. A template is not a confirmed personal target, and the workflow does not silently discover or expand the source panel.
