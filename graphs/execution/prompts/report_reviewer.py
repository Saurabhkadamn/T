"""
Prompts for report_reviewer node — execution graph.

Template variables
------------------
_USER_TEMPLATE:
  {topic}          : state["refined_topic"] or state["topic"]
  {checklist}      : _format_checklist(state["checklist"])
  {report_preview} : report_html[:_REVIEW_PROMPT_SAFETY_CHARS] (truncated in node)
"""
from __future__ import annotations

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
