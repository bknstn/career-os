"""Smoke tests for the installable package boundary."""

import tomllib
from pathlib import Path

import career_os
from career_os.api.app import create_app


def test_package_version_matches_project_and_api() -> None:
    """Package metadata and the API must advertise the same release version."""
    project_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    project = tomllib.loads(project_path.read_text(encoding="utf-8"))
    assert career_os.__version__ == project["project"]["version"]
    assert create_app().version == career_os.__version__
