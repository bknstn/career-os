# Security and private data

Career OS is a local prototype. Its unauthenticated application launchers and
Compose PostgreSQL port bind to loopback. Keep credentials and data in your own
environment; public source code does not make an instance suitable for hosting.

Candidate profiles and compensation examples in this repository are fictional.
Keep real inputs in ignored `config/*.personal.yaml`, `config/*.local.yaml`,
`.env*`, `.local/`, or `reports/` paths. Keys and database files are also ignored.
Ignoring a path does not remove earlier Git versions; force-adding bypasses the
ignore rules.

Run `make security` before committing. CI also scans reachable history.
Detectors cannot prove the absence of every secret or personal fact. Review
prose, screenshots, fixture provenance, and commit metadata as well.

Do not quote exposed information in a public issue. Use the repository's
[private vulnerability report](https://github.com/bknstn/career-os/security/advisories/new)
and share only a location and minimal description. GitHub secret scanning and
push protection are enabled. Rotate exposed live credentials and remove private
content from files and history. Previously copied or cached public data may
need separate removal.
