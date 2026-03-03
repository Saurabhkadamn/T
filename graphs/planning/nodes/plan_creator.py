"""
plan_creator — Planning Graph Node (Layer 1, Node 3)

Responsibility
--------------
Produce a structured ResearchPlan from the enriched query parameters.
Each plan section gets a stable UUID (section_id) that becomes the
LangGraph sub-graph thread_id suffix during execution.

Revision mode
-------------
When plan_revision_count > 0, the node receives the human reviewer's
feedback in state["plan_feedback"] and makes targeted changes to the
existing plan.  It does NOT regenerate the plan from scratch.

A self-generated checklist (3-5 quality gates) is always returned so the
human reviewer has criteria to evaluate the plan against.

Model: Gemini 2.5 Pro  (configured via settings.models "plan_creation")

Node contract
-------------
Input  fields consumed: context_brief, refined_topic, depth_of_research,
                        audience, objective, domain, recency_scope,
                        source_scope, assumptions, clarification_answers,
                        clarification_questions, file_contents,
                        plan_revision_count, plan_feedback (on revision),
                        plan (on revision), tenant_id
Output fields written:  plan, checklist, status="awaiting_approval"
                        On error: status="failed", error=<message>
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.config import settings
from app.llm_factory import get_llm
from graphs.planning.state import PlanningState, ResearchPlan, PlanSection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a senior research planner for Kadal AI's deep research system.
Your job is to design a structured research plan that an AI research pipeline
will execute to produce a comprehensive HTML report.

Output a single JSON object — no markdown fences, no commentary.
"""

_FRESH_PLAN_PROMPT = """\
## Context Brief
{context_brief}

## Research Parameters

- **Topic:** {refined_topic}
- **Domain:** {domain}
- **Audience:** {audience}
- **Objective:** {objective}
- **Depth:** {depth} ({max_sections} sections, {max_sources} sources/section)
- **Recency:** {recency_scope}
- **Sources available:** {source_scope}

## Assumptions Made
{assumptions_text}

## Clarifications From User
{clarification_text}

---

Design a research plan with the right number of sections for a {depth} report.
Guidelines:
- surface (1-3 sections): overview, key findings, conclusion
- intermediate (4-6 sections): intro + thematic sections + conclusion
- in-depth (7-10 sections): comprehensive coverage with subsections possible

Each section must have a distinct research focus with minimal overlap.
The search_strategy field must be a comma-separated list from: {source_scope_str}

Also produce a checklist of 3-5 quality standards the research execution must meet
(e.g. "Each section must cite at least 2 unique sources",
"Include quantitative data or metrics where available",
"Prioritise peer-reviewed sources for scientific claims").
These will be enforced during the AI research execution phase.

Return this JSON:
{{
  "title": "<report title>",
  "summary": "<2-4 sentence executive summary of the planned report>",
  "sections": [
    {{
      "section_id": "<generate a UUID v4>",
      "title": "<section heading>",
      "description": "<1-3 sentences on what this section covers>",
      "search_strategy": "<comma-separated source types>"
    }}
  ],
  "estimated_sources": <integer total across all sections>,
  "checklist": ["<gate 1>", "<gate 2>", "<gate 3>"]
}}
"""

_REVISION_PROMPT = """\
## Context Brief
{context_brief}

## Current Plan (to be revised)
{current_plan_json}

## Human Reviewer Feedback (revision round {revision_count})
{plan_feedback}

---

Revise the plan above based on the reviewer's feedback.
Make only the changes needed to address the feedback — do NOT rewrite the
entire plan unless the feedback explicitly requires it.

Preserve section_ids for sections that are not changed.
Generate new UUID v4 section_ids only for newly added sections.

Also produce an updated checklist of 3-5 quality standards the research execution must meet
(same standard as the original: source counts, data quality, recency, verifiability).
These will be enforced during the AI research execution phase.

Return the same JSON structure:
{{
  "title": "<report title>",
  "summary": "<2-4 sentence executive summary>",
  "sections": [
    {{
      "section_id": "<existing or new UUID v4>",
      "title": "<section heading>",
      "description": "<1-3 sentences>",
      "search_strategy": "<comma-separated source types>"
    }}
  ],
  "estimated_sources": <integer>,
  "checklist": ["<gate 1>", "<gate 2>", "<gate 3>"]
}}
"""


# ---------------------------------------------------------------------------
# Depth → section count guidance
# ---------------------------------------------------------------------------

_DEPTH_GUIDANCE = {
    "surface": (3, 3),        # (max_sections, max_sources)
    "intermediate": (6, 5),
    "in-depth": (10, 10),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_clarifications(questions: list[str], answers: list[str]) -> str:
    if not answers:
        return "(none)"
    return "\n".join(f"Q: {q}\nA: {a}" for q, a in zip(questions, answers))


def _parse_plan_response(raw: str) -> tuple[ResearchPlan, list[str]]:
    """Parse LLM JSON → (ResearchPlan, checklist)."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    data = json.loads(text)

    sections: list[PlanSection] = []
    for s in data.get("sections", []):
        section_id = s.get("section_id", "")
        try:
            uuid.UUID(section_id)
        except (ValueError, AttributeError):
            section_id = str(uuid.uuid4())

        sections.append(PlanSection(
            section_id=section_id,
            title=s.get("title", ""),
            description=s.get("description", ""),
            search_strategy=s.get("search_strategy", "web"),
        ))

    plan = ResearchPlan(
        title=data.get("title", "Research Report"),
        summary=data.get("summary", ""),
        sections=sections,
        estimated_sources=int(data.get("estimated_sources", len(sections) * 5)),
    )

    checklist: list[str] = [str(item) for item in (data.get("checklist") or [])]
    return plan, checklist


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def plan_creator(
    state: PlanningState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """LangGraph node: create (or revise) a structured ResearchPlan.

    On revision_count=0 → fresh plan from context_brief + research params.
    On revision_count>0 → targeted revision using human plan_feedback.
    """
    revision_count = state.get("plan_revision_count", 0)
    depth = state.get("depth_of_research", "intermediate")
    max_sections, max_sources = _DEPTH_GUIDANCE.get(depth, (6, 5))
    source_scope = state.get("source_scope") or ["web"]

    if revision_count == 0:
        # ── Fresh plan ────────────────────────────────────────────────────
        user_prompt = _FRESH_PLAN_PROMPT.format(
            context_brief=state.get("context_brief") or "(no context brief)",
            refined_topic=state.get("refined_topic") or state.get("original_topic", ""),
            domain=state.get("domain", ""),
            audience=state.get("audience", "general audience"),
            objective=state.get("objective", "understand the topic"),
            depth=depth,
            max_sections=max_sections,
            max_sources=max_sources,
            recency_scope=state.get("recency_scope", "last_2_years"),
            source_scope=", ".join(source_scope),
            source_scope_str=", ".join(source_scope),
            assumptions_text="\n".join(
                f"- {a}" for a in (state.get("assumptions") or [])
            ) or "(none)",
            clarification_text=_format_clarifications(
                state.get("clarification_questions") or [],
                state.get("clarification_answers") or [],
            ),
        )
    else:
        # ── Revision using human feedback ─────────────────────────────────
        plan_feedback = state.get("plan_feedback") or "(no feedback provided)"
        current_plan = state.get("plan")
        current_plan_json = json.dumps(current_plan, indent=2) if current_plan else "{}"

        user_prompt = _REVISION_PROMPT.format(
            context_brief=state.get("context_brief") or "(no context brief)",
            current_plan_json=current_plan_json,
            revision_count=revision_count,
            plan_feedback=plan_feedback,
        )

    llm = get_llm("plan_creation", 0.4)
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    logger.info(
        "plan_creator: invoking %s (revision=%d, topic_id=%s)",
        settings.models.model_for("plan_creation"),
        revision_count,
        state.get("topic_id"),
    )

    try:
        response = await asyncio.wait_for(
            llm.ainvoke(messages), timeout=settings.llm_timeout_seconds
        )
    except asyncio.TimeoutError:
        logger.error("plan_creator: LLM timed out after %ds", settings.llm_timeout_seconds)
        return {"status": "failed", "error": "plan_creator: LLM call timed out"}
    except Exception as exc:
        logger.error("plan_creator: LLM call failed — %s", exc)
        return {"status": "failed", "error": f"plan_creator: LLM call failed — {exc}"}

    try:
        plan, checklist = _parse_plan_response(response.content)
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        logger.error("plan_creator: failed to parse plan — %s", exc)
        return {
            "status": "failed",
            "error": f"plan_creator produced unparseable output: {exc}",
        }

    logger.info(
        "plan_creator: created plan with %d sections, %d checklist items (topic_id=%s)",
        len(plan["sections"]),
        len(checklist),
        state.get("topic_id"),
    )

    return {
        "plan": plan,
        "checklist": checklist,
        "status": "awaiting_approval",
    }
