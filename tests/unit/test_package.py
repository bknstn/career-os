"""Smoke tests for the installable package boundary."""

import career_os


def test_package_exposes_a_version() -> None:
    """The installed package can be imported and exposes its release version."""
    assert career_os.__version__ == "0.1.0"
