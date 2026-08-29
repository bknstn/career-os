# Market Explorer result: senior-backend-poland-eu

- Contract: `h1.hybrid-result.v1`
- Result state: `evidence_limited`
- Opportunities: **available** (3 jobs); broad-market claims: **disabled**.
- Panel observations: **qualified**; scope: `configured_source_panel`.
- Failed gates: `reviewed_eligible_jobs` observed `3` (threshold `>= 20`), `represented_companies` observed `2` (threshold `>= 5`), and `max_company_concentration` observed `66.67%` (2/3 jobs, threshold `<= 40.00%`).
- Skill semantics: canonical **mentions only**; no requirement, demand, gap, fit score, or hiring probability is inferred.

## Concrete opportunities

Every eligible matching job remains available, even though the panel observation gate failed.

| Evidence job ID | Company | Title | First seen | Last seen | Locations / remote scope | Mention evidence | Compensation evidence | Source URL |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `00000000-0000-0000-0000-000000000001` | Affirm | Senior Backend Engineer | `2026-08-29T08:00:00Z` | `2026-08-29T08:00:00Z` | PL / EU | Python (`00000000-0000-0000-0000-000000000201`) | PLN 300000–420000/year, Poland (`00000000-0000-0000-0000-000000000101`) | [source](https://jobs.example.test/affirm/1) |
| `00000000-0000-0000-0000-000000000002` | Affirm | Staff Platform Engineer | `2026-08-29T08:01:00Z` | `2026-08-29T08:05:00Z` | PL / EU | Python (`00000000-0000-0000-0000-000000000202`) | none | [source](https://jobs.example.test/affirm/2) |
| `00000000-0000-0000-0000-000000000003` | Beta Cloud | Senior Backend Engineer | `2026-08-29T08:02:00Z` | `2026-08-29T08:10:00Z` | PL / unknown | none | PLN 250000–350000/unknown interval, Europe (`00000000-0000-0000-0000-000000000103`) | [source](https://jobs.example.test/beta-cloud/1) |

## Panel-bounded observations (configured source panel)

All observations below describe only this configured public-source panel and its 3-job matching sample.

### Evidence quality

| Gate | Status | Threshold | Observed | Basis | Provenance |
| --- | --- | --- | --- | --- | --- |
| `reviewed_eligible_jobs` | failed | `>= 20` | `3` | manual review | `h1_evidence_limited_review_artifact.json` |
| `represented_companies` | failed | `>= 5` | `2` | computed | `opportunities.items` |
| `max_company_concentration` | failed | `<= 40.00%` | `66.67%` (2/3) | computed | `observations.sample.companies` |
| `demonstrated_eligibility_false_positives` | passed | `== 0` | `0` | manual review | `h1_evidence_limited_review_artifact.json` |
| `skill_semantics` | passed | `== mentions_only` | `mentions_only` | contract invariant | `quality.claim_policy.skill_semantics` |
| `compensation_reconciled` | passed | `== reconciled` | `reconciled` | contract invariant | `per_job.compensation` |

### Sample

- Matching jobs: **3**; evidence job IDs: `00000000-0000-0000-0000-000000000001`, `00000000-0000-0000-0000-000000000002`, `00000000-0000-0000-0000-000000000003`.
- Companies: **2** (`Affirm`, `Beta Cloud`).
- Maximum company concentration: **Affirm**, 2/3 jobs, **66.67%**; evidence job IDs: `00000000-0000-0000-0000-000000000001`, `00000000-0000-0000-0000-000000000002`.
- Collection cutoff: `2026-08-29T08:10:00Z` (maximum persisted `last_seen_at`; no collection-time age inferred).
- Skill denominator: 3 jobs with non-empty descriptions; compensation denominator: 3 matching jobs.

### Source coverage

- Configured sources: **2** (`affirm`, `beta-cloud`); matching sources: **2**; source coverage: **100.00%**.
- `affirm`: job IDs `00000000-0000-0000-0000-000000000001`, `00000000-0000-0000-0000-000000000002`.
- `beta-cloud`: job ID `00000000-0000-0000-0000-000000000003`.

### Skill mentions

| Skill | Semantics | Jobs | Job share | Companies | Company share | Evidence job IDs | Denominator job IDs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Python (`python`) | mention | 2 | 66.67% | 1 (`Affirm`) | 50.00% of 2 described companies | `00000000-0000-0000-0000-000000000001`, `00000000-0000-0000-0000-000000000002` | `00000000-0000-0000-0000-000000000001`, `00000000-0000-0000-0000-000000000002`, `00000000-0000-0000-0000-000000000003` |

Company breadth is distinct from job share; the Python mention is concentrated in one employer.

### Distributions

Seniority, workplace, and country buckets retain their exact job IDs and denominators from the sample. Empty or zero-denominator buckets are `undefined`, never zero by implication.

### Compensation

Coverage: **66.67%** (2/3 jobs); evidence job IDs: `00000000-0000-0000-0000-000000000001`, `00000000-0000-0000-0000-000000000003`. Amounts remain in original currency and interval; no conversion, annualization, component composition, or pooling is performed. The `unknown` interval component for job `00000000-0000-0000-0000-000000000003` remains a per-job group.

### Profile mention observations

Python is a covered canonical mention. Kafka is neutral because no current mention evidence exists. These are mention observations, not requirements, gaps, recommendations, or a fit score.

## Exact filters and profile metadata

The exact validated query filters and reviewed profile are retained after the result sections as audit context; they do not become an inferred target universe.
