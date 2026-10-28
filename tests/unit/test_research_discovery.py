"""Visible-link filtering for research index pages."""

from pathlib import Path

from career_os.application.research_discovery import (
    parse_visible_index_links,
    relevant_candidates,
)
from career_os.config.research_sources import ResearchSourceRegistry


def test_generic_pdf_link_uses_ai_heading_but_navigation_links_are_ignored() -> None:
    registry = ResearchSourceRegistry.load(
        Path(__file__).resolve().parents[2] / "config/radar/research.sources.v1.yaml"
    )
    source = registry.get("linkedin_economic_graph")
    script_url = "https://delivery-p143253-e1476319.adobeaemcloud.com/script.pdf"
    html = f"""
    <nav><h2>AI Talent</h2><a href="https://example.invalid/nav.pdf">View PDF</a></nav>
    <main>
      <h2>AI Talent Divide 2026</h2>
      <a href="https://delivery-p143253-e1476319.adobeaemcloud.com/report.pdf">View PDF</a>
    </main>
    <script><a href="{script_url}">AI report</a></script>
    """

    links = parse_visible_index_links(
        html, base_url="https://economicgraph.linkedin.com/workforce-data/publications"
    )
    candidates = relevant_candidates(source, links, registry)

    assert len(links) == 1
    assert links[0].title == "View PDF"
    assert links[0].context == "AI Talent Divide 2026"
    assert len(candidates) == 1
    assert candidates[0].discovered_url.endswith("report.pdf")


def test_relevant_links_are_exact_host_allowlisted_and_aliases_deduplicated() -> None:
    registry = ResearchSourceRegistry.load(
        Path(__file__).resolve().parents[2] / "config/radar/research.sources.v1.yaml"
    )
    source = registry.get("linkedin_economic_graph")
    report_url = "https://economicgraph.linkedin.com/research/labor-market-report-2026"
    pdf_url = (
        "https://delivery-p143253-e1476319.adobeaemcloud.com/adobe/assets/"
        "urn:aaid:aem:cef0613f-8120-4328-a651-eea88832fcf0/original/as/"
        "linkedIn-labor-market-report-building-a-future-of-work-that-works-jan-2026.pdf"
    )
    links = parse_visible_index_links(
        f"""
        <main>
          <h2>Labor Market Report 2026</h2>
          <a href="{report_url}">Report PDF</a>
          <a href="{pdf_url}">Labor Market Report PDF</a>
          <a href="https://unknown.example/ai-labor-report.pdf">AI labor report</a>
        </main>
        """,
        base_url="https://economicgraph.linkedin.com/workforce-data/publications",
    )

    candidates = relevant_candidates(source, links, registry)

    assert len(candidates) == 1
    assert candidates[0].canonical_url == source.candidate_document_urls[0]


def test_discovery_fills_limit_after_known_urls_and_caps_parsed_links() -> None:
    registry = ResearchSourceRegistry.load(
        Path(__file__).resolve().parents[2] / "config/radar/research.sources.v1.yaml"
    )
    source = registry.get("linkedin_economic_graph")
    links_html = "".join(
        f'<a href="https://economicgraph.linkedin.com/research/item-{index}">'
        f"AI labor report {index}</a>"
        for index in range(1, 10)
    )
    links = parse_visible_index_links(
        f"<main>{links_html}</main>",
        base_url="https://economicgraph.linkedin.com/workforce-data/publications",
    )
    known = frozenset(
        f"https://economicgraph.linkedin.com/research/item-{index}" for index in range(1, 9)
    )

    candidates = relevant_candidates(
        source,
        links,
        registry,
        limit=8,
        excluded_canonical_urls=known,
    )

    assert len(links) == 9
    assert [candidate.canonical_url for candidate in candidates] == [
        "https://economicgraph.linkedin.com/research/item-9"
    ]


def test_amazon_index_requires_ai_and_software_role_context() -> None:
    registry = ResearchSourceRegistry.load(
        Path(__file__).resolve().parents[2] / "config/radar/research.sources.v1.yaml"
    )
    source = registry.get("amazon_engineering_guidance")
    links = parse_visible_index_links(
        """
        <main>
          <a href="/news/company-news/ai-warehouse-hiring">AI warehouse hiring</a>
          <a href="/news/company-news/ai-data-center-investment">AI data-center investment</a>
          <a href="/news/company-news/software-engineer-hiring">Software engineer hiring</a>
          <a href="/news/company-news/new-guidance">
            AI guidance for software engineers and developers
          </a>
        </main>
        """,
        base_url="https://www.aboutamazon.com/news/company-news",
    )

    candidates = relevant_candidates(source, links, registry)

    assert [candidate.canonical_url for candidate in candidates] == [
        "https://www.aboutamazon.com/news/company-news/new-guidance"
    ]
