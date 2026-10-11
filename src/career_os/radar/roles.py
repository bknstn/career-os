"""Versioned, deterministic role classification for Career Radar.

The module is independent of legacy normalization and persistence. Results are
immutable and carry classifier/taxonomy versions plus exact evidence snippets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

_SEMVER: Final = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class RoleFamily(StrEnum):
    BACKEND_ENGINEERING = "backend_engineering"
    FRONTEND_ENGINEERING = "frontend_engineering"
    FULLSTACK_ENGINEERING = "fullstack_engineering"
    DATA_ENGINEERING = "data_engineering"
    PRODUCT_ENGINEERING = "product_engineering"
    PLATFORM_INFRASTRUCTURE_ENGINEERING = "platform_infrastructure_engineering"
    MACHINE_LEARNING_ENGINEERING = "machine_learning_engineering"
    AI_ENGINEERING = "ai_engineering"
    AI_PRODUCT_ENGINEERING = "ai_product_engineering"
    SOLUTIONS_ARCHITECTURE = "solutions_architecture"
    ENGINEERING_MANAGEMENT = "engineering_management"


class RoleScope(StrEnum):
    IN_SCOPE = "in_scope"
    OUT_OF_SCOPE = "out_of_scope"
    UNKNOWN_SCOPE = "unknown_scope"


class ClassificationStatus(StrEnum):
    CLASSIFIED = "classified"
    UNCLASSIFIED = "unclassified"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflict"
    UNKNOWN_SCOPE = "unknown_scope"
    OUT_OF_SCOPE = "out_of_scope"


class Seniority(StrEnum):
    INTERN = "intern"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    STAFF = "staff"
    PRINCIPAL = "principal"
    MANAGER = "manager"
    DIRECTOR = "director"
    EXECUTIVE = "executive"


class RoleContext(StrEnum):
    AI = "ai"
    MACHINE_LEARNING = "machine_learning"
    PRODUCT = "product"
    BACKEND = "backend"
    FRONTEND = "frontend"
    FULLSTACK = "fullstack"
    DATA = "data"
    PLATFORM = "platform"
    CLOUD = "cloud"
    SECURITY = "security"
    CUSTOMER_FACING = "customer_facing"


class EvidenceAxis(StrEnum):
    SCOPE = "scope"
    ROLE = "role"
    SENIORITY = "seniority"
    MANAGERIAL_FUNCTION = "managerial_function"
    CONTEXT = "context"


class EvidenceSource(StrEnum):
    TITLE = "title"
    DESCRIPTION_DUTY = "description_duty"


@dataclass(frozen=True, slots=True)
class RoleFamilyDefinition:
    id: RoleFamily
    label: str


@dataclass(frozen=True, slots=True)
class RoleTaxonomy:
    version: str
    families: tuple[RoleFamilyDefinition, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.families, tuple):
            raise ValueError("taxonomy families must be an immutable tuple")
        validate_role_taxonomy(self)


@dataclass(frozen=True, slots=True)
class ClassificationEvidence:
    axis: EvidenceAxis
    source: EvidenceSource
    rule_id: str
    matched_text: str

    def __post_init__(self) -> None:
        if not self.rule_id.strip() or not self.matched_text.strip():
            raise ValueError("classification evidence fields must be non-blank")


@dataclass(frozen=True, slots=True)
class RoleClassification:
    classifier_version: str
    taxonomy_version: str
    role_family: RoleFamily | None
    status: ClassificationStatus
    scope: RoleScope
    seniority: Seniority | None
    managerial_function: bool | None
    context: tuple[RoleContext, ...]
    evidence: tuple[ClassificationEvidence, ...]

    def __post_init__(self) -> None:
        if not _SEMVER.fullmatch(self.classifier_version) or not _SEMVER.fullmatch(
            self.taxonomy_version
        ):
            raise ValueError("classifier and taxonomy versions must be semantic versions")
        if not isinstance(self.context, tuple) or not isinstance(self.evidence, tuple):
            raise ValueError("context and evidence must be immutable tuples")
        if not all(isinstance(value, RoleContext) for value in self.context):
            raise ValueError("context contains an unsupported role context")
        if len(self.context) != len(set(self.context)):
            raise ValueError("context values must be unique")
        if not all(isinstance(item, ClassificationEvidence) for item in self.evidence):
            raise ValueError("evidence must contain ClassificationEvidence values")
        if self.managerial_function not in {True, False, None}:
            raise ValueError("managerial_function must be true, false, or unknown")
        expected_scope = {
            ClassificationStatus.CLASSIFIED: RoleScope.IN_SCOPE,
            ClassificationStatus.UNCLASSIFIED: RoleScope.IN_SCOPE,
            ClassificationStatus.AMBIGUOUS: RoleScope.IN_SCOPE,
            ClassificationStatus.CONFLICT: RoleScope.IN_SCOPE,
            ClassificationStatus.UNKNOWN_SCOPE: RoleScope.UNKNOWN_SCOPE,
            ClassificationStatus.OUT_OF_SCOPE: RoleScope.OUT_OF_SCOPE,
        }[self.status]
        if self.scope is not expected_scope:
            raise ValueError("classification status and scope do not agree")
        if self.status is ClassificationStatus.CLASSIFIED:
            if self.role_family is None:
                raise ValueError("classified results require one role family")
        elif self.role_family is not None:
            raise ValueError("unclassified results must not carry a role family")

    @property
    def role_bucket(self) -> str:
        """Return the R1 denominator bucket without making it a role family."""
        return self.role_family.value if self.role_family is not None else "unclassified"


FAMILIES: Final[tuple[RoleFamilyDefinition, ...]] = (
    RoleFamilyDefinition(RoleFamily.BACKEND_ENGINEERING, "Backend engineering"),
    RoleFamilyDefinition(RoleFamily.FRONTEND_ENGINEERING, "Frontend engineering"),
    RoleFamilyDefinition(RoleFamily.FULLSTACK_ENGINEERING, "Full-stack engineering"),
    RoleFamilyDefinition(RoleFamily.DATA_ENGINEERING, "Data engineering"),
    RoleFamilyDefinition(RoleFamily.PRODUCT_ENGINEERING, "Product engineering"),
    RoleFamilyDefinition(
        RoleFamily.PLATFORM_INFRASTRUCTURE_ENGINEERING,
        "Platform and infrastructure engineering",
    ),
    RoleFamilyDefinition(RoleFamily.MACHINE_LEARNING_ENGINEERING, "Machine-learning engineering"),
    RoleFamilyDefinition(RoleFamily.AI_ENGINEERING, "AI engineering"),
    RoleFamilyDefinition(RoleFamily.AI_PRODUCT_ENGINEERING, "AI product engineering"),
    RoleFamilyDefinition(RoleFamily.SOLUTIONS_ARCHITECTURE, "Solutions architecture"),
    RoleFamilyDefinition(RoleFamily.ENGINEERING_MANAGEMENT, "Engineering management"),
)


def validate_role_taxonomy(taxonomy: RoleTaxonomy) -> None:
    """Reject taxonomy edits that would make persisted family IDs unstable."""
    if not _SEMVER.fullmatch(taxonomy.version):
        raise ValueError("taxonomy version must be MAJOR.MINOR.PATCH")
    ids = tuple(family.id for family in taxonomy.families)
    if len(ids) != len(set(ids)):
        raise ValueError("taxonomy family IDs must be unique")
    if set(ids) != set(RoleFamily):
        raise ValueError("taxonomy must define exactly the eleven R1 role families")
    if any(not family.label.strip() for family in taxonomy.families):
        raise ValueError("taxonomy family labels must be non-blank")


ROLE_TAXONOMY: Final = RoleTaxonomy(version="1.0.0", families=FAMILIES)
CLASSIFIER_VERSION: Final = "1.0.0"

_TECHNICAL_ROLE_HEAD: Final = re.compile(
    r"\b(?:software\s+)?(?:engineer|developer|programmer|architect)\b|"
    r"\b(?:technical|engineering)\s+support\b|\btech\s+lead\b|"
    r"\b(?:data|research|applied)\s+scientist\b|\b(?:devops|sre)\b",
    re.I,
)
_TECHNICAL_TITLE: Final = re.compile(
    r"\b(?:engineer(?:ing)?|developer|software|programmer|architect|scientist|"
    r"technical|devops|sre)\b|\btech\s+lead\b",
    re.I,
)
_OUT_OF_SCOPE_HEAD: Final = re.compile(
    r"^\s*(?:product\s+manager|project\s+manager|sales\s+manager|"
    r"account\s+(?:executive|manager)|recruit(?:er|ing)|talent\s+acquisition|"
    r"marketing\s+(?:manager|analyst|director|specialist)|"
    r"finance\s+(?:manager|analyst|director)|accounting\s+(?:manager|specialist)|"
    r"(?:legal\s+counsel|attorney)|(?:product|ux)\s+designer|customer\s+success)\b",
    re.I,
)
_ENGINEERING_MANAGEMENT: Final = re.compile(
    r"\b(?:engineering|software|data|platform|infrastructure)\s+manager\b|"
    r"\bmanager\s+(?:of\s+)?(?:engineering|software|data|platform)\b|"
    r"\b(?:director|head|vp|vice\s+president|chief)\s+(?:of\s+)?engineering\b|"
    r"\bengineering\s+(?:director|head|vp|vice\s+president)\b",
    re.I,
)
_AI_PRODUCT: Final = re.compile(
    r"\b(?:ai|artificial\s+intelligence)\s+product\s+(?:software\s+)?engineer\b",
    re.I,
)
_PRODUCT_BACKEND_SPECIALTY: Final = re.compile(
    r"\bproduct\s+(?:software\s+)?engineer\s*\(\s*(?:back\s*end|backend)\s*\)",
    re.I,
)
_GENERIC_ENGINEERING_HEAD: Final = re.compile(
    r"^\s*(?:(?:staff|principal|senior|sr\.?|lead|junior|jr\.?|mid)\s+)?"
    r"(?:software\s+)?(?:engineer|developer)\b",
    re.I,
)

_ROLE_PATTERNS: Final[dict[RoleFamily, tuple[tuple[str, re.Pattern[str]], ...]]] = {
    RoleFamily.BACKEND_ENGINEERING: (
        ("backend_engineer", re.compile(r"\bback\s*end\s+(?:software\s+)?engineer\b", re.I)),
        ("backend_developer", re.compile(r"\bback\s*end\s+developer\b", re.I)),
    ),
    RoleFamily.FRONTEND_ENGINEERING: (
        ("frontend_engineer", re.compile(r"\bfront\s*end\s+(?:software\s+)?engineer\b", re.I)),
        ("frontend_developer", re.compile(r"\bfront\s*end\s+developer\b", re.I)),
    ),
    RoleFamily.FULLSTACK_ENGINEERING: (
        ("fullstack_engineer", re.compile(r"\bfull\s*stack\s+(?:software\s+)?engineer\b", re.I)),
        ("fullstack_developer", re.compile(r"\bfull\s*stack\s+developer\b", re.I)),
    ),
    RoleFamily.DATA_ENGINEERING: (
        ("data_engineer", re.compile(r"\bdata\s+(?:software\s+)?engineer\b", re.I)),
        ("analytics_engineer", re.compile(r"\banalytics\s+engineer\b", re.I)),
    ),
    RoleFamily.PRODUCT_ENGINEERING: (
        ("product_engineer", re.compile(r"\bproduct\s+(?:software\s+)?engineer\b", re.I)),
    ),
    RoleFamily.PLATFORM_INFRASTRUCTURE_ENGINEERING: (
        ("platform_engineer", re.compile(r"\bplatform\s+(?:infrastructure\s+)?engineer\b", re.I)),
        ("infrastructure_engineer", re.compile(r"\binfrastructure\s+engineer\b", re.I)),
        ("devops_engineer", re.compile(r"\bdevops\s+(?:software\s+)?engineer\b", re.I)),
        (
            "sre_title",
            re.compile(
                r"\bsite\s+reliability\s+engineer\b|"
                r"^\s*(?:(?:staff|principal|senior|lead|junior)\s+)?sre(?:\s+(?:engineer|specialist))?\b",
                re.I,
            ),
        ),
        (
            "devops_title",
            re.compile(r"^\s*(?:(?:staff|principal|senior|lead|junior)\s+)?devops\b", re.I),
        ),
    ),
    RoleFamily.MACHINE_LEARNING_ENGINEERING: (
        (
            "machine_learning_engineer",
            re.compile(r"\b(?:machine\s+learning|ml)\s+(?:software\s+)?engineer\b", re.I),
        ),
    ),
    RoleFamily.AI_ENGINEERING: (
        (
            "ai_engineer",
            re.compile(r"\b(?:ai|artificial\s+intelligence)\s+(?:software\s+)?engineer\b", re.I),
        ),
    ),
    RoleFamily.AI_PRODUCT_ENGINEERING: (("ai_product_engineer", _AI_PRODUCT),),
    RoleFamily.SOLUTIONS_ARCHITECTURE: (
        ("solutions_architect", re.compile(r"\bsolutions?\s+architect\b", re.I)),
        ("solutions_engineer", re.compile(r"\bsolutions?\s+engineer\b", re.I)),
    ),
    RoleFamily.ENGINEERING_MANAGEMENT: (("engineering_management", _ENGINEERING_MANAGEMENT),),
}

_ROLE_TERMS: Final[tuple[tuple[RoleFamily, re.Pattern[str]], ...]] = (
    (RoleFamily.BACKEND_ENGINEERING, re.compile(r"\bback\s*end\b", re.I)),
    (RoleFamily.FRONTEND_ENGINEERING, re.compile(r"\bfront\s*end\b", re.I)),
    (RoleFamily.FULLSTACK_ENGINEERING, re.compile(r"\bfull\s*stack\b", re.I)),
    (RoleFamily.DATA_ENGINEERING, re.compile(r"\bdata\b", re.I)),
    (RoleFamily.PRODUCT_ENGINEERING, re.compile(r"\bproduct\b", re.I)),
    (RoleFamily.PLATFORM_INFRASTRUCTURE_ENGINEERING, re.compile(r"\bplatform\b", re.I)),
    (RoleFamily.MACHINE_LEARNING_ENGINEERING, re.compile(r"\b(?:ml|machine\s+learning)\b", re.I)),
    (RoleFamily.AI_ENGINEERING, re.compile(r"\b(?:ai|artificial\s+intelligence)\b", re.I)),
    (RoleFamily.SOLUTIONS_ARCHITECTURE, re.compile(r"\bsolutions?\b", re.I)),
)
_DISJUNCTION: Final = re.compile(
    r"(?P<left>[\w -]+?)\s*(?:/|&|\band\b|\bor\b)\s*"
    r"(?P<right>[\w -]+?)\s+(?:software\s+)?(?:engineer|developer|architect)\b",
    re.I,
)

_SENIORITY_PATTERNS: Final[tuple[tuple[Seniority, re.Pattern[str]], ...]] = (
    (Seniority.INTERN, re.compile(r"\bintern(?:ship)?\b", re.I)),
    (Seniority.JUNIOR, re.compile(r"\b(?:junior|jr(?:\.)?|entry[- ]level|associate)(?!\w)", re.I)),
    (Seniority.MID, re.compile(r"\bmid(?:[- ]level)?\b", re.I)),
    (Seniority.SENIOR, re.compile(r"\b(?:senior|sr(?:\.)?)(?!\w)", re.I)),
    (Seniority.LEAD, re.compile(r"\blead\b", re.I)),
    (Seniority.STAFF, re.compile(r"\bstaff\b", re.I)),
    (Seniority.PRINCIPAL, re.compile(r"\bprincipal\b", re.I)),
    (Seniority.MANAGER, re.compile(r"\bmanager\b", re.I)),
    (Seniority.DIRECTOR, re.compile(r"\b(?:director|head)\b", re.I)),
    (Seniority.EXECUTIVE, re.compile(r"\b(?:vp|vice\s+president|chief)\b", re.I)),
)
_MANAGEMENT_SENIORITY = frozenset({Seniority.MANAGER, Seniority.DIRECTOR, Seniority.EXECUTIVE})
_CONTEXT_PATTERNS: Final[tuple[tuple[RoleContext, re.Pattern[str]], ...]] = (
    (RoleContext.AI, re.compile(r"\b(?:ai|artificial\s+intelligence|llm|generative\s+ai)\b", re.I)),
    (RoleContext.MACHINE_LEARNING, re.compile(r"\b(?:ml|machine\s+learning)\b", re.I)),
    (RoleContext.PRODUCT, re.compile(r"\bproduct\b", re.I)),
    (RoleContext.BACKEND, re.compile(r"\bback\s*end\b", re.I)),
    (RoleContext.FRONTEND, re.compile(r"\bfront\s*end\b", re.I)),
    (RoleContext.FULLSTACK, re.compile(r"\bfull\s*stack\b", re.I)),
    (RoleContext.DATA, re.compile(r"\bdata\b", re.I)),
    (RoleContext.PLATFORM, re.compile(r"\b(?:platform|devops|sre)\b", re.I)),
    (RoleContext.CLOUD, re.compile(r"\bcloud\b", re.I)),
    (RoleContext.SECURITY, re.compile(r"\bsecurity\b", re.I)),
    (RoleContext.CUSTOMER_FACING, re.compile(r"\bcustomer[- ]facing\b", re.I)),
)

_DUTY_SECTION: Final = re.compile(
    r"^\s*(?:you will:?|responsibilities:?|what you'll do:?)\s*$", re.I
)
_BULLET: Final = re.compile(r"^\s*(?:[•*\-]|\d+[.)])\s+(.+?)\s*$")
_DUTY_PATTERNS: Final[dict[RoleFamily, tuple[str, re.Pattern[str]]]] = {
    RoleFamily.BACKEND_ENGINEERING: (
        "duty_backend_systems",
        re.compile(
            r"^(?:build|design|develop|own|operate|maintain|scale|implement)\s+(?:reliable\s+)?(?:backend|back-end|server-side)\s+(?:systems?|services?|apis?|applications?)\b",
            re.I,
        ),
    ),
    RoleFamily.FRONTEND_ENGINEERING: (
        "duty_frontend_interfaces",
        re.compile(
            r"^(?:build|design|develop|own|maintain|implement)\s+"
            r"(?:responsive\s+)?(?:frontend|front-end|web\s+)?"
            r"(?:user interfaces?|ui|web applications?)\b",
            re.I,
        ),
    ),
    RoleFamily.FULLSTACK_ENGINEERING: (
        "duty_fullstack_features",
        re.compile(
            r"^(?:own|build|develop|deliver)\s+"
            r"(?:end-to-end|full[- ]stack)\s+(?:product\s+)?features?\b",
            re.I,
        ),
    ),
    RoleFamily.DATA_ENGINEERING: (
        "duty_data_pipelines",
        re.compile(
            r"^(?:build|design|develop|own|operate|maintain|scale|implement)\s+"
            r"(?:reliable\s+)?data pipelines?\b",
            re.I,
        ),
    ),
    RoleFamily.PRODUCT_ENGINEERING: (
        "duty_product_features",
        re.compile(r"^(?:own|build|develop|deliver)\s+(?:core\s+)?product features?\b", re.I),
    ),
    RoleFamily.PLATFORM_INFRASTRUCTURE_ENGINEERING: (
        "duty_platform_infrastructure",
        re.compile(
            r"^(?:build|design|develop|own|operate|maintain|automate|scale)\s+"
            r"(?:cloud\s+)?(?:platforms?|infrastructure|kubernetes clusters?|ci/cd platforms?)\b",
            re.I,
        ),
    ),
    RoleFamily.MACHINE_LEARNING_ENGINEERING: (
        "duty_machine_learning_models",
        re.compile(
            r"^(?:train|build|develop|deploy|serve|evaluate|operate)\s+"
            r"(?:production\s+)?(?:machine learning|ml) models?\b",
            re.I,
        ),
    ),
    RoleFamily.AI_ENGINEERING: (
        "duty_ai_systems",
        re.compile(
            r"^(?:build|design|develop|deploy|operate|evaluate)\s+"
            r"(?:production\s+)?(?:ai|artificial intelligence|llm|generative ai)"
            r"(?:-powered)?\s+(?:systems?|applications?|models?)\b",
            re.I,
        ),
    ),
    RoleFamily.SOLUTIONS_ARCHITECTURE: (
        "duty_solution_architecture",
        re.compile(
            r"^(?:design|architect|deliver)\s+(?:secure\s+)?(?:customer|client) solutions?\b", re.I
        ),
    ),
}
_PEOPLE_MANAGEMENT_DUTY: Final = re.compile(
    r"^\s*(?:manage (?:a |the )?(?:team|people|direct reports)|"
    r"manage performance (?:for|of) (?:the )?(?:team|people)|"
    r"have direct reports|own people management)\b",
    re.I,
)
_NEGATED_ACTION: Final = re.compile(
    r"\b(?:do not|don't|never|not)\s+(?:build|design|develop|own|operate|maintain|"
    r"scale|implement|train|deploy|serve|evaluate|architect|deliver)\b",
    re.I,
)
_NEGATED_PEOPLE_MANAGEMENT: Final = re.compile(
    r"\b(?:no|not|never|without|don't|do not)\b.{0,30}\b"
    r"(?:manage|management|direct reports)\b",
    re.I,
)


def classify_role(
    title: str | None,
    description: str | None = None,
    *,
    taxonomy: RoleTaxonomy = ROLE_TAXONOMY,
) -> RoleClassification:
    """Classify one title conservatively, retaining exact supporting evidence."""
    validate_role_taxonomy(taxonomy)
    title_text = title.strip() if isinstance(title, str) else ""
    description_text = description if isinstance(description, str) else ""
    evidence: list[ClassificationEvidence] = []
    if not title_text:
        return _make_result(
            taxonomy,
            None,
            ClassificationStatus.UNKNOWN_SCOPE,
            RoleScope.UNKNOWN_SCOPE,
            None,
            title_text,
            description_text,
            evidence,
        )

    out_match = _OUT_OF_SCOPE_HEAD.search(title_text)
    explicit_management = _ENGINEERING_MANAGEMENT.search(title_text)
    technical_head = _TECHNICAL_ROLE_HEAD.search(title_text)
    if out_match is not None and technical_head is None and explicit_management is None:
        evidence.append(
            _make_evidence(
                EvidenceAxis.SCOPE,
                EvidenceSource.TITLE,
                "nontechnical_role_head",
                out_match.group(0),
            )
        )
        seniority, seniority_evidence = _seniority(title_text, is_management=False)
        evidence.extend(seniority_evidence)
        return _make_result(
            taxonomy,
            None,
            ClassificationStatus.OUT_OF_SCOPE,
            RoleScope.OUT_OF_SCOPE,
            seniority,
            title_text,
            description_text,
            evidence,
        )
    if out_match is not None and (technical_head is not None or explicit_management is not None):
        technical_or_management = (
            technical_head if technical_head is not None else explicit_management
        )
        assert technical_or_management is not None
        evidence.extend(
            (
                _make_evidence(
                    EvidenceAxis.SCOPE,
                    EvidenceSource.TITLE,
                    "nontechnical_role_head",
                    out_match.group(0),
                ),
                _make_evidence(
                    EvidenceAxis.SCOPE,
                    EvidenceSource.TITLE,
                    "technical_role_head",
                    technical_or_management.group(0),
                ),
            )
        )
        seniority, seniority_evidence = _seniority(
            title_text, is_management=explicit_management is not None
        )
        evidence.extend(seniority_evidence)
        return _make_result(
            taxonomy,
            None,
            ClassificationStatus.CONFLICT,
            RoleScope.IN_SCOPE,
            seniority,
            title_text,
            description_text,
            evidence,
        )

    scope_match = technical_head or _TECHNICAL_TITLE.search(title_text) or explicit_management
    if scope_match is None:
        seniority, seniority_evidence = _seniority(title_text, is_management=False)
        evidence.extend(seniority_evidence)
        return _make_result(
            taxonomy,
            None,
            ClassificationStatus.UNKNOWN_SCOPE,
            RoleScope.UNKNOWN_SCOPE,
            seniority,
            title_text,
            description_text,
            evidence,
        )
    evidence.append(
        _make_evidence(
            EvidenceAxis.SCOPE, EvidenceSource.TITLE, "technical_title", scope_match.group(0)
        )
    )

    title_matches = _title_family_matches(title_text)
    seniority, seniority_evidence = _seniority(
        title_text, is_management=explicit_management is not None
    )
    evidence.extend(seniority_evidence)
    evidence.extend(
        _make_evidence(EvidenceAxis.ROLE, EvidenceSource.TITLE, rule_id, matched_text)
        for rule_id, matched_text in title_matches.values()
    )
    duty_matches = _description_duty_matches(description_text)
    evidence.extend(
        _make_evidence(EvidenceAxis.ROLE, EvidenceSource.DESCRIPTION_DUTY, rule_id, duty_text)
        for _, (rule_id, duty_text) in duty_matches.items()
    )

    if len(title_matches) > 1:
        return _make_result(
            taxonomy,
            None,
            ClassificationStatus.AMBIGUOUS,
            RoleScope.IN_SCOPE,
            seniority,
            title_text,
            description_text,
            evidence,
        )
    if not title_matches:
        generic_head = _GENERIC_ENGINEERING_HEAD.match(title_text)
        if generic_head is not None and len(duty_matches) == 1:
            family = next(iter(duty_matches))
            evidence.append(
                _make_evidence(
                    EvidenceAxis.ROLE,
                    EvidenceSource.TITLE,
                    "generic_technical_title",
                    generic_head.group(0),
                )
            )
            return _make_result(
                taxonomy,
                family,
                ClassificationStatus.CLASSIFIED,
                RoleScope.IN_SCOPE,
                seniority,
                title_text,
                description_text,
                evidence,
            )
        if len(duty_matches) > 1:
            return _make_result(
                taxonomy,
                None,
                ClassificationStatus.AMBIGUOUS,
                RoleScope.IN_SCOPE,
                seniority,
                title_text,
                description_text,
                evidence,
            )
        return _make_result(
            taxonomy,
            None,
            ClassificationStatus.UNCLASSIFIED,
            RoleScope.IN_SCOPE,
            seniority,
            title_text,
            description_text,
            evidence,
        )

    family = next(iter(title_matches))
    if duty_matches and set(duty_matches) != {family}:
        return _make_result(
            taxonomy,
            None,
            ClassificationStatus.CONFLICT,
            RoleScope.IN_SCOPE,
            seniority,
            title_text,
            description_text,
            evidence,
        )
    return _make_result(
        taxonomy,
        family,
        ClassificationStatus.CLASSIFIED,
        RoleScope.IN_SCOPE,
        seniority,
        title_text,
        description_text,
        evidence,
    )


def _title_family_matches(title: str) -> dict[RoleFamily, tuple[str, str]]:
    matches: dict[RoleFamily, tuple[str, str]] = {}
    spans: dict[RoleFamily, tuple[int, int]] = {}
    for family, patterns in _ROLE_PATTERNS.items():
        for rule_id, pattern in patterns:
            match = pattern.search(title)
            if match is not None:
                matches[family] = (rule_id, match.group(0))
                spans[family] = match.span()
                break

    ai_product_span = spans.get(RoleFamily.AI_PRODUCT_ENGINEERING)
    product_span = spans.get(RoleFamily.PRODUCT_ENGINEERING)
    if (
        ai_product_span
        and product_span
        and (ai_product_span[0] <= product_span[0] and ai_product_span[1] >= product_span[1])
    ):
        matches.pop(RoleFamily.PRODUCT_ENGINEERING, None)
        spans.pop(RoleFamily.PRODUCT_ENGINEERING, None)

    specialty = _PRODUCT_BACKEND_SPECIALTY.search(title)
    if specialty is not None:
        matches.pop(RoleFamily.PRODUCT_ENGINEERING, None)
        spans.pop(RoleFamily.PRODUCT_ENGINEERING, None)
        matches[RoleFamily.BACKEND_ENGINEERING] = (
            "product_engineer_backend_specialty",
            specialty.group(0),
        )

    disjunction = _DISJUNCTION.search(title)
    if disjunction is not None:
        for side in (disjunction.group("left"), disjunction.group("right")):
            disjunction_family = _disjunction_family(side)
            if disjunction_family is not None:
                matches.setdefault(
                    disjunction_family,
                    ("cross_family_disjunction", disjunction.group(0)),
                )

    platform_context = re.search(r"\b(?:ml|machine\s+learning|ai)\s+platform\b", title, re.I)
    if not matches and platform_context is not None and _GENERIC_ENGINEERING_HEAD.match(title):
        matches[RoleFamily.PLATFORM_INFRASTRUCTURE_ENGINEERING] = (
            "platform_context_specialty",
            platform_context.group(0),
        )
    return matches


def _disjunction_family(segment: str) -> RoleFamily | None:
    """Resolve one side of a title disjunction without promoting its context."""
    if _AI_PRODUCT.search(segment):
        return RoleFamily.AI_PRODUCT_ENGINEERING
    if re.search(r"\bplatform\b", segment, re.I):
        return RoleFamily.PLATFORM_INFRASTRUCTURE_ENGINEERING
    for family, pattern in _ROLE_TERMS:
        if pattern.search(segment) is None:
            continue
        if family is RoleFamily.AI_ENGINEERING and re.search(
            r"\b(?:product|platform)\b", segment, re.I
        ):
            continue
        if family is RoleFamily.MACHINE_LEARNING_ENGINEERING and re.search(
            r"\bplatform\b", segment, re.I
        ):
            continue
        return family
    return None


def _seniority(
    title: str, *, is_management: bool
) -> tuple[Seniority | None, tuple[ClassificationEvidence, ...]]:
    matches: list[tuple[Seniority, str]] = []
    for seniority, pattern in _SENIORITY_PATTERNS:
        match = pattern.search(title)
        if match is not None:
            matches.append((seniority, match.group(0)))

    if is_management:
        level_modifiers = [item for item in matches if item[0] not in _MANAGEMENT_SENIORITY]
        candidates = level_modifiers or [
            item for item in matches if item[0] in _MANAGEMENT_SENIORITY
        ]
    else:
        candidates = matches

    if not candidates:
        return None, ()
    if len(candidates) != 1:
        evidence = tuple(
            ClassificationEvidence(
                EvidenceAxis.SENIORITY,
                EvidenceSource.TITLE,
                "conflicting_seniority_markers",
                text,
            )
            for _, text in candidates
        )
        return None, evidence
    seniority, text = candidates[0]
    return seniority, (
        ClassificationEvidence(
            EvidenceAxis.SENIORITY,
            EvidenceSource.TITLE,
            "title_seniority",
            text,
        ),
    )


def _description_duty_matches(description: str) -> dict[RoleFamily, tuple[str, str]]:
    duties = _duty_bullets(description)
    matches: dict[RoleFamily, tuple[str, str]] = {}
    for duty in duties:
        if _NEGATED_ACTION.search(duty):
            continue
        for family, (rule_id, pattern) in _DUTY_PATTERNS.items():
            if pattern.search(duty) is not None:
                matches.setdefault(family, (rule_id, duty))
    return matches


def _duty_bullets(description: str) -> tuple[str, ...]:
    in_duties = False
    duties: list[str] = []
    for line in description.splitlines():
        if _DUTY_SECTION.fullmatch(line):
            in_duties = True
            continue
        bullet = _BULLET.match(line)
        if not in_duties:
            continue
        if bullet is None:
            if line.strip():
                in_duties = False
            continue
        duties.append(bullet.group(1))
    return tuple(duties)


def _make_result(
    taxonomy: RoleTaxonomy,
    family: RoleFamily | None,
    status: ClassificationStatus,
    scope: RoleScope,
    seniority: Seniority | None,
    title: str,
    description: str,
    evidence: list[ClassificationEvidence],
) -> RoleClassification:
    contexts: list[RoleContext] = []
    for context, pattern in _CONTEXT_PATTERNS:
        match = pattern.search(title)
        if match is not None:
            contexts.append(context)
            evidence.append(
                _make_evidence(
                    EvidenceAxis.CONTEXT,
                    EvidenceSource.TITLE,
                    f"context_{context.value}",
                    match.group(0),
                )
            )

    manager_title = re.search(
        r"\bengineering\s+manager\b|\bmanager\s+of\s+engineering\b", title, re.I
    )
    if manager_title is not None:
        managerial_function: bool | None = True
        evidence.append(
            _make_evidence(
                EvidenceAxis.MANAGERIAL_FUNCTION,
                EvidenceSource.TITLE,
                "engineering_manager_title",
                manager_title.group(0),
            )
        )
    else:
        manager_duty = next(
            (
                duty
                for duty in _duty_bullets(description)
                if not _NEGATED_PEOPLE_MANAGEMENT.search(duty)
                and _PEOPLE_MANAGEMENT_DUTY.search(duty)
            ),
            None,
        )
        managerial_function = manager_duty is not None
        if manager_duty is None:
            managerial_function = None
        else:
            evidence.append(
                _make_evidence(
                    EvidenceAxis.MANAGERIAL_FUNCTION,
                    EvidenceSource.DESCRIPTION_DUTY,
                    "explicit_people_management_duty",
                    manager_duty,
                )
            )

    scope_evidence = next((item for item in evidence if item.axis is EvidenceAxis.SCOPE), None)
    if scope_evidence is None and scope is RoleScope.IN_SCOPE:
        match = _TECHNICAL_TITLE.search(title)
        if match is not None:
            evidence.append(
                _make_evidence(
                    EvidenceAxis.SCOPE, EvidenceSource.TITLE, "technical_title", match.group(0)
                )
            )
    return RoleClassification(
        classifier_version=CLASSIFIER_VERSION,
        taxonomy_version=taxonomy.version,
        role_family=family,
        status=status,
        scope=scope,
        seniority=seniority,
        managerial_function=managerial_function,
        context=tuple(contexts),
        evidence=tuple(evidence),
    )


def _make_evidence(
    axis: EvidenceAxis,
    source: EvidenceSource,
    rule_id: str,
    matched_text: str,
) -> ClassificationEvidence:
    return ClassificationEvidence(axis, source, rule_id, matched_text)
