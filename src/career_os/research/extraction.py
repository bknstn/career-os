"""Deterministic text extraction for acquired research documents.

This module is deliberately offline: callers provide the exact response body,
and this code returns normalized text plus stable section/page ranges.
"""

from __future__ import annotations

import codecs
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from io import BytesIO

from pypdf import PdfReader

from career_os.domain.research import (
    ResearchDateDisclosure,
    ResearchDateState,
    ResearchMediaType,
    ResearchTextRegion,
)

EXTRACTION_VERSION = "research-extraction-v1"
# Titles and short teasers are metadata, not a sufficient extracted report body.
_MIN_HTML_WORDS = 40
_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_IGNORED_ELEMENTS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "nav",
        "header",
        "footer",
        "form",
        "template",
        "iframe",
        "object",
        "svg",
        "canvas",
    }
)
_BLOCK_ELEMENTS = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
_ARTICLE_TYPES = frozenset(
    {
        "article",
        "blogposting",
        "newsarticle",
        "report",
        "scholarlyarticle",
        "techarticle",
    }
)
_UI_CLASS_TOKENS = frozenset(
    {
        "advert",
        "banner",
        "breadcrumb",
        "cookie",
        "footer",
        "header",
        "menu",
        "modal",
        "navigation",
        "newsletter",
        "overlay",
        "promo",
        "recommend",
        "related",
        "share",
        "sidebar",
        "social",
    }
)
_HTML_ENCODING_RE = re.compile(
    r"<meta\b[^>]*(?:charset\s*=\s*['\"]?\s*([\w.-]+)|"
    r"content\s*=\s*['\"][^'\"]*?charset\s*=\s*([\w.-]+))",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class ResearchExtraction:
    """A normalized document plus offsets suitable for citation validation."""

    title: str | None
    publication_date: ResearchDateDisclosure
    normalized_text: str | None
    regions: tuple[ResearchTextRegion, ...]
    extraction_version: str
    error_code: str | None = None


type _Child = _HTMLNode | str


@dataclass(slots=True)
class _HTMLNode:
    tag: str
    attrs: dict[str, str]
    parent: _HTMLNode | None
    children: list[_Child]


class _DocumentParser(HTMLParser):
    """Build a small in-memory tree; scripts and markup remain unexecuted data."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HTMLNode("#document", {}, None, [])
        self.stack = [self.root]
        self.head_meta: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)

    def _start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_attrs = {key.lower(): value or "" for key, value in attrs}
        parent = self.stack[-1]
        node = _HTMLNode(tag.lower(), normalized_attrs, parent, [])
        parent.children.append(node)
        if node.tag == "meta" and _has_ancestor(node, "head"):
            self.head_meta.append(normalized_attrs)
        if node.tag not in _VOID_ELEMENTS and tag.lower() not in _VOID_ELEMENTS:
            self.stack.append(node)


def extract_research_document(
    body: bytes,
    media_type: ResearchMediaType,
    *,
    charset: str | None = None,
) -> ResearchExtraction:
    """Extract report text without network, filesystem, or execution side effects."""
    if not isinstance(body, bytes) or not body:
        return _failure("empty_body")
    if media_type is ResearchMediaType.HTML:
        return _extract_html(body, charset)
    return _extract_pdf(body)


def _extract_html(body: bytes, charset: str | None) -> ResearchExtraction:
    text = _decode_html(body, charset)
    parser = _DocumentParser()
    try:
        parser.feed(text)
        parser.close()
    except (AssertionError, ValueError):
        return _failure("malformed_html")

    try:
        selected = _select_content_node(parser.root)
        title = _document_title(selected, parser.root)
        publication_date = _html_publication_date(parser, title)
        fallback_section = title or "Document"
        blocks = _html_blocks(selected, fallback_section)
    except RecursionError:
        return _failure("html_structure_too_deep")
    if title and not any(block.text.casefold() == title.casefold() for block in blocks):
        blocks.insert(0, _TextBlock(title, title))
    normalized_text, regions = _render_html_blocks(blocks)
    if normalized_text is None or len(_WORD_RE.findall(normalized_text)) < _MIN_HTML_WORDS:
        return ResearchExtraction(
            title=title,
            publication_date=publication_date,
            normalized_text=None,
            regions=(),
            extraction_version=EXTRACTION_VERSION,
            error_code="html_content_too_short",
        )
    return ResearchExtraction(
        title=title,
        publication_date=publication_date,
        normalized_text=normalized_text,
        regions=regions,
        extraction_version=EXTRACTION_VERSION,
    )


def _extract_pdf(body: bytes) -> ResearchExtraction:
    try:
        reader = PdfReader(BytesIO(body), strict=False)
        if reader.is_encrypted:
            return _failure("encrypted_pdf")
        page_texts: list[tuple[int, str]] = []
        for index, page in enumerate(reader.pages, start=1):
            extracted = page.extract_text()
            normalized = _normalize_pdf_page(extracted or "")
            if normalized:
                page_texts.append((index, normalized))
    except Exception:  # malformed or unsupported PDF structures are source data failures
        return _failure("malformed_pdf")

    if not page_texts:
        return _failure("pdf_no_extractable_text")

    pieces: list[str] = []
    regions: list[ResearchTextRegion] = []
    cursor = 0
    for page_number, page_text in page_texts:
        if pieces:
            pieces.append("\n\n")
            cursor += 2
        start = cursor
        pieces.append(page_text)
        cursor += len(page_text)
        regions.append(
            ResearchTextRegion(
                media_type=ResearchMediaType.PDF,
                start_offset=start,
                end_offset=cursor,
                page_number=page_number,
            )
        )

    title: str | None = None
    try:
        metadata_title = reader.metadata.title if reader.metadata is not None else None
        title = _normalize_inline(str(metadata_title)) if metadata_title else None
    except Exception:
        # Broken optional metadata must not discard valid page text.
        title = None

    return ResearchExtraction(
        title=title or None,
        publication_date=_not_reported_date(),
        normalized_text="".join(pieces),
        regions=tuple(regions),
        extraction_version=EXTRACTION_VERSION,
    )


def _failure(error_code: str) -> ResearchExtraction:
    return ResearchExtraction(
        title=None,
        publication_date=_not_reported_date(),
        normalized_text=None,
        regions=(),
        extraction_version=EXTRACTION_VERSION,
        error_code=error_code,
    )


def _not_reported_date() -> ResearchDateDisclosure:
    return ResearchDateDisclosure(None, ResearchDateState.NOT_REPORTED)


def _decode_html(body: bytes, response_charset: str | None) -> str:
    meta_charset: str | None = None
    prefix = body[:8192].decode("ascii", errors="ignore")
    match = _HTML_ENCODING_RE.search(prefix)
    if match:
        meta_charset = match.group(1) or match.group(2)
    for candidate in (response_charset, meta_charset, "utf-8"):
        if not candidate:
            continue
        try:
            encoding = codecs.lookup(candidate).name
        except LookupError:
            continue
        return body.decode(encoding, errors="replace").lstrip("\ufeff")
    return body.decode("utf-8", errors="replace").lstrip("\ufeff")


def _select_content_node(root: _HTMLNode) -> _HTMLNode:
    articles = [
        node
        for node in _walk_nodes(root)
        if node.tag == "article" and not _has_ignored_ancestor(node)
    ]
    mains = [
        node
        for node in _walk_nodes(root)
        if (node.tag == "main" or node.attrs.get("role", "").casefold() == "main")
        and not _has_ignored_ancestor(node)
    ]
    best_article = _longest_visible_node(articles)
    best_main = _longest_visible_node(mains)
    if best_article is not None:
        article_words = len(_WORD_RE.findall(_visible_text(best_article)))
        main_words = len(_WORD_RE.findall(_visible_text(best_main))) if best_main else 0
        if best_main is None or article_words >= max(_MIN_HTML_WORDS, main_words * 0.45):
            return best_article
    if best_main is not None:
        return best_main
    bodies = [node for node in _walk_nodes(root) if node.tag == "body"]
    return _longest_visible_node(bodies) or root


def _longest_visible_node(nodes: list[_HTMLNode]) -> _HTMLNode | None:
    visible = [node for node in nodes if _visible_text(node)]
    if not visible:
        return None
    return max(visible, key=lambda node: len(_visible_text(node)))


def _document_title(selected: _HTMLNode, root: _HTMLNode) -> str | None:
    for node in _walk_nodes(selected):
        if node.tag == "h1" and not _is_ignored(node):
            heading = _normalize_inline(_visible_text(node))
            if heading:
                return heading
    for node in _walk_nodes(root):
        if node.tag == "title":
            title = _normalize_inline(_visible_text(node))
            if title:
                return title
    return None


@dataclass(frozen=True, slots=True)
class _TextBlock:
    text: str
    section: str


def _html_blocks(root: _HTMLNode, fallback_section: str) -> list[_TextBlock]:
    blocks: list[_TextBlock] = []
    active_section = fallback_section
    pending: list[str] = []

    def flush() -> None:
        value = _normalize_inline("".join(pending))
        pending.clear()
        if value:
            blocks.append(_TextBlock(value, active_section))

    def visit(node: _Child) -> None:
        nonlocal active_section
        if isinstance(node, str):
            pending.append(node)
            return
        if _is_ignored(node):
            return
        if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            flush()
            heading = _normalize_inline(_visible_text(node))
            if heading:
                active_section = heading
                blocks.append(_TextBlock(heading, active_section))
            return
        is_block = node.tag in _BLOCK_ELEMENTS
        if is_block:
            flush()
        for child in node.children:
            visit(child)
        if is_block:
            flush()

    visit(root)
    flush()
    return blocks


def _render_html_blocks(
    blocks: list[_TextBlock],
) -> tuple[str | None, tuple[ResearchTextRegion, ...]]:
    if not blocks:
        return None, ()
    pieces: list[str] = []
    regions: list[ResearchTextRegion] = []
    cursor = 0
    for block in blocks:
        if pieces:
            pieces.append("\n\n")
            cursor += 2
        start = cursor
        pieces.append(block.text)
        cursor += len(block.text)
        if regions and regions[-1].section == block.section:
            previous = regions[-1]
            regions[-1] = ResearchTextRegion(
                media_type=ResearchMediaType.HTML,
                start_offset=previous.start_offset,
                end_offset=cursor,
                section=block.section,
            )
        else:
            regions.append(
                ResearchTextRegion(
                    media_type=ResearchMediaType.HTML,
                    start_offset=start,
                    end_offset=cursor,
                    section=block.section,
                )
            )
    return "".join(pieces), tuple(regions)


def _html_publication_date(
    parser: _DocumentParser,
    document_title: str | None,
) -> ResearchDateDisclosure:
    explicit_values: list[str] = []
    for attrs in parser.head_meta:
        raw_key = attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or ""
        key = raw_key.strip().casefold()
        if key in {"article:published_time", "datepublished"} and attrs.get("content"):
            explicit_values.append(attrs["content"].strip())

    json_documents: list[tuple[str | None, str]] = []
    for node in _walk_nodes(parser.root):
        if node.tag != "script" or node.attrs.get("type", "").casefold() != "application/ld+json":
            continue
        raw = "".join(child for child in node.children if isinstance(child, str)).strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        json_documents.extend(_jsonld_publication_dates(payload))

    if document_title:
        matching = [
            publication_date
            for candidate_title, publication_date in json_documents
            if candidate_title is not None and _titles_match(document_title, candidate_title)
        ]
        if matching:
            explicit_values.extend(matching)
    elif len(json_documents) == 1:
        explicit_values.append(json_documents[0][1])

    if not explicit_values:
        return _not_reported_date()
    parsed_dates: set[date] = set()
    invalid_date = False
    for value in explicit_values:
        parsed = _parse_explicit_date(value)
        if parsed is None:
            invalid_date = True
        else:
            parsed_dates.add(parsed)
    if len(parsed_dates) == 1 and not invalid_date:
        return ResearchDateDisclosure(parsed_dates.pop(), ResearchDateState.REPORTED)
    if len(parsed_dates) == 1 and invalid_date:
        return ResearchDateDisclosure(parsed_dates.pop(), ResearchDateState.REPORTED)
    return ResearchDateDisclosure(None, ResearchDateState.UNCLEAR)


def _jsonld_publication_dates(payload: object) -> list[tuple[str | None, str]]:
    result: list[tuple[str | None, str]] = []

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        raw_types = value.get("@type")
        type_values = raw_types if isinstance(raw_types, list) else [raw_types]
        types = {
            str(item).rsplit("/", maxsplit=1)[-1].casefold()
            for item in type_values
            if isinstance(item, str)
        }
        raw_date = value.get("datePublished")
        if types.intersection(_ARTICLE_TYPES) and isinstance(raw_date, str):
            title_value = value.get("headline") or value.get("name")
            title = title_value.strip() if isinstance(title_value, str) else None
            result.append((title, raw_date.strip()))
        for key, item in value.items():
            if key in {"@graph", "mainEntity", "mainEntityOfPage"}:
                visit(item)

    visit(payload)
    return result


def _parse_explicit_date(value: str) -> date | None:
    candidate = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
            return date.fromisoformat(candidate)
        return datetime.fromisoformat(candidate.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _titles_match(left: str, right: str) -> bool:
    def key(value: str) -> str:
        return " ".join(re.findall(r"[\w]+", unicodedata.normalize("NFC", value).casefold()))

    first = key(left)
    second = key(right)
    if not first or not second:
        return False
    if first == second:
        return True
    shorter, longer = sorted((first, second), key=len)
    return len(shorter) >= len(longer) * 0.6 and longer.startswith(shorter)


def _normalize_pdf_page(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).replace("\x00", "")
    lines = [_normalize_inline(line) for line in normalized.splitlines()]
    compact: list[str] = []
    for line in lines:
        if line:
            compact.append(line)
        elif compact and compact[-1] != "":
            compact.append("")
    while compact and not compact[-1]:
        compact.pop()
    return "\n".join(compact)


def _normalize_inline(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).replace("\xa0", " ")
    return " ".join(normalized.split())


def _visible_text(node: _HTMLNode) -> str:
    pieces: list[str] = []

    def visit(current: _Child) -> None:
        if isinstance(current, str):
            pieces.append(current)
            return
        if _is_ignored(current):
            return
        for child in current.children:
            visit(child)

    visit(node)
    return " ".join(pieces)


def _walk_nodes(root: _HTMLNode) -> list[_HTMLNode]:
    result: list[_HTMLNode] = []
    stack = [root]
    while stack:
        node = stack.pop()
        result.append(node)
        stack.extend(child for child in reversed(node.children) if isinstance(child, _HTMLNode))
    return result


def _has_ancestor(node: _HTMLNode, tag: str) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.tag == tag:
            return True
        parent = parent.parent
    return False


def _has_ignored_ancestor(node: _HTMLNode) -> bool:
    parent = node.parent
    while parent is not None:
        if _is_locally_ignored(parent):
            return True
        parent = parent.parent
    return False


def _is_ignored(node: _HTMLNode) -> bool:
    return _is_locally_ignored(node) or _has_ignored_ancestor(node)


def _is_locally_ignored(node: _HTMLNode) -> bool:
    if node.tag in _IGNORED_ELEMENTS:
        return True
    attrs = node.attrs
    if "hidden" in attrs or attrs.get("aria-hidden", "").casefold() == "true":
        return True
    role = attrs.get("role", "").casefold()
    if role in {"banner", "contentinfo", "navigation", "search"}:
        return True
    classes = set((attrs.get("class", "") + " " + attrs.get("id", "")).casefold().split())
    if classes.intersection(_UI_CLASS_TOKENS):
        return True
    style = re.sub(r"\s+", "", attrs.get("style", "").casefold())
    return "display:none" in style or "visibility:hidden" in style
