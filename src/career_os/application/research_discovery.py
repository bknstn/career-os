"""Visible-link discovery with a deliberately narrow relevance boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

from career_os.config.research_sources import ResearchSource, ResearchSourceRegistry

_SKIP_TAGS = frozenset(
    {"aside", "footer", "form", "header", "nav", "noscript", "script", "style", "svg", "template"}
)
_RELEVANCE = re.compile(
    r"\b(?:ai|artificial intelligence|labor|labour|workforce|economic|economy|"
    r"productivity|jobs?|skills?|hiring|experience|adoption|technology|talent|"
    r"state of|future of work|developer|software engineer)\b",
    re.IGNORECASE,
)
_EXCLUDED = re.compile(
    r"\b(?:login|sign[- ]?in|subscribe|contact|privacy|terms|careers|events?|podcasts?|"
    r"newsletter|press release|media kit)\b",
    re.IGNORECASE,
)
_GENERIC_LINK = re.compile(
    r"^(?:view|download|read|open)(?:\s+(?:the\s+)?(?:pdf|report|paper|publication))?$", re.I
)
MAX_VISIBLE_INDEX_LINKS = 256


@dataclass(frozen=True, slots=True)
class ResearchIndexLink:
    """One visible anchor and the nearest preceding h2/h3 context."""

    url: str
    title: str
    context: str | None


@dataclass(frozen=True, slots=True)
class ResearchDiscoveryCandidate:
    """A source-allowlisted candidate URL with its official discovery provenance."""

    canonical_url: str
    discovered_url: str
    title: str | None


class _LinkParser(HTMLParser):
    def __init__(self, base_url: str, maximum_links: int) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.maximum_links = maximum_links
        self.stack: list[str] = []
        self.skip_boundaries: list[bool] = []
        self.skip_depth = 0
        self.context: str | None = None
        self._heading_parts: list[str] | None = None
        self._heading_depth = 0
        self._anchor_href: str | None = None
        self._anchor_parts: list[str] = []
        self.links: list[ResearchIndexLink] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        hidden = (
            "hidden" in attributes
            or (attributes.get("aria-hidden") or "").lower() == "true"
            or "display:none" in ((attributes.get("style") or "").replace(" ", "").lower())
        )
        boundary = self.skip_depth == 0 and (tag in _SKIP_TAGS or hidden)
        if boundary:
            self.skip_depth += 1
        self.stack.append(tag)
        self.skip_boundaries.append(boundary)
        if self.skip_depth:
            return
        if tag in {"h2", "h3"}:
            self._heading_depth += 1
            if self._heading_depth == 1:
                self._heading_parts = []
        if tag == "a" and attributes.get("href"):
            self._anchor_href = attributes["href"]
            self._anchor_parts = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            return
        visible = self.skip_depth == 0
        if visible:
            if tag == "a" and self._anchor_href:
                title = " ".join(" ".join(self._anchor_parts).split())
                if len(self.links) < self.maximum_links:
                    self.links.append(
                        ResearchIndexLink(
                            url=urljoin(self.base_url, self._anchor_href),
                            title=title,
                            context=self.context,
                        )
                    )
                self._anchor_href = None
                self._anchor_parts = []
            if tag in {"h2", "h3"} and self._heading_depth:
                self._heading_depth -= 1
                if self._heading_depth == 0:
                    heading = " ".join(" ".join(self._heading_parts or []).split())
                    self.context = heading or self.context
                    self._heading_parts = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index] == tag:
                self.skip_depth -= sum(self.skip_boundaries[index:])
                del self.stack[index:]
                del self.skip_boundaries[index:]
                break

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self._anchor_href is not None:
            self._anchor_parts.append(data)
        if self._heading_parts is not None:
            self._heading_parts.append(data)


def parse_visible_index_links(
    html: str,
    *,
    base_url: str,
    maximum_links: int = MAX_VISIBLE_INDEX_LINKS,
) -> tuple[ResearchIndexLink, ...]:
    """Parse visible anchors only; scripts, navigation, footers, and hidden nodes are omitted."""
    if maximum_links < 1:
        raise ValueError("maximum visible link count must be positive")
    parser = _LinkParser(base_url, maximum_links)
    parser.feed(html)
    parser.close()
    return tuple(parser.links)


def relevant_candidates(
    source: ResearchSource,
    links: tuple[ResearchIndexLink, ...],
    registry: ResearchSourceRegistry,
    *,
    limit: int = 8,
    excluded_canonical_urls: frozenset[str] = frozenset(),
) -> tuple[ResearchDiscoveryCandidate, ...]:
    """Keep new visible, relevant, exactly allowlisted publication links."""
    if limit < 1:
        raise ValueError("candidate limit must be positive")
    selected: list[ResearchDiscoveryCandidate] = []
    seen: set[str] = set()
    for link in links:
        title = " ".join(link.title.split())
        context = " ".join((link.context or "").split())
        signals = f"{title} {context} {link.url}"
        folded_signals = signals.casefold()
        if any(
            not any(
                re.search(rf"\b{re.escape(term.casefold())}\b", folded_signals) for term in group
            )
            for group in source.discovery_required_term_groups
        ):
            continue
        if _EXCLUDED.search(title) or not _RELEVANCE.search(signals):
            continue
        if _GENERIC_LINK.fullmatch(title) and not _RELEVANCE.search(f"{context} {link.url}"):
            continue
        try:
            canonical = registry.canonical_document_url(source.key, link.url)
        except ValueError:
            continue
        if canonical in seen or canonical in excluded_canonical_urls:
            continue
        seen.add(canonical)
        selected.append(
            ResearchDiscoveryCandidate(
                canonical_url=canonical,
                discovered_url=link.url,
                title=title or None,
            )
        )
        if len(selected) == limit:
            break
    return tuple(selected)
