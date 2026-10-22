# H1 hybrid result contract

**Status:** accepted design
**Decision date:** 2026-08-29 (Europe/Warsaw)
**Contract version:** `h1.hybrid-result.v1`
**Applies to:** Market Explorer Markdown reports and the read API
**Predecessor:** [ADR 0005](adr/0005-opportunities-first-hybrid-reports.md)

## Decision

Market Explorer emits one logical result with two evidence levels:

1. **Concrete opportunities** are the primary result. Every eligible stored job
   is present, with its exact job ID, employer, title, source identity, source
   URL (including an explicit `null` when none is stored), persisted
   first/last-seen timestamps, normalized locations/remote scope, per-job skill
   mention evidence, and source-preserved compensation components.
2. **Panel-bounded observations** are secondary. Counts, shares, skill
   mentions, company breadth, distributions, and compensation describe only
   the configured public-source panel and the exact matching sample.

Markdown is a deterministic presentation of this logical result. The read API
serializes the same fields; neither transport invents a claim or recomputes a
denominator. The canonical machine-readable example and schema are:

- [`h1_evidence_limited_result.json`](../tests/fixtures/contracts/h1_evidence_limited_result.json)
- [`h1_accepted_result.json`](../tests/fixtures/contracts/h1_accepted_result.json)
- [`h1-hybrid-result-contract.schema.json`](h1-hybrid-result-contract.schema.json)

The fixtures are deliberately small and synthetic. They test contract
invariants, not market validity.

## Product boundary

H1 uses the current configured source panel. It does **not** introduce the H2
target-universe model.

- `filters` remain the exact current Market Explorer query input.
- `scope.kind` is always `configured_source_panel` in H1.
- The result must not call the configured panel a whole labor market.
- `scope.target_universe` is `null` in H1; it must not be inferred from
  `filters`, source names, or profile metadata.
- A future H2 contract may add an explicit target universe and then bound the
  same opportunity/observation shapes to it.

No source discovery, adapter change, ingestion refresh, frontend, scoring,
LLM, or H2 implementation is part of this contract decision.

## Canonical envelope

The transport-neutral envelope has these top-level fields, in this logical
order:

```text
contract_version
scope
filters
profile
quality
opportunities
observations
```

`contract_version` is `h1.hybrid-result.v1`. `filters` and `profile` retain the
existing validated request shapes. The new H1 fields are described below.

### Scope

```json
{
  "kind": "configured_source_panel",
  "name": "senior-backend-poland-eu",
  "target_universe": null
}
```

`name` is the exact report/market name. `target_universe` is `null` in H1 to
make the boundary explicit. An H1 producer must not populate it with an
inferred universe.

### Quality state and claim policy

`quality.state` is machine-readable and is one of:

- `accepted`: every required quality gate passed for this configured panel;
- `evidence_limited`: at least one required gate failed or could not be
  evaluated.

The envelope always includes:

```json
{
  "state": "evidence_limited",
  "claim_policy": {
    "opportunities": "available",
    "panel_observations": "qualified",
    "broad_market_claims": "disabled",
    "skill_semantics": "mentions_only",
    "fit_probability": "not_provided"
  },
  "gates": [],
  "failed_gates": [],
  "observed_values": {}
}
```

`claim_policy.opportunities` is always `available`, including when the result
is evidence-limited. `broad_market_claims` is always `disabled` for the
configured panel. `panel_observations` is `panel_bounded` for an accepted
sample and `qualified` for an evidence-limited sample.

Each required gate is an object with:

```json
{
  "gate_id": "max_company_concentration",
  "status": "failed",
  "operator": "<=",
  "threshold": 0.4,
  "observed": 0.6666666667,
  "unit": "share",
  "evidence_job_ids": ["..."],
  "evidence_company_names": ["..."]
}
```

`status` is `passed`, `failed`, or `not_evaluable`. `failed_gates` contains the
IDs of every gate whose status is not `passed`, including `not_evaluable`.
`observed_values` repeats the decision-relevant values in a stable summary so
clients do not need to parse prose. Ratios are JSON numbers in `[0, 1]`; a
ratio is `null` when its denominator is zero or the value is not defined.

Every gate also has a same-ID entry in `quality.evaluation_provenance`. Its
`basis` is exactly one of `computed` (derived from this result),
`contract_invariant` (required by this contract), or `manual_review` (a human
reviewed evidence set). The nested provenance includes a non-empty `ref` and
the reviewed job IDs; manual-review rows must identify the reviewed rows when
they support a passed/failed decision. In particular, `observed: 0` for the
false-positive gate is accepted only with manual-review provenance and a
non-empty reviewed-job-ID set; `not_evaluable` may instead document an empty
review set and why the evidence was unavailable.

H1 evaluates this exact required gate policy, carried forward from the D9
product gate:

| Gate ID | Rule | Evidence |
| --- | --- | --- |
| `reviewed_eligible_jobs` | reviewed eligible jobs `>= 20` | every reviewed job ID |
| `represented_companies` | represented companies `>= 5` | matching job and company IDs/names |
| `max_company_concentration` | largest company job share `<= 0.40` | company and job IDs |
| `demonstrated_eligibility_false_positives` | reviewed false positives `== 0` | false-positive job IDs (empty on pass) |
| `skill_semantics` | `mentions_only` | skill rows and per-job mention evidence |
| `compensation_reconciled` | all reported components reconcile to source-preserved per-job components | component and job IDs |

All six gates are always present. A gate is `not_evaluable` when the result
does not contain the evidence needed to decide it; `not_evaluable` is included
in `failed_gates` and makes the result `evidence_limited`. `accepted` means all
six gates are `passed`, not merely that a sample is non-empty. The
concentration threshold and sample targets are the existing D9 policy.
Compensation coverage and source coverage remain diagnostics as well as input
to the compensation gate's reconciliation evidence; a low compensation share
must never make a concrete opportunity disappear.

### Opportunities

`opportunities.job_ids` is exactly the sorted UUID sequence of
`opportunities.items[*].job_id`; `count` and `company_count` are derived from
that sequence. Items are sorted by `job_id` ascending for deterministic
reconciliation.

Each item contains the fields below. `locations`, `skill_mentions`, and
`compensation` may be empty, but are never omitted. This keeps the primary
opportunity response self-contained and avoids requiring a second API call to
recover evidence.

```json
{
  "job_id": "00000000-0000-0000-0000-000000000001",
  "company_name": "Example",
  "title": "Senior Backend Engineer",
  "source": {
    "key": "example",
    "kind": "greenhouse",
    "url": "https://jobs.example.test/one"
  },
  "freshness": {
    "first_seen_at": "2026-08-29T08:00:00Z",
    "last_seen_at": "2026-08-29T08:05:00Z"
  },
  "locations": [
    {
      "ordinal": 0,
      "city": null,
      "region": null,
      "country_code": "PL",
      "is_primary": true,
      "confidence": 1.0
    }
  ],
  "remote_scope": "EU",
  "skill_mentions": [
    {
      "id": "00000000-0000-0000-0000-000000000201",
      "skill_id": "python",
      "matched_text": "Python",
      "section": "requirements",
      "method": "exact",
      "confidence": 1.0,
      "taxonomy_version": "1.0.2"
    }
  ],
  "compensation": [
    {
      "component_id": "00000000-0000-0000-0000-000000000101",
      "component_type": "base_salary",
      "min_value": 300000,
      "max_value": 420000,
      "currency": "PLN",
      "interval": "year",
      "location_tier": "Poland",
      "source_method": "structured_source"
    }
  ],
  "status": "active",
  "role_family": "backend_engineering",
  "seniority": "senior",
  "workplace_type": "remote"
}
```

The source URL is the stored canonical URL; it is never reconstructed from a
source key. `null` is preserved when no URL is stored. Freshness reports the
persisted observation timestamps only. H1 does not turn them into an age,
freshness score, or claim about collection time.

### Panel-bounded observations

`observations.scope` is always `configured_source_panel`. The observations
section must visibly use that label in Markdown and expose it as a machine
field in the API.

`observations.sample` contains:

- `job_ids` and `job_count` for the exact matching selection;
- `company_names` and `company_count` for distinct stored company names;
- `max_company_concentration` with numerator/denominator shares and exact job
  IDs;
- `source_coverage`, including the configured source keys, matching source
  keys, and job IDs by source;
- `collection_cutoff`, the maximum `last_seen_at` over matching jobs, or
  `null` when no jobs match;
- `skill_denominator_job_ids`, matching jobs with non-empty descriptions;
- `compensation_job_ids`, matching jobs with at least one reportable numeric
  compensation component.

The job-level `locations`, `remote_scope`, `skill_mentions`, and
`compensation` arrays are the same evidence used by the aggregate rows. A
consumer does not need to call `/v1/jobs/{job_id}` to explain an opportunity or
reconcile its observation. The detail endpoint may still expose richer
inspection fields, but it is not part of the H1 result contract.

Source coverage is an inventory of the panel, not a claim that a source with
zero matching jobs is broken:

```json
{
  "configured_source_count": 2,
  "configured_source_keys": ["alpha", "beta"],
  "matching_source_count": 2,
  "matching_source_keys": ["alpha", "beta"],
  "source_share": 1.0,
  "job_ids_by_source": [
    {"source_key": "alpha", "job_ids": ["..."]},
    {"source_key": "beta", "job_ids": ["..."]}
  ]
}
```

Every company, distribution, skill, and compensation row carries its exact
`job_ids` and `denominator_job_ids`. A row's `job_ids` must be a subset of its
denominator. The denominator must be stated even when a share is `null`.

#### Company concentration and company buckets

Company buckets use exact stored `company_name` values and the matching-job
denominator. The maximum-concentration record is deterministic: highest share,
then highest job count, then lexicographically smallest company name. Ties do
not change the result state; the selected record is just the stable evidence
representative.

#### Skill mentions

Every skill row is explicitly a **mention observation**:

```json
{
  "skill_id": "python",
  "label": "Python",
  "semantics": "mention",
  "job_ids": ["..."],
  "job_count": 1,
  "job_share": 0.5,
  "denominator_job_ids": ["...", "..."],
  "company_names": ["Example"],
  "company_count": 1,
  "company_share": 0.5,
  "denominator_company_names": ["Example", "Other"]
}
```

`company_count` and `company_share` are distinct-employer breadth beside the
job share. Their denominator is the distinct company names represented by
`denominator_job_ids` (the jobs with non-empty descriptions), not an inferred
whole-market company population. Company names and job IDs are sorted. A
mention is not a requirement, demand signal, gap, recommendation, or hiring
probability. Until H4 classification exists, the contract must not use
`required`, `demanded`, or `gap` as a skill interpretation.

Profile comparison remains mention vocabulary only. The canonical label is
`profile_mentions`, with `covered_skill_ids`, `neutral_skill_ids`, and
`uncovered_mentions`; it must not emit a fit score or silently convert an
uncovered mention into a career gap.

#### Compensation

Compensation coverage is:

```text
compensation_job_ids / matching_job_ids
```

Only jobs with reportable numeric components enter `compensation_job_ids`.
Per-job `compensation` carries the exact source component ID, original numeric
values, currency, interval, location tier, and source method. Each aggregate
group retains the source component type, original currency, original interval,
original location tier, component IDs, job IDs, denominator IDs, and observed
min/max values. The aggregate component IDs must equal the union of the
per-job component IDs, and its job IDs must equal the jobs carrying those
components.

The producer must not:

- convert currencies;
- annualize or otherwise convert intervals;
- combine base, bonus, equity, or other component types;
- combine location tiers;
- pool components with unknown intervals across jobs.

`interval: "unknown"` is a real machine value. Unknown-interval groups are
kept per job, so two jobs with unknown intervals never produce one pooled
range. Unknown values and zero denominators remain explicit as `null`, empty
IDs, or a documented unknown group; they are not treated as zero.

### Markdown/API parity

The Markdown report must use this section order after the title and a compact
quality notice:

1. `## Concrete opportunities` — exhaustive eligible jobs first;
2. `## Panel-bounded observations (configured source panel)` — sample,
   quality, source coverage, company/concentration, skills, distributions,
   compensation, and profile mentions;
3. exact filters and profile metadata as audit context.

An evidence-limited Markdown report must say that opportunities remain
available, identify the failed gate and observed value, and visibly qualify
panel observations while disabling broad-market interpretation. The API carries
the same information in `quality`, `opportunities`, and `observations`; it must
not return a second aggregate shape with different IDs or denominators.

## Acceptance criteria

H1 is accepted when the contract artifacts demonstrate all of the following:

- both `accepted` and `evidence_limited` states are machine-readable;
- all six D9 gates are present in fixed order, with matching evaluation
  provenance; manual false-positive `observed: 0` requires reviewed row IDs;
- every eligible job survives an evidence-limited state and has exact source
  and freshness evidence;
- panel scope, sample size, company count, maximum concentration, source
  coverage, and collection cutoff are present;
- skill rows contain both job share and distinct-company breadth and say
  `mention`;
- compensation remains original and unpooled, including unknown intervals;
- every aggregate reconciles to exact job IDs and declared denominators;
- existing validated `filters` and `profile` shapes remain strict and explicit;
- zero denominators are represented as `null` diagnostics, never as zero;
- the Markdown fixture places opportunities before panel observations;
- no fixture introduces a target-universe implementation, score, LLM, source
  expansion, or ingestion change.

`tests/unit/test_h1_contract_fixtures.py` is the executable acceptance check.
Passing it accepts the design only; the smallest runtime implementation slice
is a separate post-H1 decision.
