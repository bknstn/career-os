"""Pure output renderers for evidence-backed Career OS reports."""

from career_os.reporting.eligibility_funnel import render_eligibility_funnel
from career_os.reporting.markdown import render_markdown_report

__all__ = ["render_eligibility_funnel", "render_markdown_report"]
