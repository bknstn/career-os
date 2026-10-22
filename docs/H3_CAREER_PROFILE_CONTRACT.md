# H3 — Career Profile v2

Status: implemented file-backed contract.

## Purpose and boundary

Capture more than technology keywords: employment, architecture, ownership,
technical leadership, people-management boundaries, business goals, outcomes,
and career intent. The input is explicitly supplied evidence; no CV extraction or automatic
career verdict is implied.

`config/career-profile.example.yaml` contains the sole `career_profile` root.
The runtime model is `career_os.config.career_profiles.CareerProfile`, version
`h3.career-profile.v2`. CLI validation emits its canonical JSON:

```bash
uv run career-os profile validate --profile config/career-profile.example.yaml
```

The command requires no database, network, or source refresh. `--taxonomy`
optionally selects the skill taxonomy. Invalid input exits with code 2.
The JSON Schema is `docs/h3-career-profile.schema.json`; runtime validation also
checks cross-record invariants that JSON Schema cannot express.

## Records

- `sources`: stable ID, source kind, retained reference, locator and recording date.
  References identify the input evidence. Public examples use fictional
  references; private originals should remain outside Git.
- `employment`: stable ID, employer, formal title, start/end in YYYY-MM,
  and evidence. Null end means ongoing **as of the evidence date**. Overlapping
  months are retained; no tenure sum or backend-years estimate is generated.
- `skills`: canonical taxonomy ID, explicitly declared proficiency, optional
  years, and evidence. Unspecified years stay null. Other document-mentioned
  technologies live in `skill_context` claims without invented proficiency.
- `target`: structured role families, primary/exploratory seniorities, country,
  remote policy, onsite/hybrid cities, relocation, employment model and base-pay
  floor with currency, period and gross/net basis. Bonuses/equity are not base
  pay. Undisclosed pay can be considered without establishing eligibility.
  Exploratory `lead` does not establish people-management experience. This record expresses intent and does not execute H2 filtering.
- `claims`: stable ID, bounded category, context, statement and evidence.
  Categories separate architecture, technical leadership, people management,
  business goals, outcomes, constraints, preferences and interpretations of
  direction. Context scopes a claim to an initiative, project or career.
  This first slice intentionally keeps descriptive evidence as text; it does
  not extract scope metrics or infer predicates from prose.

All substantive records carry evidence. IDs are unique within their collection;
source references resolve uniquely and cannot be duplicated. Unknown keys,
blank text, invalid dates/months, negative/non-finite pay, reversed employment
periods, duplicate target values and unknown taxonomy IDs fail closed. Sources
cannot be recorded after `evidence_as_of`.

## Evidence and review semantics

Each evidence record has one of four explicit statuses:

| Status | Meaning | Required provenance |
| --- | --- | --- |
| `user_confirmed` | Explicit self-declaration, not independently verified competence | At least one `user_statement` source |
| `document_unreviewed` | Statement extracted from supplied material, awaiting user review | At least one `document` source |
| `interpretation` | Analysis rather than an established personal fact | Explicit source references |
| `unknown` | Missing evidence, not evidence of absence | Source recording the uncertainty |

Employment and proficiency records require asserted evidence; interpretations
and unknowns belong in claims. Target intent requires user confirmation.
The model validates provenance structure, not whether prose is true or a
source actually entails it: semantic review remains necessary.

`review_status: draft` permits a mixture of the four statuses. `user_reviewed`
requires a `review_source_id` referencing an explicit user statement recording
review of this profile. Existing intake statements must not be repurposed as
profile approval. Whole-profile review never promotes individual evidence
statuses, establishes skill competence, or accepts H5 usefulness.

## Fictional example

The checked-in profile is entirely synthetic. Its employers, chronology, claim
text, sources, preferences, and compensation floor are illustrative. Evidence
statuses exercise validation boundaries; they do not assert facts about a real
person. Keep your own profile in an ignored `*.personal.yaml` file.

## Validation and next boundary

Offline tests cover migration boundaries, provenance failures, dates, taxonomy,
strict YAML, CLI success/failure and JSON round-tripping. The published schema
is pinned to the runtime model by a drift check.

H1/H2 still consume their legacy skill profile. There is no automatic adapter
that would discard H3 statuses or silently replace historical report inputs.
No DB migration, persistence refresh, vacancy classification or career scoring
is introduced. H4/H5 combines this profile with one annotated vacancy and a manually
reviewed mapping. Current vacancy availability and employment/pay constraints
still need per-vacancy verification.
