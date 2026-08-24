# Deterministic job normalization rules

`job-normalization-v5` owns the pure, source-neutral rules in
`career_os.normalization.jobs`. The version is emitted with every output; task
22 owns selection, persistence, and bulk re-enrichment.

Inputs retain source facts separately from inferences. Structured source
location, workplace, employment, and compensation evidence wins over text.
Unknown scalar values are explicit (`NormalizedField(None)`), never guessed.

Seniority precedence is title, then source level/category, then a narrow strong
description phrase. Multiple title levels such as `Mid/Senior` conflict and
fail closed to unknown. Description evidence must bind the level to an explicit
`engineer role` or `developer role`; references to senior teammates and
aspirational growth do not establish the vacancy level. Years alone are never
seniority evidence. `Product Manager`
is a role family, not people-management evidence; `Engineering Manager` and
`People Manager` are management seniority evidence. `full-stack` wins over a
simultaneous backend/frontend marker. Platform and infrastructure classification
requires engineering context such as `Platform Engineer`, `Software Engineer -
Network Platforms`, or `Software Engineer, Infrastructure`; a bare team label
such as `Treasury Partner Lead - Platform` is not an engineering role. An
explicit non-platform family such as backend, data engineering, product
management, or DevOps wins over a simultaneous platform/infrastructure marker.
Otherwise contradictory role-family markers fail closed to unknown.

Locations retain every input value and ordinal. Parsing recognizes only a small
country alias set (including `PL`) and never turns a country-only value into a
city or splits city-name hyphens. Remote eligibility is emitted only for an
explicit local binding: e.g. `remote in EU`, `EU-only remote`, `work remotely
from Poland`, or a `Remote - Poland` location. An office in Poland or Europe is
not scope evidence. Parsing a raw location reports `text_parse`; it reports
`structured_source` only when the source separately supplies the supporting
country fact. Structured remote workplace evidence may combine with an exact
`Poland`, `EU`, or `Europe` source location; source strings such as
`Remote, Poland` and `Poland (Remote)` are also accepted. The observed
parenthesized form `Europe (Full Remote)` is accepted, while unbound `Europe`,
`Europe (Hybrid)`, and `USA (Remote)` do not produce an eligible Europe scope.
A localized remote role in another country is not widened into Europe-wide
eligibility.
An ATS location that explicitly contains `Remote` also supplies conservative
remote workplace evidence unless that same location says hybrid or onsite.
Observed Greenhouse location names separated by semicolons are replayed as
ordered independent locations, preserving the first value as primary.

Structured workplace values win. `source_is_remote=false` prevents prose from
claiming `remote` but does not manufacture `onsite`; affirmative `hybrid` still
has its own explicit rule. Negated markers such as `not remote` and `not
hybrid` are not evidence. Mixed affirmative remote/onsite markers are unknown
unless a structured workplace value or affirmative hybrid marker resolves them.
Prose requires workplace wording, not a technical adjective: accepted forms
include `fully remote`, `remote role/job/work`, `work remotely`, `hybrid
role/model/schedule/work arrangement`, and explicit onsite or office-based
work. `hybrid cloud` and `remote systems` are unknown. When a structured
workplace type conflicts with `source_is_remote`, workplace type wins for that
field but remote scope fails closed to unknown.

Employment prose is equally narrow: it requires an affirmative `full-time`,
`contract`, and similar category attached to a role/job/position/employment or
engagement. References to contractors, negated contract roles, and conflicting
categories such as `full-time or contract` remain unknown.

Structured compensation is kept as independent components and location tiers.
Observed Ashby salary, bonus, percentage-equity, cash-value-equity, and
commission components are replayed without combining them; commission uses the
controlled `other` bucket because the MVP schema has no commission category.
Greenhouse descriptions are replayed as plain compensation evidence. Text
parsing requires both a locally bound component marker and an unambiguous
currency-marked range; currency may precede or follow an amount. Component and
interval are resolved per range from its local phrase, and `raw_text` preserves
that local evidence span. It neither annualizes nor converts currencies, and
does not infer equity from vague prose. The bare `$` symbol is rejected as
locale ambiguous; `USD`, `EUR`, `PLN`, `GBP`, `€`, `£`, and `zł` are accepted.
Conflicting currencies fail closed. Numeric evidence with an unknown interval
is retained, reported as unknown, and kept in one-job groups so incomparable
amounts are not pooled. A compensation record with no numeric amount, currency,
or raw evidence is invalid. All Decimal amounts and confidences must be finite,
so NaN and infinities cannot cross the domain boundary.

The checked-in YAML golden corpus uses a duplicate-key-rejecting loader and
compares every output field, scalar provenance, location provenance, and
compensation provenance exactly. It is intentionally test-owned: runtime
normalization has no settings, database, or network dependency.
