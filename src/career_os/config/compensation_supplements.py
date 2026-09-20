"""Append-only manual compensation evidence, separate from H3/H4 inputs."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, ValidationError, model_validator

from career_os.config.career_profiles import Identifier, Record, Text
from career_os.config.skills import _load_yaml_mapping
from career_os.config.sources import ConfigurationError, _validation_message

Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Money = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
SubjectKind = Literal["current_role", "opportunity"]
PayPeriod = Literal["month", "year", "unknown"]
PayBasis = Literal["gross", "net", "unknown"]
PayComponent = Literal["base", "total", "unknown"]
EmploymentModel = Literal["UoP", "B2B", "unknown"]
DisclosureKind = Literal["current_compensation", "recruiter_range", "offer", "unknown"]


class CompensationSource(Record):
    """The user-reported origin of a compensation observation, not the original source."""

    id: Identifier
    kind: Literal["user_statement"]
    reference: Text
    locator: Text
    recorded_on: date


class CompensationSubject(Record):
    kind: SubjectKind
    id: Identifier
    label: Text
    reference: Text | None = None


class CompensationAmount(Record):
    minimum: Money | None = None
    maximum: Money | None = None
    currency: Currency | None = None
    period: PayPeriod
    basis: PayBasis
    component: PayComponent
    employment_model: EmploymentModel

    @model_validator(mode="after")
    def complete_or_explicitly_undisclosed(self) -> CompensationAmount:
        values = (self.minimum, self.maximum)
        if any(value is None for value in values):
            if any(value is not None for value in values):
                raise ValueError("amount minimum and maximum must be supplied together")
            if self.currency is not None:
                raise ValueError("undisclosed amount must not declare a currency")
        else:
            assert self.minimum is not None and self.maximum is not None
            if self.minimum > self.maximum:
                raise ValueError("amount minimum exceeds maximum")
        if self.currency is None and any(value is not None for value in values):
            raise ValueError("disclosed amount requires a currency")
        return self


class CompensationObservation(Record):
    id: Identifier
    subject: CompensationSubject
    amount: CompensationAmount
    disclosure_kind: DisclosureKind
    source_id: Identifier
    disclosed_on: date
    supersedes: Identifier | None = None
    note: Text | None = None

    @model_validator(mode="after")
    def declared_shape_matches_amount(self) -> CompensationObservation:
        disclosed = self.amount.minimum is not None
        if self.disclosure_kind == "unknown" and disclosed:
            raise ValueError("unknown disclosure must not supply an amount")
        if self.disclosure_kind != "unknown" and not disclosed:
            raise ValueError("known disclosure requires an amount")
        return self


class CompensationLedger(Record):
    schema_version: Literal["compensation-ledger.v1"]
    sources: tuple[CompensationSource, ...] = Field(min_length=1)
    observations: tuple[CompensationObservation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def append_only_history(self) -> CompensationLedger:
        source_ids = [source.id for source in self.sources]
        observation_ids = [observation.id for observation in self.observations]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("duplicate compensation source IDs")
        if len(set(observation_ids)) != len(observation_ids):
            raise ValueError("duplicate compensation observation IDs")
        sources = {source.id: source for source in self.sources}
        observations = {observation.id: observation for observation in self.observations}
        if missing := {item.source_id for item in self.observations} - sources.keys():
            raise ValueError("unknown compensation source reference: " + ", ".join(sorted(missing)))
        for observation in self.observations:
            if observation.disclosed_on > sources[observation.source_id].recorded_on:
                raise ValueError("compensation disclosure date exceeds source recording date")
            if observation.supersedes is not None:
                previous = observations.get(observation.supersedes)
                if previous is None:
                    raise ValueError("unknown superseded compensation observation")
                if _subject_key(previous) != _subject_key(observation):
                    raise ValueError("compensation observation may supersede only the same subject")
                if previous.disclosed_on > observation.disclosed_on:
                    raise ValueError("compensation correction precedes superseded observation")
        predecessors = [
            item.supersedes for item in self.observations if item.supersedes is not None
        ]
        if len(set(predecessors)) != len(predecessors):
            raise ValueError("compensation observation may have only one correction")
        for start in observations:
            visited: set[str] = set()
            current: str | None = start
            while current is not None:
                if current in visited:
                    raise ValueError("compensation supersession cycle")
                visited.add(current)
                current = observations[current].supersedes
        superseded = set(predecessors)
        active_by_subject: dict[tuple[SubjectKind, str], list[CompensationObservation]] = {}
        for observation in self.observations:
            if observation.id not in superseded:
                key = _subject_key(observation)
                active_by_subject.setdefault(key, []).append(observation)
        if conflicts := [items for items in active_by_subject.values() if len(items) > 1]:
            ids = ", ".join(item.id for item in conflicts[0])
            raise ValueError(
                "competing active compensation observations require explicit supersedes: " + ids
            )
        return self


class CompensationRecord(Record):
    """One appendable observation and its provenance declaration."""

    schema_version: Literal["compensation-record.v1"]
    source: CompensationSource
    observation: CompensationObservation

    @model_validator(mode="after")
    def own_source(self) -> CompensationRecord:
        if self.observation.source_id != self.source.id:
            raise ValueError("compensation record observation must reference its source")
        return self


def load_compensation_ledger(path: str | Path) -> CompensationLedger:
    data = _load_yaml_mapping(path, subject="compensation ledger")
    if set(data) != {"compensation_ledger"}:
        raise ConfigurationError("Compensation ledger root must contain only compensation_ledger")
    try:
        return CompensationLedger.model_validate(data["compensation_ledger"])
    except ValidationError as error:
        raise ConfigurationError(_validation_message("compensation ledger", error)) from error


def load_compensation_record(path: str | Path) -> CompensationRecord:
    data = _load_yaml_mapping(path, subject="compensation record")
    if set(data) != {"compensation_record"}:
        raise ConfigurationError("Compensation record root must contain only compensation_record")
    try:
        return CompensationRecord.model_validate(data["compensation_record"])
    except ValidationError as error:
        raise ConfigurationError(_validation_message("compensation record", error)) from error


def append_compensation_record(
    ledger: CompensationLedger, record: CompensationRecord
) -> CompensationLedger:
    """Return a fully validated ledger; callers persist it atomically after this succeeds."""
    existing = {source.id: source for source in ledger.sources}
    if source := existing.get(record.source.id):
        if source != record.source:
            raise ConfigurationError("compensation source ID already has different provenance")
        sources = ledger.sources
    else:
        sources = (*ledger.sources, record.source)
    try:
        return CompensationLedger(
            schema_version=ledger.schema_version,
            sources=sources,
            observations=(*ledger.observations, record.observation),
        )
    except ValidationError as error:
        raise ConfigurationError(_validation_message("compensation ledger", error)) from error


def new_compensation_ledger(record: CompensationRecord) -> CompensationLedger:
    """Create an initial ledger through the same validation path as an append."""
    try:
        return CompensationLedger(
            schema_version="compensation-ledger.v1",
            sources=(record.source,),
            observations=(record.observation,),
        )
    except ValidationError as error:
        raise ConfigurationError(_validation_message("compensation ledger", error)) from error


def dump_compensation_ledger(ledger: CompensationLedger) -> str:
    """Write canonical YAML only after the merged append-only ledger validates."""
    return yaml.safe_dump(
        {"compensation_ledger": ledger.model_dump(mode="json")},
        allow_unicode=True,
        sort_keys=False,
    )


def _subject_key(observation: CompensationObservation) -> tuple[SubjectKind, str]:
    """Labels and references can be corrected; subject kind/ID are the history identity."""
    return (observation.subject.kind, observation.subject.id)
