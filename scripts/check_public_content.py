"""Check intended public files and optionally all reachable Git history.

Run through the pinned detect-secrets environment in the Makefile. Candidate
values are never printed. Private ignored files are not read in worktree mode.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

# Reviewed dummy values used exclusively by redaction/encryption regression tests.
# Each exception binds the detector, exact value hash, and individual fixture.
ALLOWLIST_PATH = Path(".public-content-allowlist.json")
PUBLIC_REVISIONS = ("--branches", "--tags", "--remotes", "HEAD")

PRIVATE_PATHS = (
    ".env",
    ".env.*",
    ".local/*",
    "reports/*",
    "config/*.personal.yaml",
    "config/*.local.yaml",
    "config/profile.initial.yaml",
    "config/review.waymo-pudo.yaml",
    "config/vacancy.waymo-pudo.yaml",
    "docs/CAREER_PROFILE_DRAFT*",
    "docs/PROFILE_REVIEW*",
    "docs/TARGET_UNIVERSE_REVIEW*",
    "docs/SESSION_HANDOFF*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.db",
    "*.sqlite*",
    "*.dump",
)
EMAIL = re.compile(r"[A-Za-z0-9_.+%-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
LOCAL_PATH = re.compile(r"/(?:Users|home)/[^/\s]+/|[A-Z]:\\Users\\[^\\\s]+\\")
PROFILE_URL = re.compile(r"https?://(?:www\.)?linkedin\.com/in/", re.IGNORECASE)
URL_USERINFO = re.compile(r"https?://[^/\s\"']*$")


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def example_domain(domain: str) -> bool:
    domain = domain.lower()
    return (
        domain in {"example.com", "example.org", "example.net", "localhost"}
        or domain.endswith((".example.com", ".example.org", ".example.net", ".test", ".invalid"))
        or domain in {"noreply.github.com", "users.noreply.github.com"}
    )


def privacy_findings(path: str, content: str) -> Iterator[tuple[int, str]]:
    if path != ".env.example" and any(fnmatch.fnmatchcase(path, p) for p in PRIVATE_PATHS):
        yield 0, "private-file-path"
    for number, line in enumerate(content.splitlines(), 1):
        if LOCAL_PATH.search(line):
            yield number, "local-user-path"
        if PROFILE_URL.search(line):
            yield number, "personal-profile-url"
        if any(
            not example_domain(match[1]) and not URL_USERINFO.search(line[: match.start()])
            for match in EMAIL.finditer(line)
        ):
            yield number, "non-example-email"


def worktree_blobs() -> Iterator[tuple[str, bytes]]:
    paths = set(git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0"))
    for raw_path in sorted(paths - {b""}):
        path = Path(raw_path.decode())
        if path.is_file():
            yield path.as_posix(), path.read_bytes()


def history_blobs() -> Iterator[tuple[str, bytes]]:
    objects = git("rev-list", "--objects", *PUBLIC_REVISIONS).decode().splitlines()
    names = {line.split(" ", 1)[0]: line.split(" ", 1)[1] for line in objects if " " in line}
    if not names:
        return
    result = subprocess.run(
        ["git", "cat-file", "--batch"],
        input=("\n".join(names) + "\n").encode(),
        stdout=subprocess.PIPE,
        check=True,
    )
    stream = io.BytesIO(result.stdout)
    while header := stream.readline():
        object_id, kind, size = header.decode().strip().split()
        content = stream.read(int(size))
        stream.read(1)
        if kind == "blob":
            yield names[object_id], content


def main() -> int:
    from detect_secrets.core.scan import _process_line_based_plugins
    from detect_secrets.settings import default_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    reviewed: set[tuple[str, str, str]] = set()
    public_paths = {path for path, _ in worktree_blobs()}
    if ALLOWLIST_PATH.exists():
        for row in json.loads(ALLOWLIST_PATH.read_text()):
            if set(row) != {"filename", "type", "hashed_secret", "reason"} or row["reason"] not in {
                "reviewed-content-fingerprint",
                "reviewed-synthetic-redaction-fixture",
            }:
                raise ValueError("Invalid reviewed detector exception")
            if row["filename"] not in public_paths or row["type"] not in {
                "Hex High Entropy String",
                "Basic Auth Credentials",
                "Secret Keyword",
            }:
                raise ValueError("Exception must name a public fixture and reviewed detector")
            if not re.fullmatch(r"[a-f0-9]{40}", row["hashed_secret"]):
                raise ValueError("Detector exceptions must contain only a value hash")
            reviewed.add((row["filename"], row["type"], row["hashed_secret"]))
    findings: set[tuple[str, int, str]] = set()
    count = 0
    sources = [worktree_blobs()]
    if args.history:
        sources.append(history_blobs())
        emails = git("log", *PUBLIC_REVISIONS, "--format=%ae%n%ce").decode().splitlines()
        if any(
            not email.endswith(("@users.noreply.github.com", "@noreply.github.com"))
            for email in emails
        ):
            findings.add(("<commit metadata>", 0, "non-noreply-author-or-committer-email"))
    with default_settings():
        for blobs in sources:
            for path, raw_content in blobs:
                if b"\0" in raw_content:
                    continue
                count += 1
                content = raw_content.decode(errors="replace")
                for line, rule in privacy_findings(path, content):
                    findings.add((path, line, rule))
                if path == ALLOWLIST_PATH.as_posix():
                    continue
                # Pinned detect-secrets 1.5.0 line processor preserves its normal
                # syntax/context filters for historical blobs without scratch files.
                # scan_line() enables eager entropy search and is unsuitable here.
                for secret in _process_line_based_plugins(
                    list(enumerate(content.splitlines(), 1)), path
                ):
                    identity = (path, secret.type, secret.secret_hash)
                    if identity not in reviewed:
                        findings.add((path, secret.line_number, secret.type))
    for path, line, rule in sorted(findings)[:100]:
        print(f"{path}:{line}: {rule} (value redacted)")
    print(f"Checked {count} text files/blobs; {len(findings)} finding locations.")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
