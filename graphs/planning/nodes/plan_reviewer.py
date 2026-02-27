"""
plan_reviewer — Planning Graph Node (Layer 1, Node 5)

Responsibility
--------------
Self-review the plan produced by plan_creator and decide:

  A) Approve  → sets plan_approved=False (human still needs to approve),
                sets checklist (quality assurance items for the execution
                graph to verify), and moves to awaiting_approval.

  B) Revise   → increments plan_revision_count, populates checklist with
                specific revision instructions, and routes back to
                plan_creator.

Self-review loop cap: settings.loops.plan_self_review_max (default 2).
The graph router checks self_review_count BEFORE this node runs — if the
cap is reached, the plan is accepted as-is and the node is skipped.

The checklist produced on approval is different from the revision checklist:
  - Revision checklist  → instructions for plan_creator to fix the plan
  - Approval checklist  → quality gates for the execution graph to verify
    (e.g. "each section cites at least 3 sources")

Model: Gemini 2.5 Pro

Node contract
-------------
Input  fields consumed: plan, refined_topic, depth_of_research, audience,
                        objective, source_scope, self_review_count,
                        plan_revision_count, tenant_id
Output fields written:  plan_approved (always False — human approves),
                        checklist, plan_revision_count (on revise),
                        self_review_count, status
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.config import settings
from app.llm_factory import get_llm
from graphs.planning.state import PlanningState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a senior research quality reviewer for Kadal AI.
You review AI-generated research plans before a human approves them.

Output a single JSON object — no markdown fences, no commentary.
"""

_USER_PROMPT_TEMPLATE = """\
## Research Parameters
- **Topic:** {refined_topic}
- **Depth:** {depth}
- **Audience:** {audience}
- **Objective:** {objective}
- **Sources:** {source_scope}

## Plan to Review
Title: {plan_title}
Summary: {plan_summary}

Sections ({section_count}):
{sections_text}

## Review Round
Self-review round: {self_review_count} of {max_self_review}
Previous revision count: {plan_revision_count}

---

Review the plan against these criteria:
1. **Coverage** — does the section set comprehensively cover the topic?
2. **Coherence** — are sections logically ordered with minimal overlap?
3. **Audience fit** — is the scope and depth appropriate for "{audience}"?
4. **Source alignment** — does search_strategy per section match available sources?
5. **Section count** — is the number of sections right for {depth} depth?

Respond with:
{{
  "verdict": "<approve | revise>",
  "issues": ["<specific issue 1>", "..."],
  "checklist": ["<checklist item 1>", "..."]
}}

If verdict is "approve":
  - issues: [] (empty)
  - checklist: 3-5 quality gates for the execution graph
    (e.g. "Each section must cite at least 2 unique sources")

If verdict is "revise":
  - issues: specific problems found (at least 1)
  - checklist: specific instructions for plan_creator to fix
    (each item must be actionable, not vague)
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_sections(sections: list[dict]) -> str:
    lines = []
    for i, s in enumerate(sections, 1):
        lines.append(
            f"{i}. {s.get('title', '')} — {s.get('description', '')}\n"
            f"   Strategy: {s.get('search_strategy', '')}"
        )
    return "\n".join(lines)


def _parse_response(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def plan_reviewer(state: PlanningState, config: RunnableConfig | None = None) -> dict[str, Any]:
    """LangGraph node: self-review the plan and decide approve or revise.

    Returns a partial state dict.  The graph router interprets
    plan_revision_count change to decide whether to loop back to plan_creator
    or advance to awaiting_approval.
    """
    plan = state.get("plan")
    if plan is None:
        return {
            "status": "failed",
            "error": "plan_reviewer called with no plan in state",
        }

    sections = plan.get("sections", [])
    self_review_count = state.get("self_review_count", 0)
    plan_revision_count = state.get("plan_revision_count", 0)
    max_self_review = settings.loops.plan_self_review_max

    user_prompt = _USER_PROMPT_TEMPLATE.format(
        refined_topic=state.get("refined_topic") or state.get("original_topic", ""),
        depth=state.get("depth_of_research", "intermediate"),
        audience=state.get("audience", "general audience"),
        objective=state.get("objective", ""),
        source_scope=", ".join(state.get("source_scope") or ["web"]),
        plan_title=plan.get("title", ""),
        plan_summary=plan.get("summary", ""),
        section_count=len(sections),
        sections_text=_format_sections(sections),
        self_review_count=self_review_count + 1,
        max_self_review=max_self_review,
        plan_revision_count=plan_revision_count,
    )

    llm = get_llm("plan_review", 0.2)
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    logger.info(
        "plan_reviewer: self-review round %d/%d (topic_id=%s)",
        self_review_count + 1,
        max_self_review,
        state.get("topic_id"),
    )

    response = await llm.ainvoke(messages)

    try:
        parsed = _parse_response(response.content)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("plan_reviewer: failed to parse response — %s", exc)
        return {
            "status": "failed",
            "error": f"plan_reviewer produced unparseable output: {exc}",
        }

    verdict: str = parsed.get("verdict", "approve")
    checklist: list[str] = parsed.get("checklist") or []
    issues: list[str] = parsed.get("issues") or []
    new_self_review_count = self_review_count + 1

    if verdict == "revise":
        logger.info(
            "plan_reviewer: requesting revision — %d issues found (topic_id=%s)",
            len(issues),
            state.get("topic_id"),
        )
        return {
            "checklist": checklist,
            "plan_revision_count": plan_revision_count + 1,
            "self_review_count": new_self_review_count,
            "status": "planning",   # routes back to plan_creator
        }

    # Approved (plan_approved stays False — human must still approve via API)
    logger.info(
        "plan_reviewer: approved plan with %d checklist items (topic_id=%s)",
        len(checklist),
        state.get("topic_id"),
    )
    return {
        "plan_approved": False,     # human-in-the-loop — not set here
        "checklist": checklist,
        "self_review_count": new_self_review_count,
        "status": "awaiting_approval",
    }
