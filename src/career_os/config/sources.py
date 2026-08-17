"""Strict, source-specific DTOs for public ATS board configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

NonEmptyText = Annotated[str, Field(min_length=1)]


class ConfigurationError(ValueError):
    """Raised when a local configuration file cannot be safely used."""


class SourceConfigBase(BaseModel):
    """Common, non-secret settings for every configured public job board."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    key: NonEmptyText
    company_name: NonEmptyText
    enabled: bool = True


class GreenhouseSourceConfig(SourceConfigBase):
    """Configuration for a Greenhouse Job Board API source."""

    kind: Literal["greenhouse"]
    board_token: NonEmptyText


class LeverSourceConfig(SourceConfigBase):
    """Configuration for a Lever Postings API source."""

    kind: Literal["lever"]
    site: NonEmptyText
    region: Literal["global", "eu"] = "global"


class AshbySourceConfig(SourceConfigBase):
    """Configuration for an Ashby public job postings source."""

    kind: Literal["ashby"]
    board_name: NonEmptyText
    include_compensation: bool = True


class WorkablePublicSourceConfig(SourceConfigBase):
    """Configuration for Workable's public, credential-free surface."""

    kind: Literal["workable_public"]
    account_subdomain: NonEmptyText


SourceConfig = Annotated[
    GreenhouseSourceConfig | LeverSourceConfig | AshbySourceConfig | WorkablePublicSourceConfig,
    Field(discriminator="kind"),
]


class SourcesConfig(BaseModel):
    """A complete source file, validated before any collection is attempted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sources: list[SourceConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def keys_are_unique(self) -> SourcesConfig:
        """Reject ambiguous source identities before adapters or networking exist."""
        keys = [source.key for source in self.sources]
        duplicate_keys = sorted({key for key in keys if keys.count(key) > 1})
        if duplicate_keys:
            joined_keys = ", ".join(duplicate_keys)
            raise ValueError(f"duplicate source key(s): {joined_keys}")
        return self


def load_sources_config(path: str | Path) -> SourcesConfig:
    """Load a YAML source file and return only a completely validated model."""
    config_path = Path(path)
    try:
        raw_config = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ConfigurationError(
            f"Source configuration file does not exist: {config_path}"
        ) from error
    except OSError as error:
        raise ConfigurationError(
            f"Could not read source configuration file: {config_path}"
        ) from error

    try:
        parsed_config = yaml.safe_load(raw_config)
    except yaml.YAMLError as error:
        raise ConfigurationError(
            f"Malformed YAML in source configuration: {config_path}"
        ) from error

    if not isinstance(parsed_config, dict):
        raise ConfigurationError(
            f"Source configuration must contain a mapping at the document root: {config_path}"
        )

    try:
        return SourcesConfig.model_validate(parsed_config)
    except ValidationError as error:
        raise ConfigurationError(_validation_message("source configuration", error)) from error


def _validation_message(subject: str, error: ValidationError) -> str:
    """Report useful validation locations without echoing potentially secret input values."""
    details = "; ".join(
        f"{_format_location(item['loc'])}: {item['msg']}" for item in error.errors()
    )
    return f"Invalid {subject}: {details}"


def _format_location(location: tuple[object, ...]) -> str:
    """Render a Pydantic error location as a concise dotted path."""
    return ".".join(str(part) for part in location) or "root"
