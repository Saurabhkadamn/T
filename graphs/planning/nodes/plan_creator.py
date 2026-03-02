"""
plan_creator — Planning Graph Node (Layer 1, Node 4)

Responsibility
--------------
Produce a structured ResearchPlan from the enriched query parameters.
Each plan section gets a stable UUID (section_id) that becomes the
LangGraph sub-graph thread_id suffix during execution.

The plan is written to state["plan"].  If plan_reviewer sends it back for
revision (via state["plan_revision_count"] > 0), this node receives the
reviewer's feedback in state["checklist"] and refines the plan accordingly.

Model: Gemini 2.5 Pro

Node contract
-------------
Input  fields consumed: refined_topic, depth_of_research, audience,
                        objective, domain, recency_scope, source_scope,
                        assumptions, clarification_answers,
                        clarification_questions, file_contents,
                        plan_revision_count, checklist (on revision),
                        tenant_id
Output fields written:  plan, status
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

_USER_PROMPT_TEMPLATE = """\
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

## Uploaded File Topics
{file_summary_text}

{revision_instruction}

---

Design a research plan with exactly the right number of sections for a
{depth} report.  Guidelines:
- surface (1-3 sections): overview, key findings, conclusion
- intermediate (4-6 sections): intro + thematic sections + conclusion
- in-depth (7-10 sections): comprehensive coverage with subsections possible

Each section must have a distinct research focus with minimal overlap.
The search_strategy field must be a comma-separated list from:
{source_scope_str}

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
  "estimated_sources": <integer total across all sections>
}}
"""

_REVISION_INSTRUCTION = """\
## Revision Required

The plan reviewer has returned the previous plan for revision.
Revision round: {revision_count}

**Reviewer checklist feedback:**
{checklist_text}

Revise the plan to address all checklist items above.
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
    return "\n".join(
        f"Q: {q}\nA: {a}" for q, a in zip(questions, answers)
    )


def _format_file_summary(file_contents: dict[str, str], uploaded_files: list[dict]) -> str:
    if not uploaded_files:
        return "(no uploaded files)"
    summaries = []
    for f in uploaded_files:
        oid = f["object_id"]
        content = file_contents.get(oid, "")
        snippet = content[:200].replace("\n", " ") if content else "(unavailable)"
        summaries.append(f"- {f['filename']}: {snippet}…")
    return "\n".join(summaries)


def _parse_plan(raw: str) -> ResearchPlan:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    data = json.loads(text)

    # Ensure every section has a valid UUID section_id
    sections: list[PlanSection] = []
    for s in data.get("sections", []):
        section_id = s.get("section_id", "")
        try:
            uuid.UUID(section_id)   # validate it's a real UUID
        except (ValueError, AttributeError):
            section_id = str(uuid.uuid4())

        sections.append(PlanSection(
            section_id=section_id,
            title=s.get("title", ""),
            description=s.get("description", ""),
            search_strategy=s.get("search_strategy", "web"),
        ))

    return ResearchPlan(
        title=data.get("title", "Research Report"),
        summary=data.get("summary", ""),
        sections=sections,
        estimated_sources=int(data.get("estimated_sources", len(sections) * 5)),
    )


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def plan_creator(state: PlanningState, config: RunnableConfig | None = None) -> dict[str, Any]:
    """LangGraph node: create (or revise) a structured ResearchPlan.

    On the first call plan_revision_count is 0 and the node creates a new plan.
    On subsequent calls (sent back by plan_reviewer) it refines the existing plan
    using the reviewer's checklist.
    """
    depth = state.get("depth_of_research", "intermediate")
    max_sections, max_sources = _DEPTH_GUIDANCE.get(depth, (6, 5))
    source_scope = state.get("source_scope") or ["web"]
    revision_count = state.get("plan_revision_count", 0)
    checklist = state.get("checklist") or []

    revision_instruction = ""
    if revision_count > 0 and checklist:
        revision_instruction = _REVISION_INSTRUCTION.format(
            revision_count=revision_count,
            checklist_text="\n".join(f"- {item}" for item in checklist),
        )

    user_prompt = _USER_PROMPT_TEMPLATE.format(
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
        file_summary_text=_format_file_summary(
            state.get("file_contents") or {},
            state.get("uploaded_files") or [],
        ),
        revision_instruction=revision_instruction,
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
        plan = _parse_plan(response.content)
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        logger.error("plan_creator: failed to parse plan — %s", exc)
        return {
            "status": "failed",
            "error": f"plan_creator produced unparseable output: {exc}",
        }

    logger.info(
        "plan_creator: created plan with %d sections (topic_id=%s)",
        len(plan["sections"]),
        state.get("topic_id"),
    )

    return {
        "plan": plan,
        "status": "reviewing",
    }
