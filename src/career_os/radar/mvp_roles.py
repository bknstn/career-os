"""Conservative title-head fallbacks for the current Radar inventory.

The frozen R1 classifier and B adapter remain reproducible. This separately
versioned layer fills only B's unrecognized/unestablished occupations; it never
resolves conflicts, guesses a specialty from a language, or uses company copy
as evidence of a person's job. Every new label cites the original title head.
"""

from __future__ import annotations

import re
from typing import Final

from career_os.radar.role_contract import EvidenceRef, RoleAssignment, adapt_classification
from career_os.radar.roles import classify_role

MVP_ROLE_CLASSIFIER_VERSION: Final = "3.0.0"

_LEVEL = r"(?:senior|sr\.?|staff|principal|lead|junior|jr\.?|mid|intermediate|distinguished)"
_PREFIX = rf"\s*(?:(?:{_LEVEL}|founding|associate|global|regional|group)(?:\s*/\s*|\s+))*"
_END = (
    r"(?:\s+(?:I|II|III|IV|Intern(?:ship)?))?"
    r"(?=\s*$|\s*[,|:(\[\u2010-\u2015\u2212-]|"
    r"\s+(?:EMEA|DACH|US|USA|UK|Netherlands|REMOTE|with)\b)"
)
_APPLICATION = re.compile(
    r"\btalent\s+(?:pool|community|network)\b|\bgeneral\s+application\b|"
    r"\breferral\s+program\b|\bapply\s+for\s+(?:the\s+)?future\b",
    re.I,
)

# Function first, then job head. A bare 'manager', 'analyst', or technology
# mention is deliberately insufficient. Suffixes may name a team or geography.
_BUSINESS_HEADS: Final = {
    "people": (
        r"(?:technical\s+)?recruiter|"
        r"(?:technical\s+)?recruiting\s+manager|"
        r"(?:hr|human\s+resources)\s+(?:business\s+partner|operations\s+specialist|"
        r"specialist|manager|director)|"
        r"people\s+(?:business\s+partner|partner|operations\s+(?:manager|specialist)|"
        r"technology\s+analyst)|people\s*&\s*talent\s+lead|"
        r"(?:global\s+)?talent\s+(?:acquisition\s+(?:specialist|manager|partner)|"
        r"partner|brand\s+manager|marketing\s+manager)|"
        r"(?:candidate\s+experience|employer\s+brand)\s+(?:specialist|manager|lead)|"
        r"benefits\s+analyst"
    ),
    "product": r"(?:technical\s+|growth\s+|ai\s+)?product\s+(?:manager|owner|analyst)",
    "sales": (
        r"(?:(?:enterprise|strategic|major|commercial|mid-market|small\s+business|"
        r"smb|public\s+sector|new\s+business|key|b2b)\s+)*"
        r"(?:account(?:s)?\s+(?:executive|manager|director|management\s+manager)|"
        r"sales\s+(?:development\s+representative|executive|manager|director|leader)|"
        r"business\s+development\s+(?:representative|manager))|"
        r"(?:sales|revenue)\s+(?:operations|enablement)\s+(?:manager|lead|specialist)|"
        r"(?:strategic\s+)?partnerships?\s+manager|"
        r"(?:deal\s+(?:desk|strategy)|renewals(?:\s+operations)?)\s+(?:manager|specialist)"
    ),
    "marketing": (
        r"(?:(?:content|brand|product|creative|field|partner|growth|performance|"
        r"gtm|lifecycle|talent|communications|crm\s*&\s*email)\s+)*"
        r"marketing\s+(?:manager|specialist|analyst|lead|director|designer|project\s+manager)|"
        r"(?:paid\s+media|public\s+relations|social\s+media|communications|"
        r"demand\s+generation|google\s+ads|seo|technical\s+seo|on-site\s+seo)\s+"
        r"(?:manager|specialist|lead|team\s+lead)|"
        r"(?:events?|field\s*&\s*events|events\s*&\s*employee\s+engagement)\s+(?:manager|lead)|"
        r"(?:content|video)\s+team\s+lead|cro\s+specialist"
    ),
    "design": (
        r"(?:(?:product|ux|ui|brand|visual|content|web|multimedia|motion|graphic|"
        r"marketing)\s+)+designer|copywriter"
    ),
    "finance_legal": (
        r"(?:(?:tax|staff|financial|finance|accounting|treasury|risk\s+operations|"
        r"compliance\s+operations|business|quantitative|fp&a|ap|sec)\s+)+"
        r"(?:accountant|analyst|specialist|manager|director)|"
        r"accountant|(?:group\s+)?controller|"
        r"(?:regulatory\s+)?compliance\s+(?:officer|manager|lead|director)|"
        r"(?:(?:legal|commercial|corporate|product|privacy|technology|capital\s+markets)"
        r"(?:\s*&\s*|\s+))*counsel|(?:corporate\s+)?lawyer|contracts\s+manager"
    ),
    "business_operations": (
        r"(?:business\s+)?operations\s+(?:analyst|manager|specialist|supervisor|"
        r"enablement\s+(?:manager|lead)|safety\s+lead)|"
        r"(?:administrative|executive)\s+(?:assistant|manager)|"
        r"(?:office|workplace|travel|program|project|delivery|engagement|integration)\s+manager|"
        r"(?:strategic\s+)?(?:customer|client)\s+(?:success|support|experience)\s+"
        r"(?:(?:operations|strategy\s+and\s+operations|programs)\s+)?"
        r"(?:manager|lead|agent|specialist)|"
        r"product\s+support\s+specialist|autonomous\s+gse\s+operator|"
        r"vehicle\s+service\s+technician"
    ),
    "business_leadership": (
        r"(?:head|director|vp|vice\s+president)(?:\s*,\s*|\s+(?:of\s+)?)"
        r"(?:product(?:\s+(?:management|growth|marketing))?|(?:b2b\s+)?sales|finance|"
        r"design|marketing|lifecycle\s+marketing|corporate\s+development|e-commerce|"
        r"(?:strategic\s+)?partnerships|recruiting|social\s+media|legal|fp&a)|"
        r"chief\s+(?:(?:executive|revenue|financial|operating|people|marketing)\s+officer|"
        r"of\s+staff(?:\s+director)?)"
    ),
    "business_management": (
        r"(?:manager|director|head|vp|vice\s+president)(?:\s*,\s*|\s+of\s+)"
        r"(?:(?:technical\s+)?recruiting|(?:global\s+)?payroll|"
        r"(?:revenue|sales|business|fraud|finance|people|professional\s+services)\s+operations|"
        r"customer\s+success(?:\s+strategy\s+and\s+operations)?|"
        r"sales\s+strategy\s+and\s+operations|deal\s+desk|programs|renewals)"
    ),
}

_ENGINEERING_HEADS: Final = {
    "software": (
        r"(?:software|ciam\s+software|internal\s+context|forward\s+deployed)\s+engineer|"
        r"software\s+developer|full[- ]stack\s+engineer|engineer|developer|"
        r"(?:php|java|kotlin|go|golang|scala|\.net|node\.js|typescript|react)\s+"
        r"(?:engineer|developer)"
    ),
    "systems": (
        r"(?:systems?|integration|migration|mapping|reliability|release|distribution|"
        r"web\s+tracking|people\s+systems|it)\s+engineer|"
        r"(?:linux\s+)?system\s+administrator|it\s+workspace\s+administrator"
    ),
    "security": (
        r"(?:(?:cloud|application|product|corporate|offensive\s+cyber|secops|"
        r"software)\s+)?security\s+(?:(?:operations|governance|risk)\s+)?engineer|"
        r"soc\s+l2/l3\s+engineer|software\s+safety\s+engineer"
    ),
    "hardware": r"(?:electrical|mechanical|hardware(?:\s+field)?)\s+engineer",
    "mobile": r"(?:ios|android|android\s+mobile|mobile)\s+(?:engineer|developer)",
    "quality": r"(?:quality|(?:automation\s+)?qa(?:\s+automation)?)\s+engineer",
    "research": r"(?:(?:applied\s+)?research|threat\s+research)\s+engineer",
    "architecture": (
        r"(?:(?:gcp|aws)\s+)?(?:data|database|fullstack|product\s+security|"
        r"agentic\s+ai|enterprise)\s+architect"
    ),
    "devops": r"(?:aws|gcp)\s+devops",
}


def _compile(heads: dict[str, str]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple(
        (key, re.compile(rf"^{_PREFIX}(?P<head>{head}){_END}", re.I)) for key, head in heads.items()
    )


_BUSINESS_RULES: Final = _compile(_BUSINESS_HEADS)
_ENGINEERING_RULES: Final = _compile(_ENGINEERING_HEADS)
_ARCHITECT = re.compile(
    rf"^{_PREFIX}(?P<head>professional\s+services(?:\s*-\s*|\s+)technical\s+architect){_END}",
    re.I,
)
_ENGINEERING_MANAGEMENT = re.compile(
    rf"^{_PREFIX}(?P<head>(?:director|head|vp|vice\s+president)"
    r"(?:\s*,\s*|\s+(?:of\s+)?)"
    rf"(?:(?:analytics|forward\s+deployed|off-chain)\s+)?engineering){_END}",
    re.I,
)


def classify_mvp_role(title: str, description: str | None = None) -> RoleAssignment:
    """Keep accepted assignments; add exact, closed title-head evidence to gaps."""
    previous = adapt_classification(title, classify_role(title, description))
    if previous.occupation != "unknown" or previous.reason not in {
        "unrecognized_title",
        "occupation_not_established",
    }:
        return previous
    if _APPLICATION.search(title):
        return previous

    rules = (
        ("nontechnical", _BUSINESS_RULES),
        ("solutions_architecture", (("professional_services_architect", _ARCHITECT),)),
        ("engineering_management", (("engineering_management", _ENGINEERING_MANAGEMENT),)),
        ("technical_engineering", _ENGINEERING_RULES),
    )
    for occupation, patterns in rules:
        for rule_id, pattern in patterns:
            match = pattern.match(title)
            if match is None:
                continue
            engineering = occupation == "technical_engineering"
            return RoleAssignment(
                occupation=occupation,
                specialty=None,
                specialty_state="missing" if engineering else "not_applicable",
                reason="no_family_evidence" if engineering else "not_applicable",
                candidate_families=(),
                evidence=(
                    *previous.evidence,
                    EvidenceRef(
                        axis="occupation",
                        source="title",
                        rule_id=f"mvp_{rule_id}_head",
                        matched_text=match.group("head"),
                    ),
                ),
            )
    return previous
