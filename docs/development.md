# Development

Install with `uv sync --all-groups --frozen`, then run:

```sh
make check
make security
```

`make check` runs formatting, lint, strict typing, and tests. Default tests use
offline fixtures and skip PostgreSQL and live HTTP workflows. `make format`
applies formatting. Install hooks with `uv run pre-commit install`.

## PostgreSQL integration tests

Use a dedicated disposable database whose name ends in `_test`. Export
`CAREER_OS_DATABASE_URL` for that database using your private local credentials.
It must be empty before the initial migration/schema test. Follow each test's
target checks; the suite can migrate and remove schema objects.

```sh
RUN_DB_INTEGRATION=1 uv run pytest -m integration
```

## Live source tests

These contact public boards and can fail when availability or contracts change:

```sh
RUN_LIVE_SMOKE=1 uv run pytest -q -s tests/live/test_ats_live_smoke.py
```

They are opt-in and excluded from the default quality job.

## Public-content checks

```sh
make security
make security-history
```

The check runs pinned `detect-secrets` detectors plus repository privacy rules.
It reports locations and detector names without candidate values. History mode
inspects reachable commits and unique historical text blobs. Working-tree mode
checks intended public files, excluding ignored local inputs.

Redaction tests use obvious dummy values and reserved example domains.
Suppressions in `.public-content-allowlist.json` bind a reviewed fixture,
detector, and exact value hash. They contain content fingerprints or synthetic
redaction-test values; a changed value is scanned again. Automated scanning
supplements human review of prose, screenshots, provenance, and commit metadata.
