"""Strict local configuration for deterministic market selection."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from career_os.config.sources import ConfigurationError, _validation_message
from career_os.domain.normalization import RoleFamily, Seniority

NonBlankText = Annotated[str, Field(min_length=1)]


class MarketFilters(BaseModel):
    """A complete market definition with fail-closed location semantics.

    Empty filter tuples impose no restriction.  When country filtering is in
    use, ``include_remote`` adds only explicitly remote jobs whose normalized
    scope exactly matches one of ``remote_scopes``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: NonBlankText
    title_include: tuple[NonBlankText, ...] = ()
    title_exclude: tuple[NonBlankText, ...] = ()
    role_families: tuple[RoleFamily, ...] = ()
    seniorities: tuple[Seniority, ...] = ()
    country_codes: tuple[str, ...] = ()
    include_remote: bool = False
    remote_scopes: tuple[NonBlankText, ...] = ()
    company_names: tuple[NonBlankText, ...] = ()
    active_only: bool = True

    @field_validator("title_include", "title_exclude", "remote_scopes", "company_names")
    @classmethod
    def normalize_comparison_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.lower() for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("contains duplicate values after case normalization")
        return normalized

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

    @field_validator("role_families", "seniorities")
    @classmethod
    def controlled_values_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("contains duplicate controlled values")
        return values

    @model_validator(mode="after")
    def remote_selection_is_explicit(self) -> MarketFilters:
        if self.include_remote and not self.country_codes:
            raise ValueError("include_remote requires country_codes")
        if self.include_remote and not self.remote_scopes:
            raise ValueError("include_remote requires at least one remote_scope")
        if self.remote_scopes and not self.include_remote:
            raise ValueError("remote_scopes require include_remote=true")
        return self


def load_market_filters(path: str | Path) -> MarketFilters:
    """Load either the documented ``market:`` document or its inner mapping."""
    config_path = Path(path)
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ConfigurationError(
            f"Market configuration file does not exist: {config_path}"
        ) from error
    except OSError as error:
        raise ConfigurationError(
            f"Could not read market configuration file: {config_path}"
        ) from error
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise ConfigurationError(
            f"Malformed YAML in market configuration: {config_path}"
        ) from error
    if not isinstance(parsed, dict):
        raise ConfigurationError(
            f"Market configuration must contain a mapping at the document root: {config_path}"
        )
    value: object = parsed.get("market", parsed)
    if not isinstance(value, dict):
        raise ConfigurationError(
            f"Market configuration must contain a market mapping: {config_path}"
        )
    if "market" in parsed and set(parsed) != {"market"}:
        raise ConfigurationError("Invalid market configuration: root: extra keys are not permitted")
    try:
        return MarketFilters.model_validate(value)
    except ValidationError as error:
        raise ConfigurationError(_validation_message("market configuration", error)) from error
