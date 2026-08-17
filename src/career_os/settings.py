"""Typed process settings with explicit, opt-in dotenv loading."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import Field, PostgresDsn, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from career_os.config.sources import ConfigurationError, _validation_message

_APPLICATION_SETTING_ENVIRONMENT_VARIABLES = frozenset(
    {
        "CAREER_OS_ENVIRONMENT",
        "CAREER_OS_LOG_LEVEL",
        "CAREER_OS_REQUEST_TIMEOUT_SECONDS",
        "CAREER_OS_DATABASE_URL",
    }
)
_COMPOSE_ONLY_ENVIRONMENT_VARIABLES = frozenset(
    {
        "CAREER_OS_POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    }
)
_ALLOWED_SHARED_ENVIRONMENT_VARIABLES = (
    _APPLICATION_SETTING_ENVIRONMENT_VARIABLES | _COMPOSE_ONLY_ENVIRONMENT_VARIABLES
)


class Settings(BaseSettings):
    """Process configuration read from explicitly named Career OS variables."""

    model_config = SettingsConfigDict(
        env_prefix="CAREER_OS_",
        env_file=None,
        extra="forbid",
        validate_default=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    database_url: PostgresDsn | None = None


def load_settings(
    *,
    env_file: str | Path | None = None,
    overrides: Mapping[str, object] | None = None,
) -> Settings:
    """Load typed settings; dotenv files are read only when passed explicitly.

    Explicit ``overrides`` have Pydantic's normal highest precedence over
    environment variables and an optional dotenv file.
    """
    _reject_unknown_environment_variables(os.environ)
    dotenv_settings = _load_dotenv_settings(env_file)
    try:
        # The dynamic mapping is the validation boundary; Settings validates
        # its names and values before returning a typed instance.
        runtime_overrides: dict[str, Any] = {
            **dotenv_settings,
            **dict(overrides or {}),
        }
        return Settings(**runtime_overrides)
    except ValidationError as error:
        raise ConfigurationError(_validation_message("Career OS settings", error)) from error


def _reject_unknown_environment_variables(environ: Mapping[str, str]) -> None:
    """Reject misspelled prefixed variables instead of silently ignoring them."""
    unknown_variables = sorted(
        name
        for name in environ
        if name.startswith("CAREER_OS_") and name not in _ALLOWED_SHARED_ENVIRONMENT_VARIABLES
    )
    if unknown_variables:
        joined_variables = ", ".join(unknown_variables)
        raise ConfigurationError(f"Unknown Career OS environment variable(s): {joined_variables}")


def _load_dotenv_settings(env_file: str | Path | None) -> dict[str, str | None]:
    """Read only known dotenv keys without allowing it to override real env vars."""
    if env_file is None:
        return {}

    dotenv_path = Path(env_file)
    if not dotenv_path.is_file():
        raise ConfigurationError(f"Dotenv file does not exist: {dotenv_path}")

    dotenv_values_by_name = dotenv_values(dotenv_path)
    unknown_variables = sorted(
        name for name in dotenv_values_by_name if name not in _ALLOWED_SHARED_ENVIRONMENT_VARIABLES
    )
    if unknown_variables:
        joined_variables = ", ".join(unknown_variables)
        raise ConfigurationError(f"Unknown dotenv variable(s): {joined_variables}")

    return {
        name.removeprefix("CAREER_OS_").lower(): value
        for name, value in dotenv_values_by_name.items()
        if name in _APPLICATION_SETTING_ENVIRONMENT_VARIABLES and name not in os.environ
    }
