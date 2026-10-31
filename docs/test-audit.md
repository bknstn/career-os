# Test audit

Scope: all 72 test modules, their fixtures, pytest configuration, Make targets,
pre-commit hooks, and the quality workflow. The initial default run collected
888 cases: 829 passed and 59 PostgreSQL/live cases skipped.

## Changes

| Finding | Resolution | Remaining protection |
| --- | --- | --- |
| H1 fixture tests reimplemented reconciliation and manually constructed hypothetical empty results | Keep schema and review-reference validation for the two published examples; remove the parallel test-only implementation | `test_h1_runtime.py` exercises the real builder, empty results, quality gates, API projection, and Markdown; accepted and empty runtime results also validate against the published schema |
| Normalization expected-output helpers validated and rebuilt already checked-in expected dictionaries | Compare the complete serialized result directly with the golden expectation | Exact comparison still checks values, evidence, ordering, missing fields, and extra fields |
| History metric tests repeatedly loaded the full skill taxonomy and analyzed identical titles/descriptions | Use a one-skill synthetic taxonomy and cache pure analysis within each fixture | All history scenarios and real role classification remain; full taxonomy extraction stays in `test_skill_extraction.py` |
| CLI help traversed every command twice for two color settings | Traverse once with color enabled and assert readable failures | Every existing help path is still exercised |
| Greenhouse stream termination replayed three existing fixtures separately | Assert termination in the existing populated and malformed scenarios; the empty scenario already compares the entire stream | Same fixtures and terminal-event invariant |
| Two profile tests loaded the same file and asserted the same skills | Merge the distinct seniority assertion into the first test | Same assertions, one load |
| A taxonomy smoke test repeated loading already required by the extraction corpus and pinned arbitrary size bounds | Remove it | Golden extraction and focused invalid-configuration tests |
| Tests exercised only their own fake adapter or asserted a typing fixture was importable | Remove those runtime tests | Real adapter contracts, registry validation, and strict mypy fixtures |
| A valid-threshold test asserted a property of objects constructed during collection | Remove it | Actual comparison tests exercise inclusive thresholds and invalid settings |
| Source-file SHA and annotation-string assertions locked implementation spelling | Remove them | Behavioral classifier corpus, immutable input fingerprints, SQL column checks, and strict typing |
| Research router path assertions repeated the app-wide OpenAPI route inventory | Keep query-validation behavior in the router test | App-wide route inventory and OpenAPI validation |
| An HTTP retry test waited on the real host throttle despite using a fake transport | Disable that interval in this test | Dedicated clock-controlled interval/deadline tests |
| Default discovery imported optional suites just to skip them | Default discovery is limited to unit and offline contract tests | Optional suites remain available through explicit paths and their existing opt-in guards |

## Coverage decisions

| Area | Decision |
| --- | --- |
| ATS contracts and ingestion mappings | Keep both: transport completeness/pagination and canonical field mapping are different boundaries. Similar retry tests exercise separate adapter implementations. |
| Collection, lifecycle, repositories, and enrichment | Keep state transitions, transaction rollback, idempotency, stale observations, and the rule that partial scans cannot inactivate jobs. Unit and database assertions protect different failure modes. |
| Normalization, skills, and role classifiers | Keep golden corpora plus regressions for evidence, conflicts, ambiguous titles, and negation. These are product behavior; the legacy and MVP classifiers have different contracts. |
| Radar aggregation and history | Keep matched-window coverage, denominators, latest-observation selection, missing versus zero, and threshold boundaries. The R1, role-contract, and MVP aggregators are separate implementations. |
| Research acquisition and evidence | Keep destination restrictions, robots rules, request limits, extraction offsets, review lineage, retractions, and report freshness. They protect data acquisition and evidence correctness. |
| Profile, review, compensation, and reports | Keep the exposed CLI/domain behavior even where the product direction has changed. Removing a feature's tests while its commands remain would leave unsupported behavior. |
| API and CLI | Keep input-validation-before-I/O, sanitized errors, exit codes, output preservation, pagination, and schema checks. These add transport behavior to domain tests. |
| Configuration and persistence metadata | Keep inexpensive offline checks, including source/market consistency. Database tests do not run in the default CI job. |
| Public-content scanner and integration database guard | Keep: these exercise repository safety tooling and prevent destructive tests against the wrong database. |
| PostgreSQL and live suites | Keep outside the default loop. Live smoke checks one board per adapter; the larger panel probe diagnoses current board availability. |

The database fixtures repeatedly migrate and dismantle the schema. That is a
potential cost in the opt-in suite, not a measured default-loop bottleneck.
Changing their isolation or removing transaction/concurrency cases needs a
disposable PostgreSQL validation run; neither is part of this cleanup.

## Validation loop

Run the affected module while editing and `make check` before handoff. Explicit
database/live commands are in [development](development.md). No new dependency,
parallel test runner, or production-code change is required.

The initial default run took 10.18 seconds (829 passed, 59 skipped). The final
default run took 7.39 seconds (808 passed), about 27% less. Formatting, lint,
and strict mypy checks passed. All 59 optional database/live cases still collect
successfully through explicit paths. These are single local measurements, not a
performance guarantee. PostgreSQL and live endpoints were reviewed but not
executed for this audit.
