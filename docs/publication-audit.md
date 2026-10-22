# Public repository preparation audit

Date: 2026-10-22. Scope: the intended public working tree, all publishable branch/tag commit
history after fetching published refs, and GitHub repository exposure.

## Prepared files

The README now explains the purpose, implemented capabilities, fresh local
setup, commands, data limitations, and documentation entry points. Product and
architecture decisions are indexed chronologically in ADR 0000–0016. Original
ADRs 0001–0004 retain their identifiers; retrospective records state when they
were written.

108 previously tracked inputs, generated reports, or internal planning/session
files were removed from the public index. Their original local files remain on
disk and are ignored. Public regression evidence was relocated into test
fixtures. Candidate profiles, compensation values, and review mappings used by
tests were replaced with explicitly fictional examples and updated fingerprints.
The retained screenshots show application content and were visually inspected.

Compose requires a locally supplied PostgreSQL password and binds its port to
loopback. The environment template contains no supplied password. A pinned
secret detector plus privacy checks are available through Make, pre-commit, and
CI. Exact detector exceptions contain only hashes of reviewed content
fingerprints or synthetic redaction fixtures. GitHub secret scanning, push
protection, and private vulnerability reporting were enabled and read back
successfully during preparation.

## Verification

- 112 reachable commits and 874 unique historical text blobs were examined
  (approximately 24 MB of historical text), including both published branches
  and the local commits awaiting publication.
- 27 detect-secrets detector plugins ran with normal context filtering.
  Candidate findings were reviewed; no live API credential was confirmed.
  Historical local-development DSNs, synthetic redaction values, content
  fingerprints, generated temporary path strings, and a UUID:digest monitor
  key accounted for the reviewed detector matches.
- The prepared public content passes the secret/privacy check.
- A clean export of the Git index, without ignored profiles or reports, passed
  814 tests; 59 database/live tests remained explicitly skipped.
- Ruff formatting/lint and strict mypy passed. Relative Markdown links resolve
  to public files. Compose configuration and dotenv interpolation were checked
  without starting a service.
- A disposable-repository regression verified detection of a force-added
  private file, a generated synthetic key, and deleted historical content.
  Its output did not reveal the synthetic key.

## History cleanup

The maintainer authorized history rewriting and guarded force publication on
2026-10-22. The rewrite removes the historical versions of private inputs,
reports, installation logs, and the replaced documentation/test fixtures.
All author and committer identities use the public GitHub handle and noreply
address. Empty commits containing only removed material were pruned; 61
inherited implementation commits remain before the public preparation commit.

The rewritten history and prepared files passed all 27 secret detectors and
privacy checks with zero findings. A separate historical content check found
no retained private name, actual compensation amount, or user home path.
The application regression passed against the isolated clean tree. CI exposed
a color-sensitive CLI-help assertion; the assertion now compares unstyled text
and is exercised with both colored and uncolored output. History scanning
uses branch/tag/remote commit roots so private local application snapshot tree
refs do not become part of publication checks.

Both `main` and `codex/career-radar-pivot` are published at the prepared public
tip. Force pushes use the previously observed remote SHAs as leases, preventing
an unnoticed newer remote update from being overwritten. The original local
personal files and a private recovery bundle remain outside Git tracking.

At the audit there were no pull requests, forks, tags, releases, or stored
Actions artifacts. A further Actions metadata review found 38 old workflow
runs exposing personal commit email fields. Their metadata and log archives
were preserved privately, and the old public runs were removed. GitHub secret scanning, push protection, and private
vulnerability reporting are enabled. Subsequent clones use the rewritten
history; existing collaborators must avoid reintroducing the old history.

GitHub may retain old objects in cached views even after branch rewriting.
Server-side removal is a separate GitHub Support operation. The provider's
[sensitive-data removal guide](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)
describes cached references and existing copies. This audit certifies the
checked reachable history and public files, not copies held by other people or
GitHub's internal object retention.
