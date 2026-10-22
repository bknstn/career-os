# Architecture Decision Records

These records explain the project's consequential architecture and product
choices in effective-date order. Existing ADRs 0001–0004 retain their original
identifiers. ADR 0000 and 0005–0016 were reconstructed on 2026-10-22 from the
implementation history; their effective dates describe when the choices took
effect. A superseded direction remains recorded as historical context.

| ADR | Effective date | Decision |
| --- | --- | --- |
| [0000](0000-project-purpose.md) | 2026-08-17 | Project purpose and evidence boundaries |
| [0001](0001-market-explorer-foundation.md) | 2026-08-17 | Modular Python foundation |
| [0002](0002-initial-persistence-schema.md) | 2026-08-17 | PostgreSQL persistence schema |
| [0003](0003-scan-diagnostics-and-ingestion-policy.md) | 2026-08-19 | Scan diagnostics and ingestion policy |
| [0004](0004-enrichment-version-ownership.md) | 2026-08-21 | Separate enrichment version markers |
| [0005](0005-opportunities-first-hybrid-reports.md) | 2026-08-25 | Prioritize opportunities and bound aggregate claims |
| [0006](0006-explicit-target-universe.md) | 2026-09-01 | Make the target universe explicit |
| [0007](0007-provenance-and-manual-career-review.md) | 2026-09-17 | Separate profile provenance from manual vacancy judgments |
| [0008](0008-append-only-compensation-evidence.md) | 2026-09-20 | Keep compensation evidence in an append-only supplement |
| [0009](0009-pause-personal-career-product.md) | 2026-09-21 | Pause the personal decision-product direction |
| [0010](0010-pivot-to-career-radar.md) | 2026-10-11 | Pivot to Career Radar with immutable observation history |
| [0011](0011-role-axes-and-contract-prototype.md) | 2026-10-17 | Separate occupation, seniority, and work context |
| [0012](0012-usable-local-radar-mvp.md) | 2026-10-17 | Ship a local exploration MVP with visible unknowns |
| [0013](0013-english-interface-and-coverage.md) | 2026-10-18 | Use an English interface and explicit title evidence |
| [0014](0014-terminal-style-interface.md) | 2026-10-19 | Adopt the terminal-style Radar interface |
| [0015](0015-reviewed-research-stream.md) | 2026-10-19 | Add a reviewed research stream beside job observations |
| [0016](0016-public-repository-boundary.md) | 2026-10-22 | Separate public source code from private operating data |
| [0017](0017-mit-license.md) | 2026-10-22 | Use the MIT license for the project |

For the current implementation, see the [architecture overview](../architecture.md).
For setup and usage, see the [project README](../../README.md).

New records use the next number and include an effective date, status, context,
decision, and consequences. Amend status and link a successor when a decision
changes; do not rewrite historical decisions as if they were always current.
