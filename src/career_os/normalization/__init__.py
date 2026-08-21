"""Deterministic normalization helpers without source or persistence dependencies."""

from career_os.normalization.text import clean_text, html_to_text, sanitize_html

__all__ = [
    "JOB_NORMALIZER_VERSION",
    "SkillExtractor",
    "clean_text",
    "html_to_text",
    "normalize_job",
    "sanitize_html",
]


def __getattr__(name: str) -> object:
    """Keep the text helpers importable while the skill domain is initializing."""
    if name == "SkillExtractor":
        from career_os.normalization.skills import SkillExtractor

        return SkillExtractor
    if name in {"JOB_NORMALIZER_VERSION", "normalize_job"}:
        from career_os.normalization.jobs import JOB_NORMALIZER_VERSION, normalize_job

        return {"JOB_NORMALIZER_VERSION": JOB_NORMALIZER_VERSION, "normalize_job": normalize_job}[
            name
        ]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
