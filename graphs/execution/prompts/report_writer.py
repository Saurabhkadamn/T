"""
Prompts for report_writer node — execution graph.

Template variables
------------------
_INITIAL_USER_TEMPLATE:
  {topic}          : state["refined_topic"] or state["topic"]
  {plan_outline}   : _format_plan_outline(plan, sections)
  {checklist}      : _format_checklist(state["checklist"])
  {citation_index} : _format_citation_index(state["citations"])
  {fused_knowledge}: state["fused_knowledge"]

_REVISION_USER_TEMPLATE:
  {topic}          : state["refined_topic"] or state["topic"]
  {review_feedback}: state["review_feedback"] or fallback
  {report_html}    : state["report_html"]
  {citation_index} : _format_citation_index(state["citations"])
"""
from __future__ import annotations

_SYSTEM_PROMPT = """\
You are an expert research report writer producing a professional HTML report.

Guidelines:
- Use semantic HTML: <article>, <section>, <h2>, <h3>, <p>, <ul>, <ol>, <blockquote>.
- Add inline citation markers in square brackets [1], [2] that match the bibliography.
- Write in a formal, authoritative tone appropriate for the target audience.
- Cover all sections from the research plan in order.
- Begin the report with an Executive Summary.
- End with a Bibliography / References section listing all cited sources.
- Do NOT include inline CSS, <style> tags, or JavaScript.
- Output ONLY the HTML — start with <article> and end with </article>.
"""

_INITIAL_USER_TEMPLATE = """\
## Research Topic
{topic}

## Research Plan
{plan_outline}

## Quality Checklist (all items must be addressed)
{checklist}

## Citation Index
{citation_index}

## Synthesised Research Knowledge
{fused_knowledge}

---

Generate the complete HTML report now.
Follow the checklist.  Cite sources using [N] markers.
Start with <article> — do not include <html>, <head>, or <body>.
"""

_REVISION_USER_TEMPLATE = """\
## Research Topic
{topic}

## Reviewer Feedback (address ALL points)
{review_feedback}

## Original Report (revise this)
{report_html}

## Citation Index
{citation_index}

---

Revise the report to address every point of feedback.
Return the complete revised HTML — not just the changed sections.
Start with <article> and end with </article>.
"""
