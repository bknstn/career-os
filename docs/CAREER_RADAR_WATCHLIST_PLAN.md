# Career Radar Watchlist implementation plan

## Goal and scope

Let a user save companies and role/skill slices they care about, return to a
single overview, and inspect the evidence behind historical changes. This is
the first personal monitoring layer on top of Radar, supporting future Career
Thesis reviews.

The first release uses browser-local storage and the existing read-only
snapshot API. It adds no accounts, background collection, notifications,
external messaging, employee data, or inferred hiring outcomes. Existing job
shortlists remain a separate feature.

## Product contract

1. Add a Watchlist navigation entry with a useful empty state and a path to
   Explore. From Explore and role/skill views, allow watching the current
   company-board and/or occupation, specialty, skill, and text-query filters.
   Require at least one substantive filter. Explain that a company selection
   represents its configured job board, not necessarily all its vacancies.
2. Store a bounded, versioned, validated list of unique targets in localStorage.
   Persist only canonical filters and a user-facing label; reset pagination and
   inactive inventory flags. Provide removal and deduplication. Preserve the
   existing Saved jobs storage. Handle malformed and unavailable storage, and
   make persistence failures visible to users.
3. Watchlist shows each target's current saved posting count, source freshness,
   comparison state, and a link reopening its exact filters and selected period.
   Support 30/90/180-day comparisons. Separate current inventory from historical
   window counts and show dates, denominators, and sources where available.
4. Use the selected history metric and its existing quality gates. Show
   directional changes only for a valid comparable metric with numeric delta;
   retain the existing stability band and percentage-point semantics. Distinguish
   increases, decreases, stable comparisons, insufficient evidence, and request
   failures. Missing history is never zero. Do not select an unrelated metric
   merely because it has a larger change.
5. Single-company targets retain the existing multi-company quality gates.
   Show an honest explanation if comparison is unavailable. Do not invent
   headcount, hires, new openings, or closure outcomes from posting observations.
   A target without a role/skill metric can show inventory and coverage without
   asserting a directional trend.
6. Each target provides a concise evidence summary and navigation to filtered
   postings and methodology. Describe two adjacent historical windows, rather
   than implying changes since the user's last visit. No automated career
   verdict is generated.
7. Bound loading work, isolate individual target failures, and guard against
   stale asynchronous renders when navigating, removing items, or switching
   periods. Follow existing English UI, accessibility, responsive layout, and
   safe text/URL rendering conventions.

## Implementation sequence and ownership

- Planning and review: primary agent.
- Implementation: Luna with xhigh reasoning.
- Reuse the existing snapshot API and history calculation without relaxing
  evidence gates. Prefer a small reusable JavaScript module for canonical
  target storage and comparison presentation if this improves testability.
- Integrate navigation, save controls, Watchlist rendering, and responsive CSS.
- Add focused behavioral tests for canonicalization/deduplication, corrupted
  storage, persistence errors, selected-metric quality/direction, and isolated
  fetch failures or stale renders where practical. Avoid source-string tests
  as substitutes for behavior.
- Document user-visible functionality and limits in README and relevant product
  documentation. Keep all examples synthetic and public-safe.

## Acceptance and review

- Saving, reloading, reopening, changing period, and removing a target work.
- Existing Saved jobs and Explore filters continue to work.
- Insufficient evidence, stale sources, confirmed zero counts, unavailable
  sources, storage failure, and one failed target remain distinguishable.
- Values and labels agree with the snapshot API's selected metric, windows,
  denominator, quality state, and provenance.
- Perform browser verification of the main flow and a narrow/mobile layout.
- Run focused behavioral checks, then `make check` and `make security`.
- Primary agent reviews the complete diff and requests corrections from Luna.
- Remove and verify removal of all task-created temporary helpers and probes.
- Leave changes uncommitted unless explicitly requested.

## Delivery status

Implemented by Luna xhigh and independently reviewed by the primary agent.
The implementation preserves existing history calculations and quality gates;
no database migrations or collection changes were needed.

Review corrections covered period-independent deduplication, explicit applied
filter semantics, source failures and missing coverage, storage failures,
historical/current cohort differences, and navigation wrapping at tablet widths.

Validation completed:

- `make check`: 808 Python tests and 5 Node behavioral tests passed, along with
  formatting, lint, and strict typing. The Node suite is part of `make check`.
- Behavioral coverage includes saving, period changes, filter reopening,
  removal, shortlist preservation, malformed storage, comparison quality,
  bounded concurrency, individual request errors, and stale-response handling.
- Static assets are served with correct content types and dependency order,
  without accessing the database. Both JavaScript files passed syntax checks.
- Read-only checks of real company, skill, and specialty snapshots confirmed
  API compatibility and explicit missing-history states.
- Public-content scanning reported no findings.

The application release is 1.1.0. Package metadata, the runtime version, and the
editable lockfile entry agree; the changelog describes the Watchlist addition.

Browser verification was attempted, but the computer-use tool reports that
local browser use is disabled in Work settings. Desktop/mobile visual
verification remains unverified; DOM behavior and responsive CSS were reviewed.
