"""Deterministic normalization helpers without source or persistence dependencies."""

from career_os.normalization.text import clean_text, html_to_text, sanitize_html

__all__ = ["SkillExtractor", "clean_text", "html_to_text", "sanitize_html"]


def __getattr__(name: str) -> object:
    """Keep the text helpers importable while the skill domain is initializing."""
    if name == "SkillExtractor":
        from career_os.normalization.skills import SkillExtractor

        return SkillExtractor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
