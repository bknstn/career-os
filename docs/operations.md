# Collection operations

Task 27 adds local-MVP safeguards around the existing source adapters and
complete-scan lifecycle contract. These controls use PostgreSQL and persisted
run facts; they add no scheduler, queue, external alerting system, or destructive
retention job.

## Safe collection

Every ingestion holds a PostgreSQL session-level advisory lock derived from the
source key from before run creation until all network, canonical persistence,
and terminal lifecycle work finishes. A second process collecting the same
source fails before creating a run. Different source keys remain independent.
Locks are released explicitly and are also released by PostgreSQL if the owning
connection dies.

Each adapter already has bounded attempts, exponential jitter, `Retry-After`
support, explicit request timeouts, and persisted HTTP request/retry/rate-limit
counters. The operational circuit adds a cross-run boundary: after the
configured number of consecutive `partial` or `failed` terminal runs, another
attempt is rejected until the cooldown expires. A later `succeeded` run resets
the consecutive-unsafe count. A rejected lock/circuit attempt has no run ID
because no source request or durable run was started.

A source scan that claims completeness is downgraded to `partial` when:

- it returns zero after the source's latest successful non-empty scan; or
- its fetched count drops by more than the configured fraction from that scan.

The observed postings are still safely upserted, but the partial run cannot
inactivate unseen jobs. The threshold is strict: with the default `0.5`, a drop
from 10 to 5 is accepted and a drop from 10 to 4 is guarded. The safeguard has
no prior-count opinion for a first scan or after a prior successful zero scan.

## Configuration

All values are typed and fail closed through `load_settings()`:

| Variable | Default | Meaning |
|---|---:|---|
| `CAREER_OS_MAX_FETCH_DROP_RATIO` | `0.5` | Largest accepted fractional drop; range `[0, 1)` |
| `CAREER_OS_CIRCUIT_FAILURE_THRESHOLD` | `3` | Consecutive partial/failed runs that open the circuit |
| `CAREER_OS_CIRCUIT_COOLDOWN_SECONDS` | `900` | Time before a guarded recovery attempt is allowed |
| `CAREER_OS_FRESHNESS_TARGET_SECONDS` | `86400` | Maximum age of the latest successful run |
| `CAREER_OS_REQUEST_TIMEOUT_SECONDS` | `30` | Per-request adapter timeout |

Tune the drop threshold only after inspecting the source's normal posting-count
volatility. Do not set it near `1` merely to make warnings disappear.

## Diagnosis and metrics

Run:

```bash
uv run career-os operations status
uv run career-os operations status --strict
uv run career-os runs list
uv run career-os runs show RUN_UUID
```

`operations status` emits deterministic JSON per source: active jobs, latest
terminal status and counters, last successful age, freshness, consecutive
unsafe runs, and circuit state. `--strict` exits `1` if any enabled source is
stale or circuit-open, making it suitable for cron/CI monitoring.

The loopback read API exposes `/metrics` in Prometheus text format. Labels are
limited to `source_key` and `source_kind`; job IDs and URLs never appear. It
includes active jobs, freshness age/state, circuit state, latest fetched and
rejected counts, and latest HTTP request/retry/rate-limit counters. Metrics are
derived read-only from PostgreSQL and return `503` through the API's sanitized
database/configuration error boundary when unavailable.

## Recovery playbook

1. Inspect `operations status`, then the latest partial/failed runs.
2. Confirm the source board is reachable and compare retained diagnostics with
   the adapter's recorded fixture contract. Never paste raw bodies or secrets
   into logs or issues.
3. For a zero/drop guard, verify the public board itself before changing the
   threshold. Existing jobs remain active, so waiting is safe.
4. For an open circuit, fix the source/configuration and wait for the cooldown.
   There is no hidden mutable circuit flag to clear; state is derived from run
   history. A successful recovery run closes it.
5. For an apparent overlap, locate the other `collect` process. If it crashed,
   PostgreSQL releases its advisory lock when the connection closes.

## Retention and exposure

The local MVP retains raw latest payloads, immutable job versions, and
collection-run history indefinitely. Collection never physically deletes job
history. Back up PostgreSQL before manual maintenance. Define and test a hosted
retention policy before deployment; do not delete raw evidence until reports
and audit requirements are understood.

The API is unauthenticated and must remain bound to `127.0.0.1`. `/metrics`
reveals configured source keys and operational volumes, so it must not be
published directly either.
