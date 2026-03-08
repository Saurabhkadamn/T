"""
report_writer — Execution Graph Node (Main pipeline, Node 3)

Responsibility
--------------
Generate a complete, professional HTML research report from the fused
knowledge base.  Called on the first write and on revision (when
report_reviewer sends it back with feedback).

On initial write (revision_count == 0):
  Uses fused_knowledge as the primary input with the research plan for structure.

On revision (revision_count == 1):
  Incorporates review_feedback to improve specific sections of report_html.

HTML output structure:
  - Executive summary (first)
  - One <section> per plan section, in plan order
  - In-text citation markers [1], [2], … matching bibliography entries
  - Bibliography / References section (last)
  - Semantic HTML: <article>, <h2>, <h3>, <p>, <ul>, <ol>
  - No inline CSS — keep HTML clean for downstream rendering

Model: Gemini 2.5 Pro (reasoning task — report generation)
  Streaming is handled at the WebSocket/worker layer; this node returns
  the complete HTML string.

Loop cap: settings.loops.report_revision_max (enforced by report_reviewer routing)

Node contract
-------------
Input  fields consumed: fused_knowledge, plan, checklist, citations,
                        topic, refined_topic, report_html (on revision),
                        review_feedback (on revision), revision_count
Output fields written:  report_html, status="reviewing"
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.config import settings
from app.llm_factory import get_llm
from app.tracing import node_span
from graphs.execution.state import Citation, ExecutionState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_plan_outline(plan: dict, sections: list[dict]) -> str:
    """Build a numbered outline from the plan sections."""
    title = plan.get("title", "") if plan else ""
    lines: list[str] = []
    if title:
        lines.append(f"**Title:** {title}")
    for i, s in enumerate(sections, 1):
        lines.append(
            f"{i}. **{s.get('title', '')}** — {s.get('description', '')}"
        )
    return "\n".join(lines) or "(no plan outline)"


def _format_checklist(checklist: list[str]) -> str:
    if not checklist:
        return "(no checklist provided)"
    return "\n".join(f"- {item}" for item in checklist)


def _format_citation_index(citations: list[Citation]) -> str:
    """Build a numbered citation list: [1] Title — URL — Author — Date."""
    if not citations:
        return "(no citations)"

    # Deduplicate by URL, preserving first occurrence.
    seen: set[str] = set()
    unique: list[Citation] = []
    for c in sorted(citations, key=lambda x: x["trust_tier"]):
        key = c["url"] or c["title"]
        if key not in seen:
            seen.add(key)
            unique.append(c)

    lines: list[str] = []
    for i, c in enumerate(unique, 1):
        parts = [f"[{i}]", c["title"] or "(untitled)"]
        if c["url"]:
            parts.append(c["url"])
        if c["authors"]:
            parts.append(c["authors"])
        if c["published_at"]:
            parts.append(c["published_at"])
        lines.append(" — ".join(parts))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

@node_span("report_writer")
async def report_writer(
    state: ExecutionState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """LangGraph node: generate or revise the HTML research report.

    First call (revision_count == 0): full generation from fused_knowledge.
    Subsequent calls (revision_count > 0): targeted revision from feedback.
    """
    job_id: str = state.get("job_id", "")
    topic: str = state.get("refined_topic") or state.get("topic", "")
    plan: dict = state.get("plan") or {}
    sections: list[dict] = state.get("sections") or []
    checklist: list[str] = state.get("checklist") or []
    citations: list[Citation] = state.get("citations") or []
    fused_knowledge: str | None = state.get("fused_knowledge")
    revision_count: int = state.get("revision_count", 0)
    review_feedback: str | None = state.get("review_feedback")
    existing_html: str | None = state.get("report_html")

    citation_index = _format_citation_index(citations)

    logger.info(
        "report_writer: generating report (revision=%d, job_id=%s)",
        revision_count,
        job_id,
    )

    if revision_count == 0 or not existing_html:
        # Initial report generation.
        if not fused_knowledge:
            logger.error(
                "report_writer: fused_knowledge is None — cannot generate report "
                "(job_id=%s)",
                job_id,
            )
            return {
                "report_html": None,
                "status": "failed",
                "error": "report_writer: fused_knowledge missing",
            }
        user_prompt = _INITIAL_USER_TEMPLATE.format(
            topic=topic,
            plan_outline=_format_plan_outline(plan, sections),
            checklist=_format_checklist(checklist),
            citation_index=citation_index,
            fused_knowledge=fused_knowledge,
        )
    else:
        # Revision pass — incorporate reviewer feedback.
        user_prompt = _REVISION_USER_TEMPLATE.format(
            topic=topic,
            review_feedback=review_feedback or "(no specific feedback)",
            report_html=existing_html,
            citation_index=citation_index,
        )

    llm = get_llm("report_writing", 0.4)
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    try:
        response = await asyncio.wait_for(
            llm.ainvoke(messages, config=config), timeout=settings.llm_timeout_seconds
        )
        report_html: str = response.content.strip()
    except asyncio.TimeoutError:
        logger.error(
            "report_writer: LLM timed out after %ds (job_id=%s)",
            settings.llm_timeout_seconds, job_id,
        )
        return {
            "report_html": state.get("report_html"),
            "status": "partial_success",
            "error": f"report_writer: LLM timed out after {settings.llm_timeout_seconds}s",
        }
    except Exception as exc:
        logger.error(
            "report_writer: LLM call failed — %s (job_id=%s)", exc, job_id
        )
        # Return partial_success so the graph routes to exporter and persists
        # whatever HTML already exists, rather than terminating with status=failed.
        return {
            "report_html": state.get("report_html"),   # preserve any existing HTML
            "status": "partial_success",
            "error": f"report_writer: LLM error — {exc}",
        }

    logger.info(
        "report_writer: generated %d chars of HTML (revision=%d, job_id=%s)",
        len(report_html),
        revision_count,
        job_id,
    )

    return {
        "report_html": report_html,
        "status": "reviewing",
    }
