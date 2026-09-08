"""Golden and regression tests for pure deterministic job normalization."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pytest
import yaml
from yaml.constructor import ConstructorError

from career_os.domain.normalization import (
    JobNormalization,
    JobNormalizationInput,
    NormalizedCompensation,
    NormalizedField,
    NormalizedLocation,
    SourceCompensationEvidence,
    SourceLocationEvidence,
)
from career_os.normalization.jobs import JOB_NORMALIZER_VERSION, normalize_job


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys instead of overwriting them."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def test_versioned_golden_corpus_compares_complete_stable_output() -> None:
    corpus = _golden_corpus()
    assert corpus["normalizer_version"] == JOB_NORMALIZER_VERSION
    cases = _list(corpus["cases"], "cases")
    assert cases
    ids: set[str] = set()

    for raw_case in cases:
        case = _mapping(raw_case, "case")
        _exact_keys(case, {"id", "input", "expected"}, "case")
        case_id = _string(case["id"], "case.id")
        assert case_id not in ids
        ids.add(case_id)
        result = normalize_job(_input_from_case(_mapping(case["input"], f"{case_id}.input")))
        expected = _expected_output(_mapping(case["expected"], f"{case_id}.expected"), case_id)
        assert _serialize(result) == expected


def test_remote_scope_requires_local_remote_binding_and_honest_provenance() -> None:
    natural_orders = (
        ("Remote in EU.", "EU"),
        ("EU-only remote role.", "EU"),
        ("You may work remotely from Poland.", "Poland"),
    )
    for description, scope in natural_orders:
        result = normalize_job(JobNormalizationInput(title="Engineer", description=description))
        assert result.remote_scope.value == scope
        assert result.remote_scope.method == "description_rule"

    onsite = normalize_job(
        JobNormalizationInput(title="Engineer", description="Work from our office in Poland.")
    )
    ordinary_location = normalize_job(
        JobNormalizationInput(
            title="Engineer",
            description="Office-first role.",
            locations=(SourceLocationEvidence(raw="Warsaw, Poland"),),
        )
    )
    parsed_remote_location = normalize_job(
        JobNormalizationInput(
            title="Engineer",
            description="",
            locations=(SourceLocationEvidence(raw="Remote - Poland"),),
        )
    )
    structured_remote_location = normalize_job(
        JobNormalizationInput(
            title="Engineer",
            description="",
            locations=(SourceLocationEvidence(raw="Remote - Poland", country_code="PL"),),
        )
    )
    observed_location_variants = (
        ("Remote, Poland", None, "Poland"),
        ("Poland (Remote)", None, "Poland"),
        ("Poland", "Remote", "Poland"),
        ("Europe", "Remote", "Europe"),
        ("EU", "Remote", "EU"),
        ("Europe (Full Remote)", None, "Europe"),
    )

    assert onsite.remote_scope.value is None
    assert ordinary_location.remote_scope.value is None
    assert _scalar(parsed_remote_location.remote_scope) == {
        "value": "Poland",
        "method": "text_parse",
        "evidence": "Remote - Poland",
        "confidence": "0.700",
    }
    assert _scalar(structured_remote_location.remote_scope) == {
        "value": "Poland",
        "method": "structured_source",
        "evidence": "Remote - Poland",
        "confidence": "1.000",
    }
    for raw_location, workplace, expected_scope in observed_location_variants:
        result = normalize_job(
            JobNormalizationInput(
                title="Engineer",
                description="",
                locations=(SourceLocationEvidence(raw=raw_location),),
                source_workplace_type=workplace,
            )
        )
        assert result.remote_scope.value == expected_scope
        assert result.remote_scope.method == "text_parse"
        if "remote" in raw_location.casefold():
            assert result.workplace_type.value == "remote"
            assert result.workplace_type.method == "text_parse"

    localized_remote_outside_scope = normalize_job(
        JobNormalizationInput(
            title="Engineer",
            description="",
            locations=(SourceLocationEvidence(raw="Germany"),),
            source_workplace_type="Remote",
        )
    )
    assert localized_remote_outside_scope.remote_scope.value is None

    unbound_europe = normalize_job(
        JobNormalizationInput(
            title="Engineer",
            description="",
            locations=(SourceLocationEvidence(raw="Europe"),),
        )
    )
    hybrid_europe = normalize_job(
        JobNormalizationInput(
            title="Engineer",
            description="",
            locations=(SourceLocationEvidence(raw="Europe (Hybrid)"),),
        )
    )
    usa_remote = normalize_job(
        JobNormalizationInput(
            title="Engineer",
            description="",
            locations=(SourceLocationEvidence(raw="USA (Remote)"),),
        )
    )
    assert unbound_europe.remote_scope.value is None
    assert hybrid_europe.remote_scope.value is None
    assert usa_remote.remote_scope.value is None

    explicit_remote_location = normalize_job(
        JobNormalizationInput(
            title="Engineer",
            description="",
            locations=(SourceLocationEvidence(raw="Remote, Poland; Remote, United Kingdom"),),
        )
    )
    assert _scalar(explicit_remote_location.workplace_type) == {
        "value": "remote",
        "method": "text_parse",
        "evidence": "Remote",
        "confidence": "0.800",
    }


@pytest.mark.parametrize(
    "description",
    ("This is not remote in EU.", "No remote in Poland.", "Work without remote in Europe."),
)
def test_remote_scope_rejects_negated_remote_context(description: str) -> None:
    result = normalize_job(JobNormalizationInput(title="Engineer", description=description))
    assert result.remote_scope.value is None


def test_employment_requires_local_affirmative_job_context_and_no_conflict() -> None:
    full_time = normalize_job(
        JobNormalizationInput(title="Engineer", description="Full-time role.")
    )
    contract = normalize_job(
        JobNormalizationInput(title="Engineer", description="Contract engagement.")
    )
    contractor_reference = normalize_job(
        JobNormalizationInput(title="Engineer", description="Work with contractors across teams.")
    )
    negated = normalize_job(
        JobNormalizationInput(title="Engineer", description="This is not a contract role.")
    )
    mixed = normalize_job(
        JobNormalizationInput(title="Engineer", description="Full-time or contract role.")
    )

    assert full_time.employment_type.value == "full_time"
    assert contract.employment_type.value == "contract"
    assert contractor_reference.employment_type.value is None
    assert negated.employment_type.value is None
    assert mixed.employment_type.value is None


def test_workplace_honors_negative_or_conflicting_evidence_and_source_false() -> None:
    source_false = normalize_job(
        JobNormalizationInput(
            title="Engineer",
            description="This job is fully remote in EU.",
            source_is_remote=False,
        )
    )
    negated = normalize_job(
        JobNormalizationInput(title="Engineer", description="This is not remote and not hybrid.")
    )
    conflict = normalize_job(
        JobNormalizationInput(
            title="Engineer", description="Remote work with onsite attendance is required."
        )
    )
    affirmative_hybrid = normalize_job(
        JobNormalizationInput(
            title="Engineer", description="This is a hybrid role with remote and onsite days."
        )
    )

    assert source_false.workplace_type.value is None
    assert source_false.remote_scope.value is None
    assert negated.workplace_type.value is None
    assert conflict.workplace_type.value is None
    assert affirmative_hybrid.workplace_type.value == "hybrid"


def test_workplace_prose_requires_employment_context_and_structured_conflicts_fail_closed() -> None:
    technical_mentions = (
        "Build hybrid cloud infrastructure.",
        "Maintain remote systems and remote monitoring.",
        "Improve remote team collaboration.",
    )
    for description in technical_mentions:
        result = normalize_job(JobNormalizationInput(title="Engineer", description=description))
        assert result.workplace_type.value is None

    structured_remote_conflict = normalize_job(
        JobNormalizationInput(
            title="Engineer",
            description="Fully remote in EU.",
            source_workplace_type="Remote",
            source_is_remote=False,
        )
    )
    structured_onsite_conflict = normalize_job(
        JobNormalizationInput(
            title="Engineer",
            description="Remote work in EU.",
            source_workplace_type="Onsite",
            source_is_remote=True,
        )
    )

    assert structured_remote_conflict.workplace_type.value == "remote"
    assert structured_remote_conflict.remote_scope.value is None
    assert structured_onsite_conflict.workplace_type.value == "onsite"
    assert structured_onsite_conflict.remote_scope.value is None


def test_seniority_and_role_family_are_conservative() -> None:
    product_manager = normalize_job(JobNormalizationInput(title="Product Manager", description=""))
    senior_product_manager = normalize_job(
        JobNormalizationInput(title="Senior Product Manager", description="")
    )
    product_management_director = normalize_job(
        JobNormalizationInput(title="Director, Product Management - Core Platforms", description="")
    )
    mid_description = normalize_job(
        JobNormalizationInput(title="Engineer", description="A mid-level engineer role.")
    )
    mixed_title = normalize_job(
        JobNormalizationInput(
            title="Mid/Senior Backend Engineer",
            description="2+ years required. Middle-to-Senior growth in 6-12 months.",
        )
    )
    aspirational_description = normalize_job(
        JobNormalizationInput(
            title="Backend Software Engineer",
            description=(
                "Make architecture decisions with guidance from senior engineers. "
                "Grow from mid-level to senior engineer."
            ),
        )
    )
    explicit_fullstack = normalize_job(
        JobNormalizationInput(title="Full-stack Backend Engineer", description="")
    )
    contradictory = normalize_job(
        JobNormalizationInput(title="Backend and Frontend Engineer", description="")
    )
    backend_platform = normalize_job(
        JobNormalizationInput(title="Staff Backend Engineer, Events Platform", description="")
    )
    data_platform = normalize_job(
        JobNormalizationInput(title="Senior Data Engineer - Revenue Data Platform", description="")
    )
    product_platform = normalize_job(
        JobNormalizationInput(title="Product Manager, Cash Platform", description="")
    )
    plural_platform = normalize_job(
        JobNormalizationInput(title="Software Engineer - Network Platforms", description="")
    )
    non_engineering_platforms = normalize_job(
        JobNormalizationInput(title="Account Executive, Enterprise Platforms", description="")
    )
    ordinary = normalize_job(JobNormalizationInput(title="Engineer", description=""))

    assert (product_manager.role_family.value, product_manager.seniority.value) == (
        "product_management",
        None,
    )
    assert senior_product_manager.seniority.value == "senior"
    assert product_management_director.role_family.value == "product_management"
    assert mid_description.seniority.value == "mid"
    assert mixed_title.seniority.value is None
    assert aspirational_description.seniority.value is None
    assert explicit_fullstack.role_family.value == "fullstack_engineering"
    assert contradictory.role_family.value is None
    assert backend_platform.role_family.value == "backend_engineering"
    assert data_platform.role_family.value == "data_engineering"
    assert product_platform.role_family.value == "product_management"
    assert plural_platform.role_family.value == "platform_engineering"
    assert non_engineering_platforms.role_family.value is None
    assert ordinary.role_family.value is None


def test_platform_and_infrastructure_roles_require_engineering_context() -> None:
    positive_titles = (
        "Staff Software Engineer, Infrastructure (Cloud)",
        "Senior Infrastructure Engineer",
        "Platform Developer",
        "Software Engineer - Network Platforms",
    )
    for title in positive_titles:
        result = normalize_job(JobNormalizationInput(title=title, description=""))
        assert result.role_family.value == "platform_engineering"

    guarded_titles = {
        "Treasury Partner Lead - Platform": None,
        "IT Service Desk Lead - Platform": None,
        "Staff Infrastructure Security Engineer": None,
        "Senior Product Manager, Core Infrastructure": "product_management",
        "Senior DevOps Engineer (Infrastructure)": "devops_engineering",
        "Staff Software Engineer, Mobile (App Infrastructure)": "platform_engineering",
    }
    for title, expected_role in guarded_titles.items():
        result = normalize_job(JobNormalizationInput(title=title, description=""))
        assert result.role_family.value == expected_role


def test_location_parsing_preserves_order_and_does_not_turn_country_into_city() -> None:
    result = normalize_job(
        JobNormalizationInput(
            title="Engineer",
            description="",
            locations=(
                SourceLocationEvidence(raw="Warsaw/PL", is_primary=True),
                SourceLocationEvidence(raw="Poland"),
                SourceLocationEvidence(raw="Bielsko-Biała - Poland"),
                SourceLocationEvidence(raw="Distributed"),
            ),
        )
    )

    assert [_location(location) for location in result.locations] == [
        {
            "ordinal": 0,
            "raw": "Warsaw/PL",
            "city": "Warsaw",
            "region": None,
            "country_code": "PL",
            "is_primary": True,
            "confidence": "0.800",
            "method": "text_parse",
        },
        {
            "ordinal": 1,
            "raw": "Poland",
            "city": None,
            "region": None,
            "country_code": "PL",
            "is_primary": False,
            "confidence": "0.800",
            "method": "text_parse",
        },
        {
            "ordinal": 2,
            "raw": "Bielsko-Biała - Poland",
            "city": "Bielsko-Biała",
            "region": None,
            "country_code": "PL",
            "is_primary": False,
            "confidence": "0.800",
            "method": "text_parse",
        },
        {
            "ordinal": 3,
            "raw": "Distributed",
            "city": None,
            "region": None,
            "country_code": None,
            "is_primary": False,
            "confidence": "0.400",
            "method": "text_parse",
        },
    ]


def test_text_compensation_uses_occurrence_local_component_interval_and_evidence() -> None:
    result = normalize_job(
        JobNormalizationInput(
            title="Engineer",
            description="",
            compensation_text=(
                "Base salary USD 100,000 - 120,000 annually; "
                "bonus USD 20,000 - 30,000 hourly. "
                "Salary EUR 4,000 - 5,000 monthly. USD 50 - 60."
            ),
        )
    )

    assert [_compensation(item) for item in result.compensation] == [
        {
            "component_type": "base_salary",
            "min_value": "100000",
            "max_value": "120000",
            "currency": "USD",
            "interval": "year",
            "location_tier": None,
            "raw_text": "Base salary USD 100,000 - 120,000 annually",
            "source_method": "text_parse",
            "confidence": "0.700",
        },
        {
            "component_type": "bonus",
            "min_value": "20000",
            "max_value": "30000",
            "currency": "USD",
            "interval": "hour",
            "location_tier": None,
            "raw_text": "bonus USD 20,000 - 30,000 hourly",
            "source_method": "text_parse",
            "confidence": "0.700",
        },
        {
            "component_type": "base_salary",
            "min_value": "4000",
            "max_value": "5000",
            "currency": "EUR",
            "interval": "month",
            "location_tier": None,
            "raw_text": "Salary EUR 4,000 - 5,000 monthly",
            "source_method": "text_parse",
            "confidence": "0.700",
        },
    ]


def test_text_compensation_rejects_ambiguous_or_unscoped_ranges() -> None:
    ambiguous = normalize_job(
        JobNormalizationInput(title="Engineer", description="", compensation_text="$100k-$120k")
    )
    unscoped = normalize_job(
        JobNormalizationInput(title="Engineer", description="", compensation_text="USD 100k-120k")
    )
    explicit = normalize_job(
        JobNormalizationInput(
            title="Engineer", description="", compensation_text="Salary USD 100k-120k"
        )
    )

    assert ambiguous.compensation == ()
    assert unscoped.compensation == ()
    assert [_compensation(item) for item in explicit.compensation] == [
        {
            "component_type": "base_salary",
            "min_value": "100000",
            "max_value": "120000",
            "currency": "USD",
            "interval": None,
            "location_tier": None,
            "raw_text": "Salary USD 100k-120k",
            "source_method": "text_parse",
            "confidence": "0.700",
        }
    ]


def test_text_compensation_accepts_suffix_currency_and_rejects_conflicts() -> None:
    affirm = normalize_job(
        JobNormalizationInput(
            title="Senior Backend Engineer",
            description="",
            compensation_text=(
                "Base Pay Grade - N 308,000 zł - 428,000 zł. Equity USD 32,000 - 48,000."
            ),
        )
    )
    conflict = normalize_job(
        JobNormalizationInput(
            title="Engineer",
            description="",
            compensation_text="Salary 100,000 PLN - 120,000 EUR per year.",
        )
    )

    assert [_compensation(item) for item in affirm.compensation] == [
        {
            "component_type": "base_salary",
            "min_value": "308000",
            "max_value": "428000",
            "currency": "PLN",
            "interval": None,
            "location_tier": None,
            "raw_text": "Base Pay Grade - N 308,000 zł - 428,000 zł",
            "source_method": "text_parse",
            "confidence": "0.700",
        },
        {
            "component_type": "equity",
            "min_value": "32000",
            "max_value": "48000",
            "currency": "USD",
            "interval": None,
            "location_tier": None,
            "raw_text": "Equity USD 32,000 - 48,000",
            "source_method": "text_parse",
            "confidence": "0.700",
        },
    ]
    assert conflict.compensation == ()


def test_ashby_structured_locations_and_tiered_components_are_preserved() -> None:
    payload = _mapping(_json_fixture("ashby/populated.json")["jobs"][0], "ashby.job")
    secondary = _mapping(_list(payload["secondaryLocations"], "secondaryLocations")[0], "secondary")
    address = _mapping(secondary["address"], "secondary.address")
    compensation = _mapping(payload["compensation"], "compensation")
    tier = _mapping(_list(compensation["compensationTiers"], "tiers")[0], "tier")
    components = _list(tier["components"], "components")

    result = normalize_job(
        JobNormalizationInput(
            title=_string(payload["title"], "title"),
            description=_string(payload["descriptionPlain"], "descriptionPlain"),
            locations=(
                SourceLocationEvidence(
                    raw=_string(payload["location"], "location"), is_primary=True
                ),
                SourceLocationEvidence(
                    raw=_string(secondary["location"], "secondary.location"),
                    city=_string(address["addressLocality"], "addressLocality"),
                    region=_string(address["addressRegion"], "addressRegion"),
                    country_code=_string(address["addressCountry"], "addressCountry"),
                ),
            ),
            source_employment_type=_string(payload["employmentType"], "employmentType"),
            source_workplace_type=_string(payload["workplaceType"], "workplaceType"),
            source_is_remote=_bool(payload["isRemote"], "isRemote"),
            structured_compensation=tuple(
                SourceCompensationEvidence(
                    component_type=_string(component["compensationType"], "compensationType"),
                    min_value=_fixture_decimal(component["minValue"], "minValue"),
                    max_value=_fixture_decimal(component["maxValue"], "maxValue"),
                    currency=_optional_string(component["currencyCode"], "currencyCode"),
                    interval=_string(component["interval"], "interval"),
                    location_tier=_string(tier["tierSummary"], "tierSummary"),
                    raw_text=_string(component["summary"], "summary"),
                )
                for raw_component in components
                for component in (_mapping(raw_component, "component"),)
            ),
        )
    )

    assert [_location(item) for item in result.locations] == [
        {
            "ordinal": 0,
            "raw": "Warsaw, Poland",
            "city": "Warsaw",
            "region": None,
            "country_code": "PL",
            "is_primary": True,
            "confidence": "0.800",
            "method": "text_parse",
        },
        {
            "ordinal": 1,
            "raw": "Remote - Poland",
            "city": "Warsaw",
            "region": "Mazowieckie",
            "country_code": "PL",
            "is_primary": False,
            "confidence": "1.000",
            "method": "structured_source",
        },
    ]
    assert _scalar(result.remote_scope) == {
        "value": "Poland",
        "method": "structured_source",
        "evidence": "Remote - Poland",
        "confidence": "1.000",
    }
    assert [(item.component_type, item.location_tier) for item in result.compensation] == [
        ("base_salary", "Senior Data Engineer"),
        ("equity", "Senior Data Engineer"),
    ]


def test_greenhouse_location_is_parsed_without_creating_remote_scope() -> None:
    payload = _mapping(_json_fixture("greenhouse/populated.json")["jobs"][0], "greenhouse.job")
    location = _mapping(payload["location"], "greenhouse.location")
    result = normalize_job(
        JobNormalizationInput(
            title=_string(payload["title"], "title"),
            description="Office-first collaboration.",
            locations=(SourceLocationEvidence(raw=_string(location["name"], "location.name")),),
        )
    )

    assert _location(result.locations[0]) == {
        "ordinal": 0,
        "raw": "Warsaw, Poland",
        "city": "Warsaw",
        "region": None,
        "country_code": "PL",
        "is_primary": False,
        "confidence": "0.800",
        "method": "text_parse",
    }
    assert result.remote_scope.value is None


@pytest.mark.parametrize(
    "factory",
    [
        lambda: SourceLocationEvidence(raw="Warsaw", country_code="pl"),
        lambda: SourceCompensationEvidence(component_type="salary"),
        lambda: SourceCompensationEvidence(
            component_type="salary", min_value=Decimal("2"), max_value=Decimal("1")
        ),
        lambda: SourceCompensationEvidence(component_type="salary", min_value=Decimal("NaN")),
        lambda: NormalizedCompensation(
            "base_salary", None, None, None, None, None, None, "text_parse", Decimal("0.7")
        ),
        lambda: NormalizedCompensation(
            "base_salary",
            Decimal("Infinity"),
            None,
            "USD",
            None,
            None,
            "USD Infinity",
            "structured_source",
            Decimal("0.7"),
        ),
        lambda: NormalizedField("senior", "title_rule", "Senior", Decimal("NaN")),
        lambda: NormalizedLocation(
            True, "Warsaw", None, None, None, False, Decimal("0.4"), "text_parse"
        ),
        lambda: NormalizedLocation(
            0, "Warsaw", None, None, None, False, Decimal("-Infinity"), "text_parse"
        ),
        lambda: JobNormalization(
            JOB_NORMALIZER_VERSION,
            "Engineer",
            NormalizedField("uncontrolled", "title_rule", "Engineer", Decimal("0.9")),
            NormalizedField(None),
            (),
            NormalizedField(None),
            NormalizedField(None),
            NormalizedField(None),
            (),
        ),
    ],
)
def test_frozen_contracts_fail_closed(factory: object) -> None:
    assert callable(factory)
    with pytest.raises((TypeError, ValueError)):
        factory()


def _golden_corpus() -> dict[str, object]:
    loaded = yaml.load(
        _fixture_path("normalization/golden_normalization.v1.yaml").read_text(),
        Loader=_UniqueKeyLoader,
    )
    corpus = _mapping(loaded, "golden corpus")
    _exact_keys(corpus, {"normalizer_version", "cases"}, "golden corpus")
    _string(corpus["normalizer_version"], "normalizer_version")
    return corpus


def _input_from_case(raw: dict[object, object]) -> JobNormalizationInput:
    allowed = {
        "title",
        "description",
        "source_level",
        "source_category",
        "source_employment_type",
        "source_workplace_type",
        "source_is_remote",
        "locations",
        "structured_compensation",
        "compensation_text",
    }
    _required_keys(raw, {"title", "description"}, "input")
    _only_keys(raw, allowed, "input")
    locations = tuple(
        _source_location(_mapping(item, "input.locations item"))
        for item in _list(raw.get("locations", []), "input.locations")
    )
    compensation = tuple(
        _source_compensation(_mapping(item, "input.structured_compensation item"))
        for item in _list(raw.get("structured_compensation", []), "input.structured_compensation")
    )
    source_remote = raw.get("source_is_remote")
    if source_remote is not None:
        source_remote = _bool(source_remote, "input.source_is_remote")
    return JobNormalizationInput(
        title=_string(raw["title"], "input.title"),
        description=_string(raw["description"], "input.description"),
        source_level=_optional_string(raw.get("source_level"), "input.source_level"),
        source_category=_optional_string(raw.get("source_category"), "input.source_category"),
        source_employment_type=_optional_string(
            raw.get("source_employment_type"), "input.source_employment_type"
        ),
        source_workplace_type=_optional_string(
            raw.get("source_workplace_type"), "input.source_workplace_type"
        ),
        source_is_remote=source_remote,
        locations=locations,
        structured_compensation=compensation,
        compensation_text=_optional_string(raw.get("compensation_text"), "input.compensation_text"),
    )


def _source_location(raw: dict[object, object]) -> SourceLocationEvidence:
    _required_keys(raw, {"raw"}, "input.locations")
    _only_keys(raw, {"raw", "city", "region", "country_code", "is_primary"}, "input.locations")
    return SourceLocationEvidence(
        raw=_string(raw["raw"], "input.locations.raw"),
        city=_optional_string(raw.get("city"), "input.locations.city"),
        region=_optional_string(raw.get("region"), "input.locations.region"),
        country_code=_optional_string(raw.get("country_code"), "input.locations.country_code"),
        is_primary=_bool(raw.get("is_primary", False), "input.locations.is_primary"),
    )


def _source_compensation(raw: dict[object, object]) -> SourceCompensationEvidence:
    _required_keys(raw, {"component_type"}, "input.structured_compensation")
    _only_keys(
        raw,
        {
            "component_type",
            "min_value",
            "max_value",
            "currency",
            "interval",
            "location_tier",
            "raw_text",
        },
        "input.structured_compensation",
    )
    return SourceCompensationEvidence(
        component_type=_string(
            raw["component_type"], "input.structured_compensation.component_type"
        ),
        min_value=_optional_decimal(
            raw.get("min_value"), "input.structured_compensation.min_value"
        ),
        max_value=_optional_decimal(
            raw.get("max_value"), "input.structured_compensation.max_value"
        ),
        currency=_optional_string(raw.get("currency"), "input.structured_compensation.currency"),
        interval=_optional_string(raw.get("interval"), "input.structured_compensation.interval"),
        location_tier=_optional_string(
            raw.get("location_tier"), "input.structured_compensation.location_tier"
        ),
        raw_text=_optional_string(raw.get("raw_text"), "input.structured_compensation.raw_text"),
    )


def _expected_output(raw: dict[object, object], case_id: str) -> dict[str, object]:
    _exact_keys(
        raw,
        {
            "normalizer_version",
            "title_normalized",
            "role_family",
            "seniority",
            "locations",
            "employment_type",
            "workplace_type",
            "remote_scope",
            "compensation",
        },
        f"{case_id}.expected",
    )
    return {
        "normalizer_version": _string(raw["normalizer_version"], "expected.normalizer_version"),
        "title_normalized": _string(raw["title_normalized"], "expected.title_normalized"),
        "role_family": _expected_scalar(raw["role_family"], "expected.role_family"),
        "seniority": _expected_scalar(raw["seniority"], "expected.seniority"),
        "employment_type": _expected_scalar(raw["employment_type"], "expected.employment_type"),
        "workplace_type": _expected_scalar(raw["workplace_type"], "expected.workplace_type"),
        "remote_scope": _expected_scalar(raw["remote_scope"], "expected.remote_scope"),
        "locations": [
            _expected_location(item) for item in _list(raw["locations"], "expected.locations")
        ],
        "compensation": [
            _expected_compensation(item)
            for item in _list(raw["compensation"], "expected.compensation")
        ],
    }


def _expected_scalar(raw: object, name: str) -> dict[str, object]:
    value = _mapping(raw, name)
    _exact_keys(value, {"value", "method", "evidence", "confidence"}, name)
    return {
        "value": _optional_string(value["value"], f"{name}.value"),
        "method": _optional_string(value["method"], f"{name}.method"),
        "evidence": _optional_string(value["evidence"], f"{name}.evidence"),
        "confidence": _optional_decimal_string(value["confidence"], f"{name}.confidence"),
    }


def _expected_location(raw: object) -> dict[str, object]:
    value = _mapping(raw, "expected.locations item")
    _exact_keys(
        value,
        {"ordinal", "raw", "city", "region", "country_code", "is_primary", "confidence", "method"},
        "expected.locations item",
    )
    ordinal = value["ordinal"]
    if type(ordinal) is not int:
        raise AssertionError("expected.locations.ordinal must be an integer")
    return {
        "ordinal": ordinal,
        "raw": _string(value["raw"], "expected.locations.raw"),
        "city": _optional_string(value["city"], "expected.locations.city"),
        "region": _optional_string(value["region"], "expected.locations.region"),
        "country_code": _optional_string(value["country_code"], "expected.locations.country_code"),
        "is_primary": _bool(value["is_primary"], "expected.locations.is_primary"),
        "confidence": _decimal_string(value["confidence"], "expected.locations.confidence"),
        "method": _string(value["method"], "expected.locations.method"),
    }


def _expected_compensation(raw: object) -> dict[str, object]:
    value = _mapping(raw, "expected.compensation item")
    _exact_keys(
        value,
        {
            "component_type",
            "min_value",
            "max_value",
            "currency",
            "interval",
            "location_tier",
            "raw_text",
            "source_method",
            "confidence",
        },
        "expected.compensation item",
    )
    return {
        "component_type": _string(value["component_type"], "expected.compensation.component_type"),
        "min_value": _optional_decimal_string(
            value["min_value"], "expected.compensation.min_value"
        ),
        "max_value": _optional_decimal_string(
            value["max_value"], "expected.compensation.max_value"
        ),
        "currency": _optional_string(value["currency"], "expected.compensation.currency"),
        "interval": _optional_string(value["interval"], "expected.compensation.interval"),
        "location_tier": _optional_string(
            value["location_tier"], "expected.compensation.location_tier"
        ),
        "raw_text": _optional_string(value["raw_text"], "expected.compensation.raw_text"),
        "source_method": _string(value["source_method"], "expected.compensation.source_method"),
        "confidence": _decimal_string(value["confidence"], "expected.compensation.confidence"),
    }


def _serialize(result: JobNormalization) -> dict[str, object]:
    return {
        "normalizer_version": result.normalizer_version,
        "title_normalized": result.title_normalized,
        "role_family": _scalar(result.role_family),
        "seniority": _scalar(result.seniority),
        "employment_type": _scalar(result.employment_type),
        "workplace_type": _scalar(result.workplace_type),
        "remote_scope": _scalar(result.remote_scope),
        "locations": [_location(item) for item in result.locations],
        "compensation": [_compensation(item) for item in result.compensation],
    }


def _scalar(value: NormalizedField) -> dict[str, object]:
    return {
        "value": value.value,
        "method": value.method,
        "evidence": value.evidence,
        "confidence": str(value.confidence) if value.confidence is not None else None,
    }


def _location(value: NormalizedLocation) -> dict[str, object]:
    return {
        "ordinal": value.ordinal,
        "raw": value.raw,
        "city": value.city,
        "region": value.region,
        "country_code": value.country_code,
        "is_primary": value.is_primary,
        "confidence": str(value.confidence),
        "method": value.method,
    }


def _compensation(value: NormalizedCompensation) -> dict[str, object]:
    return {
        "component_type": value.component_type,
        "min_value": str(value.min_value) if value.min_value is not None else None,
        "max_value": str(value.max_value) if value.max_value is not None else None,
        "currency": value.currency,
        "interval": value.interval,
        "location_tier": value.location_tier,
        "raw_text": value.raw_text,
        "source_method": value.source_method,
        "confidence": str(value.confidence),
    }


def _mapping(value: object, name: str) -> dict[object, object]:
    if not isinstance(value, dict):
        raise AssertionError(f"{name} must be a mapping")
    return value


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise AssertionError(f"{name} must be a list")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise AssertionError(f"{name} must be a non-empty string")
    return value


def _optional_string(value: object, name: str) -> str | None:
    return None if value is None else _string(value, name)


def _bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise AssertionError(f"{name} must be a bool")
    return value


def _decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, str):
        raise AssertionError(f"{name} must be a decimal string")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise AssertionError(f"{name} must be a decimal string") from exc


def _optional_decimal(value: object, name: str) -> Decimal | None:
    return None if value is None else _decimal(value, name)


def _fixture_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise AssertionError(f"{name} must be a JSON number or decimal string")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise AssertionError(f"{name} must be a JSON number or decimal string") from exc


def _decimal_string(value: object, name: str) -> str:
    return str(_decimal(value, name))


def _optional_decimal_string(value: object, name: str) -> str | None:
    return None if value is None else _decimal_string(value, name)


def _required_keys(value: dict[object, object], keys: set[str], name: str) -> None:
    missing = keys - value.keys()
    if missing:
        raise AssertionError(f"{name} is missing keys: {sorted(missing)}")


def _only_keys(value: dict[object, object], allowed: set[str], name: str) -> None:
    unknown = value.keys() - allowed
    if unknown:
        raise AssertionError(f"{name} has unsupported keys: {sorted(unknown)}")


def _exact_keys(value: dict[object, object], keys: set[str], name: str) -> None:
    _required_keys(value, keys, name)
    _only_keys(value, keys, name)


def _json_fixture(name: str) -> dict[str, Any]:
    loaded: object = json.loads(_fixture_path(f"ats/{name}").read_text())
    return _mapping(loaded, name)


def _fixture_path(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures" / name


@pytest.mark.parametrize(
    "title", ["Tech Lead Manager, Backend", "Technical Lead Manager, Full-stack"]
)
def test_tech_lead_manager_is_management_not_ic(title: str) -> None:
    result = normalize_job(JobNormalizationInput(title=title, description=""))
    assert result.role_family.value == "engineering_management"
    assert result.seniority.value == "manager"
    ic = normalize_job(JobNormalizationInput(title="Tech Lead, Backend", description=""))
    assert ic.role_family.value == "backend_engineering"
    assert ic.seniority.value == "lead"


@pytest.mark.parametrize(
    ("duty", "family"),
    [
        (
            "Lead the design and development of high-impact backend systems and APIs.",
            "backend_engineering",
        ),
        (
            "Help build and evolve mission-critical horizontal infrastructure and tooling.",
            "platform_engineering",
        ),
    ],
)
def test_generic_software_role_uses_direct_duties_with_evidence(duty: str, family: str) -> None:
    result = normalize_job(
        JobNormalizationInput(
            title="Staff Software Engineer, Example",
            description=f"You will:\n\n• {duty}\n\nYou have:\n• 6 years experience.",
        )
    )
    assert result.role_family.value == family
    assert result.role_family.method == "description_rule"
    assert result.role_family.evidence == duty
    assert result.role_family.confidence == Decimal("0.700")
    assert result.seniority.value == "staff"


@pytest.mark.parametrize(
    "description",
    [
        "We build backend systems and infrastructure.",
        "You have:\n• 6+ years of backend or infrastructure experience",
        "We prefer:\n• Lead the design and development of high-impact backend systems and APIs.",
        "You will:\n• Collaborate with engineers who build infrastructure.",
        "You will:\n• Do not lead the design and development of backend systems.",
        "You will:\n• Work with teams.\nYou have:\n"
        "• Lead the design and development of backend systems.",
        "You will:\n• Lead the design and development of backend systems.\n"
        "• Help build and evolve horizontal infrastructure and tooling.",
    ],
)
def test_role_description_fallback_rejects_context_preferences_negation_and_conflicts(
    description: str,
) -> None:
    result = normalize_job(
        JobNormalizationInput(title="Staff Software Engineer", description=description)
    )
    assert result.role_family.value is None


@pytest.mark.parametrize(
    ("title", "family"),
    [
        ("Senior Android Engineer", None),
        ("Software Engineer, Android", None),
        ("Account Executive", None),
        ("Backend and Frontend Engineer", None),
        ("Frontend Software Engineer", "frontend_engineering"),
        ("Software Engineer, Full Stack", "fullstack_engineering"),
    ],
)
def test_description_does_not_override_specialist_or_conflicting_titles(
    title: str, family: str | None
) -> None:
    result = normalize_job(
        JobNormalizationInput(
            title=title,
            description="You will:\n• Lead the design and development of backend systems.",
        )
    )
    assert result.role_family.value == family
