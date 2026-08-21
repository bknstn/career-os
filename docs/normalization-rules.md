# Deterministic job normalization rules

`job-normalization-v1` owns the pure, source-neutral rules in
`career_os.normalization.jobs`. The version is emitted with every output; task
22 owns selection, persistence, and bulk re-enrichment.

Inputs retain source facts separately from inferences. Structured source
location, workplace, employment, and compensation evidence wins over text.
Unknown scalar values are explicit (`NormalizedField(None)`), never guessed.

Seniority precedence is title, then source level/category, then a narrow strong
description phrase. Years alone are never seniority evidence. `Product Manager`
is a role family, not people-management evidence; `Engineering Manager` and
`People Manager` are management seniority evidence. `full-stack` wins over a
simultaneous backend/frontend marker; other contradictory role-family markers
fail closed to unknown.

Locations retain every input value and ordinal. Parsing recognizes only a small
country alias set (including `PL`) and never turns a country-only value into a
city or splits city-name hyphens. Remote eligibility is emitted only for an
explicit local binding: e.g. `remote in EU`, `EU-only remote`, `work remotely
from Poland`, or a `Remote - Poland` location. An office in Poland or Europe is
not scope evidence. Parsing a raw location reports `text_parse`; it reports
`structured_source` only when the source separately supplies the supporting
country fact.

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
Text parsing requires a currency-marked range; component and interval are
resolved per range from its local phrase, and `raw_text` preserves that local
evidence span. It neither annualizes nor converts currencies, and does not
infer equity from vague prose. The bare `$` symbol is rejected as locale
ambiguous; `USD`, `EUR`, `PLN`, `GBP`, `€`, and `£` are accepted. A compensation
record with no numeric amount, currency, or raw evidence is invalid. All Decimal
amounts and confidences must be finite, so NaN and infinities cannot cross the
domain boundary.

The checked-in YAML golden corpus uses a duplicate-key-rejecting loader and
compares every output field, scalar provenance, location provenance, and
compensation provenance exactly. It is intentionally test-owned: runtime
normalization has no settings, database, or network dependency.
