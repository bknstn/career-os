# Career Radar role taxonomy and classifier

**P2 accepted by independent reviewer, 2026-10-11.** Reviewed implementation
`9406a6a`. Initial classifier `1.0.0`; taxonomy `1.0.0`.
**P4 calibration accepted by independent reviewer, 2026-10-17:** reviewed
implementation `6c9ab6d`, current classifier `1.1.0`, unchanged taxonomy `1.0.0`.
The [role-contract decision](adr/0011-role-axes-and-contract-prototype.md)
records the diagnostic evidence and limitations. The reusable
offline implementation is [`career_os.radar.roles`](../src/career_os/radar/roles.py).
This document describes its rule boundaries; it does not change the accepted
normalizer or persist classifications.

## Families

The taxonomy has exactly the eleven R1 families:

| ID | Meaning |
|---|---|
| `backend_engineering` | Backend and server-side engineering roles |
| `frontend_engineering` | Frontend and user-interface engineering roles |
| `fullstack_engineering` | Explicit full-stack engineering roles |
| `data_engineering` | Data engineering and analytics engineering roles |
| `product_engineering` | General product engineering roles |
| `platform_infrastructure_engineering` | Platform, infrastructure, DevOps, and SRE roles |
| `machine_learning_engineering` | Explicit ML or machine-learning engineering roles |
| `ai_engineering` | Explicit AI or artificial-intelligence engineering roles |
| `ai_product_engineering` | Explicit AI product engineering roles |
| `solutions_architecture` | Solutions architects and solutions engineers |
| `engineering_management` | Engineering managers and engineering leadership roles |

`unclassified` is a result bucket, not a family. Staff, Principal, Lead, Senior,
and other levels are separate from role family.

## Result and evidence

`classify_role(title, description=None)` returns a frozen result containing the
taxonomy and classifier versions; one family or `None`; status; scope; seniority;
managerial function; title context; and immutable exact-match evidence. Evidence
identifies its axis (`scope`, `role`, `seniority`, `managerial_function`, or
`context`), source (`title` or an explicit `description_duty`), stable rule ID,
and matched text. No confidence score is emitted.

Status and scope are explicit:

- `classified` has exactly one family and `in_scope` scope.
- `unclassified` is clearly technical but no listed family is supported; it
  remains in the R1 denominator as `role_bucket=unclassified`.
- `ambiguous` means one title or a generic title's direct duties support more
  than one family. It carries no family.
- `conflict` means a specific title family disagrees with an explicit duty
  section, or a title gives incompatible technical/nontechnical role heads.
- `out_of_scope` is reserved for clear nontechnical role heads such as Product
  Manager. Family or technical words elsewhere in the title do not override a
  concrete technical role head.
- `unknown_scope` is used when the title does not establish whether a role is
  technical. Descriptions cannot turn unknown scope into an in-scope role.

## Rule boundaries

Classifier `1.1.0` adds a closed list of 17 reviewed nontechnical heads, listed
in the calibration report. These must start the title, retain their exact word
order, and end at the title end or a comma, dash, vertical bar, or opening
parenthesis. Matching ignores case and repeated whitespace, while evidence
retains the original substring. These heads take scope precedence over trailing
technical context. Seniority uses the head's existing marker-conflict rules;
trailing context does not supply engineering-manager title evidence. This is
not a broad exclusion of management, support, or customer-success roles.

The same version recognizes `Full-Stack Developer`, including full-stack context,
and the leading heads `Manager, Software Engineering` and `Engineer Manager`.
They use existing families; the two manager variants also retain explicit
managerial-function evidence. Uncertain TPM, mobile, customer-success technical,
support, sales, and specialty boundaries remain unresolved under existing rules.

The title's role head determines the family. Other title words may add context,
but context alone does not choose a family. Thus `Senior Backend Engineer, AI
Platform` is backend engineering with AI and platform context; `Staff Software
Engineer, ML Platform` is platform engineering with ML context. `AI Engineer`,
`ML Engineer`, and `AI Product Engineer` are separate families. `AI Technical
Support Specialist` is technical but unclassified. `Sales Engineer` is not
assumed to be backend. `Engineering Manager, Product Engineering` is engineering
management with product context.

`Product Engineer` maps to `product_engineering`. The explicit parenthetical
specialty in `Product Engineer (Backend)` selects `backend_engineering`; this is
the only current Product Engineer specialty override. Other cross-family
disjunctions, including `Backend and Frontend Engineer` and `AI Product Engineer
/ ML Engineer`, remain ambiguous. A generic `Software Engineer` title can use
description evidence only when one family is supported by direct duty bullets
under an explicit `Responsibilities`, `You will`, or `What you'll do` heading.
The recognized bullets name a concrete object such as backend systems, user
interfaces, data pipelines, product features, infrastructure, or ML models.
Requirements, experience lists, company/team descriptions, marketing prose,
negated actions, and broad technology mentions do not qualify. Conflicting direct
duties are ambiguous; duties that contradict a specific title family are a
conflict.

Seniority is extracted only from title markers. One unambiguous level is kept;
multiple markers such as `Mid/Senior` or `Staff Principal` produce unknown
seniority with evidence for each conflict. For engineering-management titles,
an explicit level modifier such as `Senior` or `Staff` is used when present;
otherwise the role tier (`Manager`, `Director`, or `Executive`) is reported.
`Lead` is a seniority value, never a family or proof of people management.

Managerial function remains `null` without explicit evidence. An Engineering
Manager title or a direct positive duty such as `Manage a team of engineers`
sets it to true. Mentoring, supporting managers, company claims, and negative
statements do not. No rule sets it to false because the title and description
usually cannot prove absence of people-management responsibility.

## Golden corpus and version changes

[`golden_role_classification.v1.yaml`](../tests/fixtures/radar/golden_role_classification.v1.yaml)
contains seven retained current titles with `source_key` and database `job_id`,
plus separately marked synthetic edge cases. Retained titles came from the
accepted [immutable-history decision](adr/0010-pivot-to-career-radar.md);
they are current-row examples, not per-scan snapshots. The corpus covers all
eleven families and the rule boundaries above. Its tests validate provenance,
taxonomy version, and expected labels. Seven real examples do not establish
whole-corpus precision or coverage; titles with unsupported spellings may remain
unclassified.

Family IDs are stable; the taxonomy validator requires exactly these eleven
IDs. Rule changes that can alter classifications require a classifier version
change. Family additions, removals, or meaning changes require a taxonomy version
change and a new reviewed corpus. Existing `JobPosting` fields are mutable, so
these results are not historical evidence until a later P3 design persists
versioned inputs and outputs.
