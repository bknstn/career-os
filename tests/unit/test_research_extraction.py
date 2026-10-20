"""Deterministic, in-memory coverage for research document extraction."""

from __future__ import annotations

import builtins
import socket
from datetime import date
from io import BytesIO

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from career_os.domain.research import (
    ResearchDateState,
    ResearchMediaType,
    ResearchTextLocation,
)
from career_os.research.extraction import (
    EXTRACTION_VERSION,
    extract_research_document,
)


def test_html_prefers_article_and_creates_heading_scoped_offsets() -> None:
    body = b"""<!doctype html>
    <html><head><title>Developer Experience Outlook</title>
    <meta property="article:published_time" content="2026-04-22T09:30:00Z">
    <meta name="dateModified" content="2099-12-31">
    <script type="application/ld+json">{
      "@type":"Article","headline":"Developer Experience Outlook",
      "datePublished":"2026-04-22","dateModified":"2099-12-31"
    }</script></head><body>
    <nav><a>Navigation noise should never appear in the report body.</a></nav>
    <header>Site header noise with many extra words should be ignored.</header>
    <main><article>
      <header><h1>Developer Experience Outlook</h1></header>
      <p>This report studies software tasks, developer experience, and production AI use.</p>
      <h2>Results</h2>
      <p>Observed task changes affect experienced engineers across several production teams.</p>
      <h2>Limitations</h2>
      <p>The measured sample is limited and does not establish broad employment effects.</p>
          <p>The cohort was defined using working developers and analyzed tasks at multiple
          levels of experience.</p>
    </article></main>
    <script>document.body.innerText = 'This injected text is not report evidence';</script>
    <footer>Footer boilerplate must also be removed.</footer>
    </body></html>"""

    result = extract_research_document(body, ResearchMediaType.HTML)

    assert result.error_code is None
    assert result.extraction_version == EXTRACTION_VERSION
    assert result.title == "Developer Experience Outlook"
    assert result.publication_date.value == date(2026, 4, 22)
    assert result.publication_date.state is ResearchDateState.REPORTED
    assert result.normalized_text is not None
    text = result.normalized_text
    assert "Observed task changes affect experienced engineers" in text
    assert text.startswith("Developer Experience Outlook")
    assert "Navigation noise" not in text
    assert "Site header noise" not in text
    assert "Footer boilerplate" not in text
    assert "injected text" not in text

    quote = "Observed task changes affect experienced engineers"
    start = text.index(quote)
    result_location = ResearchTextLocation(
        ResearchMediaType.HTML,
        start,
        start + len(quote),
        section="Results",
    )
    wrong_section_location = ResearchTextLocation(
        ResearchMediaType.HTML,
        start,
        start + len(quote),
        section="Limitations",
    )
    results_region = next(region for region in result.regions if region.section == "Results")
    assert results_region.contains(result_location)
    assert not any(region.contains(wrong_section_location) for region in result.regions)


def test_html_jsonld_publication_date_matches_current_article_and_ignores_modified() -> None:
    title = "AI Skills in Production"
    body = f"""<html><head><title>{title}</title>
    <script type="application/ld+json">{{"@graph":[
      {{"@type":"Article","headline":"Unrelated sidebar story",
       "datePublished":"2001-01-01"}},
      {{"@type":"https://schema.org/Report","headline":"{title}",
       "datePublished":"2025-11-03","dateModified":"2098-12-31"}}
    ]}}</script></head><body><main><h1>{title}</h1>
    <p>This report explains how skilled workers use AI in production workflows and what tasks are
    affected.</p>
    <p>It also describes a sample, the evaluation method, and important limitations for interpreting
    the results.</p>
    <p>The evidence remains specific to the measured occupations, platform, and period.</p>
    </main></body></html>""".encode()

    result = extract_research_document(body, ResearchMediaType.HTML)

    assert result.error_code is None
    assert result.publication_date.value == date(2025, 11, 3)
    assert result.publication_date.state is ResearchDateState.REPORTED


def test_html_decodes_response_charset_entities_unicode_and_collapses_whitespace() -> None:
    source = """<html><head><meta charset="windows-1252"><title>Caf&eacute; &amp; AI</title></head>
    <body><main><h1>Caf&eacute; &amp; AI</h1><p>
    Workers\u2019 coöperation — measured with AI &amp; software skills — varies across roles.
    </p><p>Additional observations describe tasks, seniority, production use, methods, and limits.
    </p><p>These results concern observed work patterns and do not establish employment effects.</p>
    <p>Additional notes connect technical skills with work activities across teams, tools, and
    occupations.</p></main></body></html>"""

    result = extract_research_document(source.encode("cp1252"), ResearchMediaType.HTML)

    assert result.error_code is None
    assert result.title == "Café & AI"
    assert result.normalized_text is not None
    expected = (
        "Workers\u2019 coöperation — measured with AI & software skills — varies across roles."
    )
    assert expected in result.normalized_text
    assert "  " not in result.normalized_text
    assert result.publication_date.state is ResearchDateState.NOT_REPORTED


def test_pdf_pages_keep_real_one_based_page_ranges_and_reject_wrong_page() -> None:
    body = _text_pdf(["First page contains one finding.", "Second page contains another finding."])

    result = extract_research_document(body, ResearchMediaType.PDF)

    assert result.error_code is None
    assert result.publication_date.state is ResearchDateState.NOT_REPORTED
    assert result.normalized_text == (
        "First page contains one finding.\n\nSecond page contains another finding."
    )
    assert [region.page_number for region in result.regions] == [1, 2]
    assert result.normalized_text is not None
    quote = "Second page contains another finding."
    start = result.normalized_text.index(quote)
    location = ResearchTextLocation(
        ResearchMediaType.PDF,
        start,
        start + len(quote),
        page_number=2,
    )
    wrong_page = ResearchTextLocation(
        ResearchMediaType.PDF,
        start,
        start + len(quote),
        page_number=1,
    )
    assert result.regions[1].contains(location)
    assert not any(region.contains(wrong_page) for region in result.regions)


def test_empty_malformed_encrypted_and_textless_pdfs_fail_explicitly() -> None:
    assert extract_research_document(b"", ResearchMediaType.PDF).error_code == "empty_body"
    assert (
        extract_research_document(b"not a PDF document", ResearchMediaType.PDF).error_code
        == "malformed_pdf"
    )
    encrypted = _text_pdf(["Hidden text."], password="secret")
    assert extract_research_document(encrypted, ResearchMediaType.PDF).error_code == "encrypted_pdf"
    assert (
        extract_research_document(_blank_pdf(), ResearchMediaType.PDF).error_code
        == "pdf_no_extractable_text"
    )


def test_html_title_and_short_summary_alone_are_not_report_body() -> None:
    body = b"""<html><head><title>Short report</title></head><body><main><article>
    <h1>Short report</h1><p>This brief teaser describes AI and work in a few words.</p>
    </article></main></body></html>"""

    result = extract_research_document(body, ResearchMediaType.HTML)

    assert result.title == "Short report"
    assert result.normalized_text is None
    assert result.regions == ()
    assert result.error_code == "html_content_too_short"


def test_deeply_nested_html_still_extracts_body_text() -> None:
    nested_start = "<div>" * 100
    nested_end = "</div>" * 100
    body = (
        "<main>"
        + nested_start
        + "<article><h1>Deep layout report</h1><p>"
        + (
            "This report includes a meaningful body with findings, methods, sample context, "
            "and limits. Its text stays available even when publisher markup wraps it in many "
            "nested containers. The extractor preserves publication content and citation "
            "offsets without recursive ancestry checks."
        )
        + "</p></article>"
        + nested_end
        + "</main>"
    ).encode()

    result = extract_research_document(body, ResearchMediaType.HTML)

    assert result.error_code is None
    assert result.normalized_text is not None
    assert "findings, methods, sample context, and limits" in result.normalized_text


def test_html_and_pdf_extraction_do_not_open_files_or_network_connections(monkeypatch) -> None:
    html_body = (
        b"<main><h1>Offline report</h1><p>All extraction inputs are provided directly as bytes."
        b" This report includes methods, findings, and limitations across multiple"
        b" observations.</p>"
        b"<p>This body has enough words to produce a normalized report text section and"
        b" preserve its content without external connections or filesystem access.</p></main>"
    )
    pdf_body = _text_pdf(
        ["An in-memory PDF page includes a finding and sufficient extractable words."]
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("extractor attempted external I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("urllib.request.urlopen", forbidden)

    assert extract_research_document(html_body, ResearchMediaType.HTML).error_code is None
    assert extract_research_document(pdf_body, ResearchMediaType.PDF).error_code is None


def _text_pdf(page_texts: list[str], password: str | None = None) -> bytes:
    writer = PdfWriter()
    for value in page_texts:
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
        font_ref = writer._add_object(font)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
            }
        )
        escaped = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 200 Td ({escaped}) Tj ET".encode("cp1252"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if password is not None:
        writer.encrypt(password)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
