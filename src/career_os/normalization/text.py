"""Deterministic, deliberately small HTML and plain-text cleanup helpers."""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser
from typing import Final
from unicodedata import normalize
from urllib.parse import urlsplit

_BLOCK_TAGS: Final = frozenset({"article", "div", "h1", "h2", "h3", "h4", "h5", "h6", "p"})
_LIST_TAGS: Final = frozenset({"ul", "ol"})
_DROP_CONTENT_TAGS: Final = frozenset({"script", "style", "template"})
_SAFE_TAGS: Final = frozenset(
    {
        "a",
        "b",
        "br",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "i",
        "li",
        "ol",
        "p",
        "strong",
        "ul",
    }
)
_HORIZONTAL_WHITESPACE: Final = re.compile(r"[\t\f\v ]+")
_EXCESS_BLANK_LINES: Final = re.compile(r"\n{3,}")


def clean_text(value: str) -> str:
    """Normalize display text while preserving at most one blank line boundary.

    Text is NFKC-normalized, non-breaking/zero-width spacing is made explicit,
    and repeated horizontal whitespace is collapsed.  Display casing is never
    changed.  The result is stripped and therefore suitable for required-field
    validation after a non-blank check by the caller.
    """
    if not isinstance(value, str):
        raise TypeError("text must be a string")

    normalized = normalize("NFKC", value)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\u00a0", " ").replace("\u2007", " ")
    normalized = normalized.replace("\u202f", " ").replace("\u200b", "")
    normalized = normalized.replace("\u2028", "\n").replace("\u2029", "\n")
    normalized = _HORIZONTAL_WHITESPACE.sub(" ", normalized)
    normalized = "\n".join(line.strip() for line in normalized.split("\n"))
    return _EXCESS_BLANK_LINES.sub("\n\n", normalized).strip()


def _is_safe_href(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


class _SanitizingHtmlParser(HTMLParser):
    """Allow a small stable HTML subset and discard executable content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.open_tags: list[str] = []
        self.drop_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            self.drop_tags.append(tag)
            return
        if self.drop_tags or tag not in _SAFE_TAGS:
            return
        if tag == "br":
            self.parts.append("<br>")
            return
        if tag == "a":
            href = next((value for name, value in attrs if name.lower() == "href"), None)
            if href is not None and _is_safe_href(href.strip()):
                self.parts.append(f'<a href="{escape(href.strip(), quote=True)}">')
            else:
                self.parts.append("<a>")
        else:
            self.parts.append(f"<{tag}>")
        self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() != "br":
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            if self.drop_tags and self.drop_tags[-1] == tag:
                self.drop_tags.pop()
            return
        if self.drop_tags or tag not in _SAFE_TAGS or tag == "br":
            return
        if tag not in self.open_tags:
            return
        while self.open_tags:
            open_tag = self.open_tags.pop()
            self.parts.append(f"</{open_tag}>")
            if open_tag == tag:
                return

    def handle_data(self, data: str) -> None:
        if not self.drop_tags:
            self.parts.append(escape(data, quote=False))

    def rendered(self) -> str:
        while self.open_tags:
            self.parts.append(f"</{self.open_tags.pop()}>")
        return "".join(self.parts)


class _TextHtmlParser(HTMLParser):
    """Render source HTML as cleaned, section-preserving plain text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.drop_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            self.drop_tags.append(tag)
        elif not self.drop_tags:
            if tag in _BLOCK_TAGS or tag in _LIST_TAGS or tag == "br":
                self.parts.append("\n")
            elif tag == "li":
                self.parts.append("\n• ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            if self.drop_tags and self.drop_tags[-1] == tag:
                self.drop_tags.pop()
        elif not self.drop_tags and (tag in _BLOCK_TAGS or tag in _LIST_TAGS or tag == "li"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.drop_tags:
            self.parts.append(data)


def sanitize_html(value: str) -> str:
    """Return a deterministic allowlisted HTML fragment with no executable content."""
    if not isinstance(value, str):
        raise TypeError("html must be a string")
    parser = _SanitizingHtmlParser()
    parser.feed(value)
    parser.close()
    return parser.rendered()


def html_to_text(value: str) -> str:
    """Convert source HTML into normalized text without script/style/template content."""
    if not isinstance(value, str):
        raise TypeError("html must be a string")
    parser = _TextHtmlParser()
    parser.feed(value)
    parser.close()
    return clean_text("".join(parser.parts))
