# ADR 0008: Keep compensation evidence in an append-only supplement

- **Effective date:** 2026-09-20
- **Status:** Accepted
- **Recorded:** 2026-10-22, retrospectively from the implementation history

## Context

Pay information can arrive separately from a public vacancy or profile. Updating historical inputs would invalidate their digests and confuse source dates with later disclosures.

## Decision

Store dated compensation observations and sources in a separate ledger. Corrections append an explicit supersedes link for the same subject. Compare only compatible currency, pay basis, component, and employment model, using Decimal arithmetic for month/year conversion.

## Consequences

Unknown terms remain unknown. Recruiter ranges remain distinct from offers. Taxes, exchange rates, and total-to-base conversion are outside the workflow. Actual pay records stay private.
