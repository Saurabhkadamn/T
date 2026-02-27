"""
report_reviewer — Execution Graph Node (Main pipeline, Node 4)

Responsibility
--------------
Review the generated HTML report against the quality checklist.
Either approve the report or request one targeted revision.

Verdicts:
  "approved"       → route to exporter (no revision needed)
  "needs_revision" → route back to report_writer (if revision_count < max)
  "approved"       → force-approve if revision_count >= max (avoid infinite loop)

The routing function (should_revise) is defined in this module and used as
a conditional edge in graph.py.  The revision cap is enforced there, NOT
inside this node.

Model: Gemini 2.5 Pro (reasoning task — quality review)

Loop cap: settings.loops.report_revision_max (default 1 — enforced in graph.py)

Node contract
-------------
Input  fields consumed: report_html, checklist, topic, revised_topic,
                        revision_count
Output fields written:  review_feedback (str or None), revision_count (+1),
                        status="exporting" or "writing"
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.config import settings
from app.llm_factory import get_llm
from graphs.execution.state import ExecutionState

logger = logging.getLogger(__name__)

# Maximum characters of the report to include in the review prompt.
# Avoids exceeding context window for very long reports.
_MAX_REPORT_CHARS = 40_000


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a quality control reviewer for AI-generated research reports.
Evaluate the report against the provided checklist and return a structured verdict.
Output a single JSON object — no markdown fences, no commentary.
"""

_USER_TEMPLATE = """\
## Research Topic
{topic}

## Quality Checklist
{checklist}

## Report to Review
{report_preview}

---

Evaluate whether the report satisfies ALL checklist items.

Return:
{{
  "verdict": "approved" | "needs_revision",
  "feedback": null | "<specific, actionable feedback — list what needs fixing>"
}}

Rules:
- "approved" only if ALL checklist items are met.
- "needs_revision" must include concrete feedback (not vague comments).
- Feedback must be specific enough for a writer to action it.
- Do NOT request structural changes that cannot be made in one revision pass.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_checklist(checklist: list[str]) -> str:
    if not checklist:
        return "(no checklist — approve if report covers the topic adequately)"
    return "\n".join(f"- {item}" for item in checklist)


def _parse_verdict(raw: str) -> tuple[str, str | None]:
    """Return (verdict, feedback).  Defaults to approved on parse error."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(text)
        verdict = str(parsed.get("verdict", "approved")).lower()
        feedback = parsed.get("feedback") or None
        if verdict not in ("approved", "needs_revision"):
            verdict = "approved"
        return verdict, feedback
    except Exception as exc:
        logger.error("report_reviewer: failed to parse verdict — %s", exc)
        return "approved", None


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def report_reviewer(
    state: ExecutionState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """LangGraph node: review the HTML report against the quality checklist."""
    job_id: str = state.get("job_id", "")
    topic: str = state.get("refined_topic") or state.get("topic", "")
    checklist: list[str] = state.get("checklist") or []
    report_html: str | None = state.get("report_html")
    revision_count: int = state.get("revision_count", 0)

    logger.info(
        "report_reviewer: reviewing report (revision_count=%d, job_id=%s)",
        revision_count,
        job_id,
    )

    if not report_html:
        logger.error(
            "report_reviewer: no report_html to review (job_id=%s)", job_id
        )
        return {
            "review_feedback": "Report is empty — cannot review.",
            "revision_count": revision_count + 1,
            "status": "writing",
        }

    # Truncate for the review prompt if the report is very long.
    report_preview = report_html[:_MAX_REPORT_CHARS]
    if len(report_html) > _MAX_REPORT_CHARS:
        report_preview += "\n…[report truncated for review]"

    user_prompt = _USER_TEMPLATE.format(
        topic=topic,
        checklist=_format_checklist(checklist),
        report_preview=report_preview,
    )

    llm = get_llm("report_review", 0.2)
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    try:
        response = await llm.ainvoke(messages)
        verdict, feedback = _parse_verdict(response.content)
    except Exception as exc:
        logger.error(
            "report_reviewer: LLM call failed — %s (job_id=%s)", exc, job_id
        )
        # Non-fatal: approve on error to avoid blocking the pipeline.
        verdict, feedback = "approved", None

    logger.info(
        "report_reviewer: verdict=%r (job_id=%s)", verdict, job_id
    )

    next_status = "exporting" if verdict == "approved" else "writing"

    return {
        "review_feedback": feedback,
        "revision_count": revision_count + 1,
        "status": next_status,
    }


# ---------------------------------------------------------------------------
# Routing function (used as conditional edge in graph.py)
# ---------------------------------------------------------------------------

def should_revise(state: ExecutionState) -> str:
    """Conditional edge: route to report_writer for revision or exporter.

    Checks:
      1. Was the verdict "approved" (review_feedback is None)?
      2. Has the revision cap been reached?

    Returns:
      "report_writer" if revision needed and cap not reached.
      "exporter"      otherwise.
    """
    review_feedback: str | None = state.get("review_feedback")
    revision_count: int = state.get("revision_count", 0)
    max_revisions: int = settings.loops.report_revision_max

    if review_feedback and revision_count <= max_revisions:
        logger.info(
            "should_revise: sending back for revision (revision=%d/%d)",
            revision_count,
            max_revisions,
        )
        return "report_writer"

    logger.info(
        "should_revise: routing to exporter (revision=%d, feedback=%s)",
        revision_count,
        review_feedback is not None,
    )
    return "exporter"
