"""Tests for fail-closed process and source-file configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from career_os.config.sources import (
    AshbySourceConfig,
    ConfigurationError,
    GreenhouseSourceConfig,
    LeverSourceConfig,
    WorkablePublicSourceConfig,
    load_sources_config,
)
from career_os.settings import load_settings


def test_loads_a_valid_mixed_source_config(tmp_path: Path) -> None:
    config_path = _write_sources(
        tmp_path,
        """
        sources:
          - key: greenhouse
            kind: greenhouse
            company_name: Greenhouse Company
            board_token: greenhouse-board
          - key: lever
            kind: lever
            company_name: Lever Company
            site: lever-company
            region: eu
          - key: ashby
            kind: ashby
            company_name: Ashby Company
            board_name: AshbyCompany
            include_compensation: false
          - key: workable
            kind: workable_public
            company_name: Workable Company
            account_subdomain: workable-company
        """,
    )

    sources = load_sources_config(config_path).sources

    assert [type(source) for source in sources] == [
        GreenhouseSourceConfig,
        LeverSourceConfig,
        AshbySourceConfig,
        WorkablePublicSourceConfig,
    ]
    assert sources[1].region == "eu"
    assert sources[2].include_compensation is False
    assert sources[3].enabled is True


@pytest.mark.parametrize(
    ("content", "expected_message"),
    [
        (
            """
            sources:
              - key: greenhouse
                kind: greenhouse
                company_name: Greenhouse Company
                board_token: greenhouse-board
            unexpected: value
            """,
            "unexpected",
        ),
        (
            """
            sources:
              - key: greenhouse
                kind: greenhouse
                company_name: Greenhouse Company
                board_token: greenhouse-board
                source_extra: value
            """,
            "source_extra",
        ),
        (
            """
            sources:
              - key: greenhouse
                kind: greenhouse
                company_name: Greenhouse Company
                board_token: greenhouse-board
                site: belongs-to-lever
            """,
            "site",
        ),
    ],
)
def test_rejects_unknown_top_level_source_and_kind_specific_keys(
    tmp_path: Path, content: str, expected_message: str
) -> None:
    config_path = _write_sources(tmp_path, content)

    with pytest.raises(ConfigurationError, match=expected_message):
        load_sources_config(config_path)


def test_rejects_duplicate_source_keys(tmp_path: Path) -> None:
    config_path = _write_sources(
        tmp_path,
        """
        sources:
          - key: duplicate
            kind: greenhouse
            company_name: First
            board_token: first
          - key: duplicate
            kind: ashby
            company_name: Second
            board_name: second
        """,
    )

    with pytest.raises(ConfigurationError, match="duplicate source key"):
        load_sources_config(config_path)


def test_rejects_an_invalid_lever_region(tmp_path: Path) -> None:
    config_path = _write_sources(
        tmp_path,
        """
        sources:
          - key: lever
            kind: lever
            company_name: Lever Company
            site: lever-company
            region: apac
        """,
    )

    with pytest.raises(ConfigurationError, match="region"):
        load_sources_config(config_path)


def test_rejects_an_unknown_source_kind(tmp_path: Path) -> None:
    config_path = _write_sources(
        tmp_path,
        """
        sources:
          - key: unknown
            kind: unknown
            company_name: Unknown Company
        """,
    )

    with pytest.raises(ConfigurationError, match="kind"):
        load_sources_config(config_path)


def test_rejects_a_missing_kind_specific_required_field(tmp_path: Path) -> None:
    config_path = _write_sources(
        tmp_path,
        """
        sources:
          - key: greenhouse
            kind: greenhouse
            company_name: Greenhouse Company
        """,
    )

    with pytest.raises(ConfigurationError, match="board_token"):
        load_sources_config(config_path)


def test_rejects_malformed_yaml(tmp_path: Path) -> None:
    config_path = _write_sources(tmp_path, "sources:\n  - key: [unterminated\n")

    with pytest.raises(ConfigurationError, match="Malformed YAML"):
        load_sources_config(config_path)


def test_rejects_a_missing_source_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="does not exist"):
        load_sources_config(tmp_path / "missing.yaml")


def test_settings_parse_environment_and_explicit_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAREER_OS_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("CAREER_OS_REQUEST_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv(
        "CAREER_OS_DATABASE_URL",
        "postgresql+psycopg://career_os:dev-only@localhost:5432/career_os",
    )

    settings = load_settings()
    overridden_settings = load_settings(overrides={"log_level": "WARNING"})

    assert settings.log_level == "DEBUG"
    assert settings.request_timeout_seconds == 12.5
    assert (
        str(settings.database_url)
        == "postgresql+psycopg://career_os:dev-only@localhost:5432/career_os"
    )
    assert overridden_settings.log_level == "WARNING"
    assert overridden_settings.request_timeout_seconds == 12.5


def test_workable_feature_flag_defaults_false_parses_booleans_and_rejects_misspellings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert load_settings().enable_workable_public is False
    monkeypatch.setenv("CAREER_OS_ENABLE_WORKABLE_PUBLIC", "true")
    assert load_settings().enable_workable_public is True
    monkeypatch.delenv("CAREER_OS_ENABLE_WORKABLE_PUBLIC")
    monkeypatch.setenv("CAREER_OS_ENABLE_WORKABLE_PUBLI", "true")
    with pytest.raises(ConfigurationError, match="ENABLE_WORKABLE_PUBLI"):
        load_settings()


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("false", False)])
def test_workable_feature_flag_parses_from_explicit_dotenv(
    tmp_path: Path, value: str, expected: bool
) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(f"CAREER_OS_ENABLE_WORKABLE_PUBLIC={value}\n", encoding="utf-8")
    assert load_settings(env_file=dotenv_path).enable_workable_public is expected


def test_settings_do_not_read_dotenv_unless_explicitly_requested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "POSTGRES_DB=career_os\nCAREER_OS_LOG_LEVEL=DEBUG\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    default_settings = load_settings()
    dotenv_settings = load_settings(env_file=dotenv_path)

    assert default_settings.log_level == "INFO"
    assert dotenv_settings.log_level == "DEBUG"


def test_loads_the_shared_dotenv_example_without_passing_compose_values_to_settings() -> None:
    dotenv_example = Path(__file__).resolve().parents[2] / ".env.example"

    settings = load_settings(env_file=dotenv_example)

    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.database_url is not None


def test_environment_values_override_explicit_dotenv_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("CAREER_OS_LOG_LEVEL=DEBUG\n", encoding="utf-8")
    monkeypatch.setenv("CAREER_OS_LOG_LEVEL", "ERROR")

    settings = load_settings(env_file=dotenv_path)

    assert settings.log_level == "ERROR"


def test_allows_exported_compose_only_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAREER_OS_POSTGRES_PORT", "55432")
    monkeypatch.setenv("POSTGRES_DB", "career_os")
    monkeypatch.setenv("POSTGRES_USER", "career_os")
    monkeypatch.setenv("POSTGRES_PASSWORD", "dev-only")

    settings = load_settings()

    assert settings.database_url is None


def test_rejects_unknown_prefixed_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAREER_OS_UNEXPECTED", "not-a-secret")

    with pytest.raises(ConfigurationError, match="CAREER_OS_UNEXPECTED"):
        load_settings()


def test_rejects_invalid_typed_environment_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAREER_OS_REQUEST_TIMEOUT_SECONDS", "forever")

    with pytest.raises(ConfigurationError, match="request_timeout_seconds"):
        load_settings()


def test_rejects_unknown_explicit_overrides_without_echoing_values() -> None:
    with pytest.raises(ConfigurationError) as error_info:
        load_settings(overrides={"unexpected": "sensitive-value"})

    assert "unexpected" in str(error_info.value)
    assert "sensitive-value" not in str(error_info.value)


def test_rejects_unknown_dotenv_fields_without_echoing_their_values(tmp_path: Path) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("CAREER_OS_UNEXPECTED=sensitive-value\n", encoding="utf-8")

    with pytest.raises(ConfigurationError) as error_info:
        load_settings(env_file=dotenv_path)

    assert "CAREER_OS_UNEXPECTED" in str(error_info.value)
    assert "sensitive-value" not in str(error_info.value)


def _write_sources(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / "sources.yaml"
    config_path.write_text(content, encoding="utf-8")
    return config_path
