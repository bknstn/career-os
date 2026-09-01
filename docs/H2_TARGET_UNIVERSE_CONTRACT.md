# H2 Target Universe v1 contract

**Status:** accepted implementation contract
**Decision date:** 2026-09-01 (Europe/Warsaw)
**Contract version:** `h2.target-universe.v1`
**Predecessor:** [`H1_HYBRID_RESULT_CONTRACT.md`](H1_HYBRID_RESULT_CONTRACT.md)

## Decision

H2 adds an explicit, first-class **Target Universe**: the roles, levels,
named companies/source boards, and location boundary that a user would
actually consider. It is not an alias for `MarketFilters`.

H1 remains available unchanged through its existing market-report CLI and API
path. H2 has a separate input and result boundary:

- CLI: `career-os market report --target-universe PATH --profile PATH --output PATH`;
- API: `POST /v1/target-universe-reports`.

The H2 result makes concrete opportunities primary. Its observations are
bounded to the explicit target universe and its declared configured
public-source panel. H2 has its own explicit-boundary quality gates; an
evidence-limited result never removes concrete opportunities or enables a
broad-market claim.

## Explicit input

The strict YAML document has one root key, `target_universe`:

```yaml
target_universe:
  name: example-review-required
  role_families: [backend_engineering, platform_engineering]
  seniorities: [senior, lead, staff]
  source_panel:
    - source_key: affirm-greenhouse
      company_name: Affirm
    - source_key: gitlab-greenhouse
      company_name: GitLab
  geography:
    country_codes: [PL]
    include_remote: true
    remote_scopes: [Poland, EU, Europe]
  exclusions:
    title_contains: [frontend, mobile, qa]
```

The checked-in file is an **unreviewed illustrative template**:
[`../config/target-universe.template.yaml`](../config/target-universe.template.yaml).
Its four source rows were selected from existing configuration only; they are
not a user-confirmed target universe. A real dogfood report requires the user
to copy the template and explicitly review every company, source key, role,
location, remote scope, and exclusion.

V1 requires all of the following:

- at least one target role family and accepted seniority;
- a non-empty, source-key-sorted `source_panel`; every row has both the named
  company and its configured source key;
- at least one ISO country code; remote work is included only when
  `include_remote: true` and every accepted normalized remote scope is stated;
- optional, explicit `exclusions.title_contains` terms. They are literal
  normalized-title exclusions, not classifier output.

`company_name` retains the user's capitalization in configuration, API, and
Markdown. A separate lowercase comparison form is used only to check the
persisted source's company identity. `source_key` is an exact configured
identity: it is not lowercased or otherwise rewritten, and its supplied order
must already be unique and lexicographically sorted. At report time, every
declared source key must resolve in the same read transaction to an **enabled**
persisted source with the same normalized company name. A missing, disabled, or
mismatched row fails closed; it is not silently dropped, broadened to a company
search, or replaced by a similar board.

The internal evidence query is compiled from the target universe only after
that model is validated. It always selects active jobs in exactly those source
keys, requested role families/seniorities, geography/remote scope, and stated
title exclusions. That compiler projection is deliberately not sent over the
H2 transport.

## Result envelope

The H2 envelope has this logical order:

```text
contract_version
scope
profile
quality
opportunities
observations
```

```json
{
  "contract_version": "h2.target-universe.v1",
  "scope": {
    "kind": "target_universe",
    "name": "example-review-required",
    "target_universe": {"...": "the exact validated input above"}
  },
  "profile": {"...": "unchanged existing Profile v1 context"},
  "quality": {"...": "the H2 explicit-universe quality shape"},
  "opportunities": {"...": "exhaustive matching stored jobs"},
  "observations": {
    "scope": "target_universe_configured_source_panel",
    "...": "reconciled sample and aggregate evidence"
  }
}
```

`scope.target_universe` is the complete, validated declaration. It is never
derived from a profile, generic market filter, source name, opportunity title,
or the set of currently matching jobs. `filters` is intentionally absent from
the H2 result, so consumers cannot mistake a query mechanism for the user's
career boundary.

The opportunity and aggregate structures retain the strict H1 invariants:
stable ID ordering, source-preserved job evidence, exact aggregate
denominators, per-job skill **mentions** (not requirements), compensation
reconciliation, and no fit probability. H2 adds these boundary invariants:

1. the configured source keys in observations equal `target_universe.source_panel`;
2. every opportunity source key and normalized company identity belongs to that
   exact panel;
3. sample and opportunities still contain exactly the same UUID sequence;
4. Markdown and API are pure projections of one immutable H2 result.

The published executable JSON Schema is
[`h2-target-universe-result-contract.schema.json`](h2-target-universe-result-contract.schema.json).
It reuses strict shared H1 evidence definitions and fixes the H2 envelope,
scope, target-universe declaration, gate order, and H2 observed values. Tests
validate both the canonical result and API serialization against it.

The retained `profile` and `profile_mentions` fields are the unchanged H1
skill-inventory context. H2 does not infer current role, scope, preferences,
experience, or constraints; those are H3 Profile v2 work.

## Evidence and quality interpretation

H2 deliberately does **not** reuse H1's representative-panel gates for
company count or concentration. A complete, explicit three-company universe is
not a low-quality sample merely because it has three companies or one company
has most of its currently open roles. Company count and concentration remain
auditable observations, not H2 quality failures.

H2 evaluates these gates in fixed order:

| Gate | Rule | Purpose |
| --- | --- | --- |
| `target_universe_boundary` | declared source panel resolves exactly | target source/company boundary integrity |
| `opportunity_evidence_reconciled` | all result evidence invariants reconcile | exact opportunity/sample/aggregate evidence |
| `reviewed_eligible_opportunities` | every current matching opportunity has manual review | eligibility review, with no arbitrary 20-job threshold |
| `demonstrated_eligibility_false_positives` | reviewed false positives `== 0` | precision of the reviewed universe |
| `skill_semantics` | `mentions_only` | no inferred requirements or gaps |
| `compensation_reconciled` | source components reconcile | compensation evidence integrity |

Without a complete manual review, the two review gates are
`not_evaluable`, so observations are `qualified`. With a complete review and
all invariant gates passed, observations are `bounded` for that exact target
universe. `broad_market_claims` is always `disabled`; neither state claims
coverage beyond the configured source panel.

## Non-goals

H2 v1 does not add inferred universes, profile v2, scoring or fit probability,
requirement classification, LLM use, source discovery/expansion, source
refresh, database persistence/migrations, a frontend, salary normalization,
or recommendation logic. Those remain later North Star layers.
