"""Strict, explicit Target Universe v1 configuration.

This is deliberately not a second spelling of :mod:`markets`.  A target
universe records the user's named company/source boundary as well as the role
and geography they would consider.  The report service compiles it to the
existing lower-level market query only after validating the declared source
panel against persisted source identity.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from career_os.config.markets import MarketFilters
from career_os.config.sources import ConfigurationError, _validation_message
from career_os.domain.normalization import RoleFamily, Seniority

NonBlankText = Annotated[str, Field(min_length=1)]


class TargetUniverseSource(BaseModel):
    """One named employer and its exact configured public-source identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_key: NonBlankText
    company_name: NonBlankText

    @field_validator("company_name")
    @classmethod
    def company_name_is_not_blank(cls, value: str) -> str:
        """Keep user-facing capitalization; normalize only for comparisons."""
        if not value.strip():  # Defensive: minLength intentionally permits whitespace.
            raise ValueError("company_name must be non-blank")
        return value

    @property
    def normalized_company_name(self) -> str:
        """PostgreSQL-compatible comparison form without changing display text."""
        return self.company_name.lower()


class TargetUniverseGeography(BaseModel):
    """Fail-closed country and normalized remote-scope boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    country_codes: tuple[str, ...] = Field(min_length=1)
    include_remote: bool = False
    remote_scopes: tuple[NonBlankText, ...] = ()

    @field_validator("country_codes")
    @classmethod
    def normalize_country_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.upper() for value in values)
        if any(
            len(value) != 2 or not value.isascii() or not value.isalpha() for value in normalized
        ):
            raise ValueError("must contain ISO 3166-1 alpha-2 country codes")
        if len(set(normalized)) != len(normalized):
            raise ValueError("contains duplicate country codes")
        return normalized

    @field_validator("remote_scopes")
    @classmethod
    def normalize_remote_scopes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.lower() for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("contains duplicate remote scopes after case normalization")
        return normalized

    @model_validator(mode="after")
    def remote_selection_is_explicit(self) -> TargetUniverseGeography:
        if self.include_remote and not self.remote_scopes:
            raise ValueError("include_remote requires at least one remote_scope")
        if self.remote_scopes and not self.include_remote:
            raise ValueError("remote_scopes require include_remote=true")
        return self


class TargetUniverseExclusions(BaseModel):
    """Optional, visible exclusions.  V1 intentionally has no hidden rules."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    title_contains: tuple[NonBlankText, ...] = ()

    @field_validator("title_contains")
    @classmethod
    def normalize_title_terms(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.lower() for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("contains duplicate title exclusions after case normalization")
        return normalized


class TargetUniverse(BaseModel):
    """An explicit definition of the market a user considers."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: NonBlankText
    role_families: tuple[RoleFamily, ...] = Field(min_length=1)
    seniorities: tuple[Seniority, ...] = Field(min_length=1)
    source_panel: tuple[TargetUniverseSource, ...] = Field(min_length=1)
    geography: TargetUniverseGeography
    exclusions: TargetUniverseExclusions = TargetUniverseExclusions()

    @field_validator("role_families", "seniorities")
    @classmethod
    def controlled_values_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("contains duplicate controlled values")
        return values

    @model_validator(mode="after")
    def source_panel_is_canonical(self) -> TargetUniverse:
        source_keys = tuple(item.source_key for item in self.source_panel)
        if source_keys != tuple(sorted(set(source_keys))):
            raise ValueError("source_panel source_key values must be unique and sorted")
        return self

    @property
    def source_keys(self) -> tuple[str, ...]:
        return tuple(item.source_key for item in self.source_panel)

    @property
    def source_companies(self) -> dict[str, str]:
        """User-facing declared company names by exact configured source key."""
        return {item.source_key: item.company_name for item in self.source_panel}

    @property
    def normalized_source_companies(self) -> dict[str, str]:
        """Normalized company identity by exact configured source key."""
        return {item.source_key: item.normalized_company_name for item in self.source_panel}

    def as_market_filters(self) -> MarketFilters:
        """Compile only the selection predicates needed by the existing engine.

        Source membership is intentionally *not* expressed through
        ``MarketFilters.company_names``: it is an exact source-key restriction
        owned by the target-universe runtime path.
        """
        return MarketFilters(
            name=self.name,
            title_exclude=self.exclusions.title_contains,
            role_families=self.role_families,
            seniorities=self.seniorities,
            country_codes=self.geography.country_codes,
            include_remote=self.geography.include_remote,
            remote_scopes=self.geography.remote_scopes,
            active_only=True,
        )


def load_target_universe(path: str | Path) -> TargetUniverse:
    """Load a ``target_universe:`` document, rejecting unknown root keys."""
    config_path = Path(path)
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ConfigurationError(
            f"Target universe configuration file does not exist: {config_path}"
        ) from error
    except OSError as error:
        raise ConfigurationError(
            f"Could not read target universe configuration file: {config_path}"
        ) from error
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise ConfigurationError(
            f"Malformed YAML in target universe configuration: {config_path}"
        ) from error
    if not isinstance(parsed, dict):
        raise ConfigurationError(
            "Target universe configuration must contain a mapping at the document root: "
            f"{config_path}"
        )
    value: object = parsed.get("target_universe", parsed)
    if not isinstance(value, dict):
        raise ConfigurationError(
            f"Target universe configuration must contain a target_universe mapping: {config_path}"
        )
    if "target_universe" in parsed and set(parsed) != {"target_universe"}:
        raise ConfigurationError(
            "Invalid target universe configuration: root: extra keys are not permitted"
        )
    try:
        return TargetUniverse.model_validate(value)
    except ValidationError as error:
        raise ConfigurationError(
            _validation_message("target universe configuration", error)
        ) from error
