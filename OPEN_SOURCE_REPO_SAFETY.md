# Open-Source Repository Safety Runbook

> **Purpose:** keep a public/open-source repository free of secrets, private operational details, employer/client information, personal data, and environment-specific infrastructure details.
>
> **Audience:** humans and AI coding agents such as Codex, Claude Code, Cursor agents, GitHub Copilot agents, and similar tools.
>
> **Rule:** treat this document as a **mandatory pre-commit gate**. Before creating a commit, opening a PR, or publishing changes, run through this checklist.

---

## 1. Core principle

Assume that **everything committed to this repository may become permanently public and searchable**.

Do not commit information merely because:

- it already exists locally;
- it is present in another private repository;
- it is available in shell history;
- it appears in logs;
- it was supplied in a prompt;
- it is accessible through an environment variable;
- it is visible in a deployment environment;
- it seems harmless in isolation.

When in doubt, **generalize, replace with a placeholder, or omit it**.

---

## 2. Agent operating rules

AI coding agents working in this repository MUST follow these rules.

### Before editing

1. Read this file.
2. Treat the repository as public unless explicitly told otherwise.
3. Do not import private context from:
   - current employer repositories;
   - client projects;
   - internal documentation;
   - tickets;
   - Slack/Teams messages;
   - private prompts;
   - production environments;
   - personal notes;
   - unrelated local repositories.
4. Do not copy implementation details from proprietary code unless the user explicitly confirms that the material is safe and licensed for reuse.
5. Prefer generic examples and synthetic data.

### Before committing

The agent MUST:

1. inspect all changed files;
2. inspect the staged diff;
3. check for secrets and private identifiers;
4. check documentation, comments, fixtures, tests, config, and commit messages;
5. replace environment-specific details with safe placeholders;
6. stop and flag anything suspicious instead of guessing that it is safe.

If the user did not explicitly ask the agent to create a commit, the agent should leave the changes uncommitted.

---

## 3. Never commit

### Secrets and credentials

Never commit:

- API keys;
- access tokens;
- refresh tokens;
- OAuth secrets;
- passwords;
- session cookies;
- JWTs;
- private keys;
- signing keys;
- SSH keys;
- cloud credentials;
- database credentials;
- webhook secrets;
- encryption keys;
- `.env` files containing real values.

Safe:

```env
OPENAI_API_KEY=
DATABASE_URL=
TELEGRAM_BOT_TOKEN=
```

Unsafe:

```env
OPENAI_API_KEY=sk-real-key-here
DATABASE_URL=<database-url-with-real-embedded-credentials>
```

---

## 4. Do not expose private infrastructure

Do not commit real deployment or infrastructure details unless they are intentionally public.

Avoid:

- production hostnames;
- private DNS names;
- real IP addresses;
- VPS addresses;
- cloud account IDs;
- tenant IDs;
- Kubernetes cluster names;
- internal service names;
- database hostnames;
- bucket names;
- private registry URLs;
- VPN details;
- SSH usernames;
- internal ports when they reveal topology;
- monitoring endpoints;
- admin URLs;
- deployment paths tied to a real machine;
- internal CI/CD URLs.

Prefer:

```yaml
host: ${APP_HOST}
database_url: ${DATABASE_URL}
```

or:

```text
api.example.com
db.internal.example
127.0.0.1
localhost
YOUR_SERVER_IP
YOUR_PROJECT_ID
```

---

## 5. Do not leak employer or client context

The repository must not reveal details about current or former work unless those details are already intentionally public and relevant.

Do not include:

- employer-internal project names;
- private product codenames;
- client names;
- ticket IDs;
- Jira links;
- internal architecture;
- internal APIs;
- internal database schemas;
- proprietary business logic;
- internal incident details;
- internal metrics;
- internal team names;
- private GitHub/GitLab URLs;
- internal Slack/Teams links;
- confidential roadmap information.

Bad:

```text
This reproduces the billing flow used by <current-employer> in service X.
```

Better:

```text
This example demonstrates a generic subscription billing flow.
```

---

## 6. Do not leak personal data

Do not commit real:

- names;
- email addresses;
- phone numbers;
- home addresses;
- customer IDs;
- account numbers;
- device IDs;
- personal access URLs;
- calendar data;
- health data;
- financial data;
- user messages.

Use synthetic fixtures:

```json
{
  "name": "Jane Doe",
  "email": "jane@example.com",
  "user_id": "user_123"
}
```

---

## 7. Configuration policy

Real configuration belongs outside Git.

Use:

- environment variables;
- `.env.example`;
- documented placeholders;
- secret managers;
- local untracked config.

Recommended:

```text
.env
.env.local
.env.production
secrets/
credentials/
```

should be ignored by Git.

Example `.gitignore`:

```gitignore
.env
.env.*
!.env.example

secrets/
credentials/

*.pem
*.key
*.p12
*.pfx
```

---

## 8. Deployment files

Deployment examples are allowed, but they must be reusable and sanitized.

Good:

```yaml
services:
  app:
    image: your-org/your-app:latest
    environment:
      DATABASE_URL: ${DATABASE_URL}
      OPENAI_API_KEY: ${OPENAI_API_KEY}
```

Avoid:

```yaml
services:
  app:
    image: registry.private-company.net/team/project:latest
    environment:
      DATABASE_URL: <production-database-url-with-embedded-credentials>
```

Public documentation should explain **how to deploy**, not expose **where the maintainer personally deploys**.

---

## 9. AI prompts and agent instructions

Prompts are source code for an AI product and should be reviewed like any other source file.

Before committing prompts, remove:

- private names;
- employer-specific context;
- internal workflows;
- private URLs;
- credentials;
- customer examples;
- copied proprietary material;
- irrelevant personal context.

Prefer generic instructions that work for any user or deployment.

---

## 10. Logs, fixtures, snapshots, and test data

These are common leak sources.

Review carefully:

```text
logs/
fixtures/
snapshots/
cassettes/
test-data/
examples/
debug/
tmp/
```

Do not use copied production payloads.

Create synthetic examples instead.

Bad:

```json
{
  "email": "<real-person-email>",
  "authorization": "Bearer eyJ..."
}
```

Good:

```json
{
  "email": "user@example.com",
  "authorization": "Bearer TEST_TOKEN"
}
```

---

## 11. Comments and documentation

Secrets are not the only risk.

Comments can leak architecture and operational context too.

Bad:

```python
# Production currently runs on our Hetzner VPS at 203.0.113.42.
```

Good:

```python
# Configure the production host through the deployment environment.
```

Bad:

```text
We use the same retry strategy as the payments service at <company>.
```

Good:

```text
The worker uses exponential backoff for transient failures.
```

---

## 12. Commit messages and PR descriptions

Public history is part of the repository.

Do not put private information in:

- commit messages;
- branch names;
- PR titles;
- PR descriptions;
- issue descriptions;
- release notes.

Bad:

```text
fix integration with internal-arrive-billing-prod
```

Good:

```text
fix retry handling for billing integration
```

---

## 13. Mandatory pre-commit review

Before every commit, inspect:

```bash
git status --short
```

Then inspect the full diff:

```bash
git diff
git diff --staged
```

Check filenames:

```bash
git diff --name-only
git diff --staged --name-only
```

Look for suspicious strings:

```bash
git grep -n -I -E \
'(api[_-]?key|secret|token|password|passwd|authorization|bearer|private[_-]?key|client[_-]?secret)'
```

Also inspect newly created files that may not yet be tracked.

---

## 14. Secret scanning

If available, run an automated scanner before publishing.

Recommended tools include:

```bash
gitleaks detect
```

or equivalent secret-scanning tooling.

Automated scanning does **not** replace manual review.

It usually will not detect:

- private architecture;
- employer names;
- personal information;
- sensitive URLs;
- proprietary implementation details;
- operational context.

---

## 15. Repository history matters

Deleting a secret in a later commit does not make it safe.

If a secret was committed:

1. assume it is compromised;
2. revoke or rotate it immediately;
3. remove it from the current tree;
4. rewrite Git history if appropriate;
5. force-push only after understanding the impact on collaborators.

Never rely on history rewriting as a substitute for credential rotation.

---

## 16. Dependency and license check

Before adding a dependency, verify that its license is compatible with the repository's license and distribution model.

Prefer dependencies with clearly stated licenses.

Be especially careful with:

- GPL;
- AGPL;
- non-commercial licenses;
- source-available licenses;
- custom AI model licenses;
- copied snippets with unclear provenance.

Do not assume that code found online is free to reuse.

---

## 17. Safe placeholders

Use consistent placeholders.

### Domains

```text
example.com
api.example.com
```

### Email

```text
user@example.com
developer@example.com
```

### IDs

```text
user_123
project_123
YOUR_PROJECT_ID
```

### Credentials

```text
YOUR_API_KEY
YOUR_TOKEN
changeme
```

### Hosts

```text
localhost
127.0.0.1
YOUR_SERVER_IP
```

For documentation, prefer reserved/example values rather than real-looking production data.

---

## 18. Public-safe architecture descriptions

Architecture documentation should describe the **design**, not private operational reality.

Good:

```text
Client
  ↓
API
  ↓
Agent runtime
  ↓
LLM provider
  ↓
PostgreSQL
```

Avoid unnecessarily documenting:

```text
real hostname
real VPS
real cloud account
real database name
real deployment user
real private network
real monitoring URL
```

---

## 19. Agent pre-commit gate

Before creating a commit, an AI agent MUST answer all of the following internally:

- [ ] No secrets or credentials are present.
- [ ] No `.env` file with real values is staged.
- [ ] No private infrastructure details are present.
- [ ] No employer/client confidential information is present.
- [ ] No personal or customer data is present.
- [ ] Test data is synthetic.
- [ ] URLs, IDs, hostnames, and account names are safe for public disclosure.
- [ ] Prompts contain no private context.
- [ ] Comments and docs contain no operational leaks.
- [ ] Commit message contains no private identifiers.
- [ ] New dependencies have acceptable licenses.
- [ ] `git diff --staged` has been reviewed in full.

If any answer is uncertain:

> **Do not commit. Replace the value with a generic placeholder or ask the user whether the information is intentionally public.**

---

## 20. Definition of Done

A change is safe to publish only when:

1. it works without relying on maintainer-specific private information;
2. configuration can be supplied externally;
3. examples are synthetic;
4. documentation can be read by a stranger without revealing private operational context;
5. the staged diff passes this runbook;
6. no secret scanner reports unresolved findings.

---

# Integration with AI coding agents

This file should be referenced from the repository-level agent instructions.

## `AGENTS.md`

Add:

```md
## Public repository safety

This repository is intended to be public/open source.

Before modifying, committing, or publishing code, read and follow
`OPEN_SOURCE_REPO_SAFETY.md`.

Before every commit:
1. review the complete staged diff;
2. apply the pre-commit gate from that document;
3. remove or generalize private operational details;
4. never commit secrets, personal data, employer/client information, or
   maintainer-specific deployment configuration.

If uncertain whether information is public-safe, do not commit it.
```

## `CLAUDE.md`

The same instruction can be included for Claude Code:

```md
Before every commit, follow `OPEN_SOURCE_REPO_SAFETY.md`.

Treat the repository as public. Never import private context from unrelated
repositories, employer/client systems, local configuration, prompts, logs, or
deployment environments.

Do not create a commit until the staged diff passes the runbook's pre-commit
gate.
```

---

# Recommended repository baseline

A public repository should normally contain something like:

```text
project/
├── README.md
├── LICENSE
├── AGENTS.md
├── OPEN_SOURCE_REPO_SAFETY.md
├── .gitignore
├── .env.example
├── src/
└── tests/
```

Optional later additions:

```text
CONTRIBUTING.md
SECURITY.md
CODE_OF_CONDUCT.md
CHANGELOG.md
```

---

## Final rule

> **Public code should describe the product, not the maintainer's private environment.**

Build the repository so that another developer can clone it, provide their own
configuration, and run it without needing to know anything about the original
author's employer, accounts, infrastructure, customers, or personal setup.
