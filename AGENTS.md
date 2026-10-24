## Open-source repository safety

This repository is intended to be public/open source.

Before modifying, committing, or publishing code, read and follow
`OPEN_SOURCE_REPO_SAFETY.md`.

Treat the rules in that document as mandatory repository-level instructions.

Before every commit:
- review the complete staged diff;
- apply the pre-commit safety gate defined in `OPEN_SOURCE_REPO_SAFETY.md`;
- remove or generalize private operational details;
- never commit secrets, credentials, personal data, employer/client information,
  proprietary context, or maintainer-specific deployment configuration.

If you are uncertain whether some information is safe for a public repository,
do not commit it. Prefer a generic placeholder or ask for clarification.
