"""
query_analyzer — Planning Graph Node (Layer 1, Node 2)

Responsibility
--------------
Absorbs the former clarifier node.  Reads ``context_brief`` (LLM-generated
summary from context_builder) and decides in a single LLM call whether to:

  a) Ask clarifying questions (needs_clarification=True), or
  b) Extract all research parameters and proceed to planning.

This replaces the old two-step flow (query_analyzer → clarifier) with a
single node that handles both clarification and parameter extraction.

Clarification decision rules
-----------------------------
Set needs_clarification=True when ANY of:
  - Topic < 4 words AND context_brief doesn't resolve the ambiguity
  - Topic is generic/broad AND no domain is inferable from context
  - Multiple plausible interpretations exist
  - No clear research objective

Model: configured via settings.models ("query_analysis" task → Pro by default)

Node contract
-------------
Input  fields consumed: original_topic, context_brief, clarification_answers,
                        clarification_questions, clarification_count,
                        uploaded_files, tenant_id
Output fields written:  needs_clarification, clarification_questions,
                        clarification_count, refined_topic, depth_of_research,
                        audience, objective, domain, recency_scope,
                        source_scope, assumptions, status
"""

from __future__ import annotations

import asyncio
import json
import logging
from itertools import zip_longest
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.config import settings
from app.llm_factory import get_llm
from graphs.planning.prompts.query_analyzer import (
    _SYSTEM_PROMPT,
    _USER_PROMPT_TEMPLATE,
)
from graphs.planning.state import PlanningState, SourceType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_clarification(questions: list[str], answers: list[str]) -> str:
    if not answers:
        return "(none)"
    return "\n".join(
        f"Q: {q}\nA: {a}"
        for q, a in zip_longest(questions, answers, fillvalue="(not answered)")
    )


def _parse_response(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def query_analyzer(
    state: PlanningState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """LangGraph node: classify topic and extract research parameters.

    Returns a partial state dict; LangGraph merges it into the full state.
    """
    topic = state.get("original_topic", "").strip()
    context_brief = state.get("context_brief", "")
    clarification_questions = state.get("clarification_questions") or []
    clarification_answers = state.get("clarification_answers") or []
    clarification_count = state.get("clarification_count", 0)
    uploaded_files = state.get("uploaded_files") or []

    user_prompt = _USER_PROMPT_TEMPLATE.format(
        topic=topic,
        context_brief=context_brief or "(no context brief available)",
        clarification_text=_format_clarification(clarification_questions, clarification_answers),
    )

    llm = get_llm("query_analysis", 0.2)
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    logger.info(
        "query_analyzer: invoking %s for topic_id=%s",
        settings.models.model_for("query_analysis"),
        state.get("topic_id"),
    )

    try:
        response = await asyncio.wait_for(
            llm.ainvoke(messages, config=config), timeout=settings.llm_timeout_seconds
        )
    except asyncio.TimeoutError:
        logger.error(
            "query_analyzer: LLM timed out after %ds", settings.llm_timeout_seconds
        )
        return {"status": "failed", "error": "query_analyzer: LLM call timed out"}
    except Exception as exc:
        logger.error("query_analyzer: LLM call failed — %s", exc)
        return {"status": "failed", "error": f"query_analyzer: LLM call failed — {exc}"}

    raw_text: str = response.content

    try:
        parsed = _parse_response(raw_text)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("query_analyzer: failed to parse LLM JSON response — %s", exc)
        return {
            "status": "failed",
            "error": f"query_analyzer produced unparseable output: {exc}",
        }

    needs_clarification = bool(parsed.get("needs_clarification", False))

    # Validate and coerce source_scope.
    # "tavily" is a provider preference, not a source type the LLM should control
    # independently — preserve it from the user's original request only.
    valid_sources: set[SourceType] = {"web", "tavily", "arxiv", "content_lake", "files"}
    original_scope: list[SourceType] = state.get("source_scope") or []
    source_scope: list[SourceType] = [
        s for s in parsed.get("source_scope", ["web"]) if s in valid_sources and s != "tavily"
    ]
    if "web" not in source_scope:
        source_scope.insert(0, "web")
    if not uploaded_files and "files" in source_scope:
        source_scope.remove("files")
    # Honour the user's explicit Tavily opt-in — LLM cannot add or remove it.
    if "tavily" in original_scope and "tavily" not in source_scope:
        source_scope.append("tavily")

    # Increment clarification_count only when asking new questions
    new_clarification_count = clarification_count
    if needs_clarification:
        new_clarification_count = clarification_count + 1

    result: dict[str, Any] = {
        "needs_clarification": needs_clarification,
        "clarification_questions": parsed.get("clarification_questions") or [] if needs_clarification else [],
        "clarification_count": new_clarification_count,
        "refined_topic": parsed.get("refined_topic", topic),
        "depth_of_research": parsed.get("depth_of_research", "intermediate"),
        "audience": parsed.get("audience", ""),
        "objective": parsed.get("objective", ""),
        "domain": parsed.get("domain", ""),
        "recency_scope": parsed.get("recency_scope", "last_2_years"),
        "source_scope": source_scope,
        "assumptions": parsed.get("assumptions") or [],
        "status": "needs_clarification" if needs_clarification else "planning",
    }

    logger.info(
        "query_analyzer: complete — needs_clarification=%s depth=%s topic_id=%s",
        needs_clarification,
        result["depth_of_research"],
        state.get("topic_id"),
    )

    return result
