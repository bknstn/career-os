# Contributing

Use the [README](README.md) for setup and the
[development guide](docs/development.md) for checks.

Keep changes focused. Describe the resulting behavior, evidence limits, and
validation. Add regression coverage when behavior changes. Adapters must
preserve evidence and fail closed on incomplete scans. Classification and
enrichment changes need explicit version ownership and reviewed fixtures.

Run `make check` and `make security` before proposing changes. Use your public
GitHub handle and GitHub noreply email for commit metadata; history checks
reject personal author or committer addresses. Use fictional
candidate data and reserved example domains in tests. Keep real profiles,
correspondence, pay records, tokens, backups, and generated local reports out of
Git. See [SECURITY.md](SECURITY.md).

Record consequential decisions in `docs/adr/` with the next number, effective
date, context, decision, and consequences. Keep usage instructions in the README
and guides.
