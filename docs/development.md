# Development

Install with `uv sync --all-groups --frozen`, then run:

```sh
make check
make security
```

`make check` runs formatting, lint, strict typing, and tests. `make test` (or
`uv run pytest`) collects only `tests/unit` and `tests/contract`; both run offline.
`tests/typing` belongs to `make typecheck`. PostgreSQL and live HTTP tests require
an explicit path and opt-in. `make format` applies formatting. Install hooks with
`uv run pre-commit install`.

During development, run the affected file first, then `make check` before handing
off the change:

```sh
uv run pytest tests/unit/test_radar_mvp_history.py
```

Keep each rule at the lowest useful test boundary. Reuse existing scenarios for
additional assertions; add cases for distinct behavior or observed regressions.
Do not duplicate golden-corpus execution, test a fake's own implementation, or
pin source-file hashes and type-annotation strings. See the
[test audit](test-audit.md) for the current coverage decisions.

## PostgreSQL integration tests

Use a dedicated disposable database whose name ends in `_test`. Export
`CAREER_OS_DATABASE_URL` for that database using your private local credentials.
It must be empty before the initial migration/schema test. Follow each test's
target checks; the suite can migrate and remove schema objects.

```sh
RUN_DB_INTEGRATION=1 uv run pytest tests/integration
```

## Live source tests

These contact public boards and can fail when availability or contracts change:

```sh
RUN_LIVE_SMOKE=1 uv run pytest -q -s tests/live/test_ats_live_smoke.py
```

They are opt-in and excluded from the default quality job.
The broader source-panel probe is a separate diagnostic:

```sh
RUN_D2_PANEL_LIVE=1 uv run pytest -q -s tests/live/test_revised_source_panel.py
```

## Public-content checks

```sh
make security
make security-history
```

The check runs pinned `detect-secrets` detectors plus repository privacy rules.
It reports locations and detector names without candidate values. History mode
inspects commits and historical text blobs reachable from branches, tags,
remote-tracking branches, and HEAD. Private application snapshot tree refs
remain local and are outside the publishable commit history. Working-tree mode
checks intended public files, excluding ignored local inputs.

Redaction tests use obvious dummy values and reserved example domains.
Suppressions in `.public-content-allowlist.json` bind a reviewed fixture,
detector, and exact value hash. They contain content fingerprints or synthetic
redaction-test values; a changed value is scanned again. Automated scanning
supplements human review of prose, screenshots, provenance, and commit metadata.
