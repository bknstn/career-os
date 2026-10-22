"""Publication checks reject private inputs even if force-added to Git."""

from runpy import run_path

import pytest

CHECKER = run_path("scripts/check_public_content.py")
privacy_findings = CHECKER["privacy_findings"]
example_domain = CHECKER["example_domain"]


@pytest.mark.parametrize(
    "path",
    [".env", ".env.production", "config/profile.personal.yaml", "reports/review.md"],
)
def test_private_paths_are_rejected_without_needing_secret_content(path):
    assert (0, "private-file-path") in set(privacy_findings(path, "ordinary text"))


def test_reserved_examples_pass_and_realistic_contacts_are_rejected():
    assert not list(privacy_findings("sample.md", "Contact: user@example.org"))
    contact = "person" + "@" + "company" + ".com"
    assert (1, "non-example-email") in set(privacy_findings("sample.md", contact))
    assert not example_domain("notexample.com")
    assert not example_domain("example.org.attacker.com")


def test_user_home_paths_and_personal_profile_urls_are_rejected():
    local_path = "/".join(["", "Users", "sample", "Documents", "profile.pdf"])
    profile_url = "https://www.linkedin.com/" + "in/" + "sample"
    assert (1, "local-user-path") in set(privacy_findings("sample.md", local_path))
    assert (1, "personal-profile-url") in set(privacy_findings("sample.md", profile_url))


def test_empty_env_template_is_shareable():
    assert not list(privacy_findings(".env.example", "POSTGRES_PASSWORD="))
