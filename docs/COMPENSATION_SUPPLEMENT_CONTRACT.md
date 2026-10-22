# Manual compensation supplement v1

Status: bounded file-backed workflow for salary evidence reported after a
recruiter conversation. It is separate from the historical H3 profile and H4
vacancy snapshot, so it does not change their digests, source text or reviews.

## Purpose

Public vacancy pages often omit compensation. This workflow records a current
role's salary and one selected opportunity's manually reported salary with the
source, date and disclosure context needed to interpret it. It compares a
current role and an opportunity only when the evidence has the same explicit
currency, gross/net basis, base/total component and employment model as the H3
target. It only converts annual and monthly values using `Decimal` arithmetic.

The workflow does not infer taxes, exchange rates, bonus/equity, base from
total compensation, contract type, currency, offer status, vacancy availability
or hiring eligibility. A recruiter range is not an offer.

## Ledger and history

`config/compensation-supplement.example.yaml` has one `compensation_ledger`
root with:

- `sources`: a user statement recording where the user learned the fact and when
  it was recorded. Private recruiter correspondence is described, not copied or
  treated as an independently verified source.
- `observations`: a current-role or opportunity amount, its disclosed date,
  currency/period, gross or net, base or total, UoP/B2B/unknown, and disclosure
  context (`current_compensation`, `recruiter_range`, `offer` or `unknown`).
  Missing monetary details use explicit `unknown` values and do not become
  comparable evidence.
- `supersedes`: an optional previous observation ID for the *same* subject.
  Corrections append a new observation; they never replace old YAML values.

Only one active observation may exist per subject. A competing record requires
an explicit `supersedes` link, an unrelated subject cannot be superseded, and
cycles/forks are rejected. Labels and URLs may change in a correction; subject
kind and ID are the stable identity.

The public ledger contains entirely fictional values for an example current
role and opportunity. Keep real amounts and correspondence outside Git.

## Commands

Validate a ledger without database or network access:

```bash
uv run career-os career compensation validate \
  --ledger config/compensation-supplement.example.yaml
```

Append one manually prepared record. The command creates an initial ledger when
the path does not exist; otherwise it atomically writes the validated append.
Use [the record template](../config/compensation-record.example.yaml) as the
input shape.

```bash
uv run career-os career compensation record \
  --ledger config/compensation-supplement.example.yaml \
  --record path/to/new-compensation-record.yaml
```

Render a Markdown or JSON comparison. `--profile` supplies the existing H3
target floor; the command only reads that profile and does not update it.

```bash
uv run career-os career compensation compare \
  --ledger config/compensation-supplement.example.yaml \
  --profile config/career-profile.example.yaml \
  --current-role sample_current \
  --opportunity sample_opportunity \
  --output .local/reports/compensation.md
```

Invalid records, unresolved conflicts and output errors fail before the ledger
or an existing output is replaced. Markdown and JSON are projections of the
same canonical comparison, which includes the cited sources and full
supersession history.

## Floor semantics

The H3 target owns the minimum; the implementation does not hard-code a salary threshold. An opportunity whose complete comparable range lies at or above the floor
`meets` it. A range that crosses the floor is `partial`, not accepted. A range
below it is `not_met`. Unknown or mismatching opportunity metadata leaves the
floor `unknown`.

The floor is evaluated from opportunity evidence independently of whether the
current role can be compared. If a current salary is unknown, the output keeps
the difference unknown but can still show a verified opportunity floor result.
