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
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.config import settings
from app.llm_factory import get_llm
from graphs.planning.state import PlanningState, SourceType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are the Query Analyzer for Kadal AI's deep research system.
You receive a context brief (a prose summary of the user's conversation history
and uploaded files) plus the user's research topic and any clarification answers.

Your job: decide whether the request is specific enough to plan, or whether
clarifying questions are needed first.

Output a single JSON object — no markdown fences, no commentary.
"""

_USER_PROMPT_TEMPLATE = """\
## Research Topic
{topic}

## Context Brief (summary of conversation and uploaded files)
{context_brief}

## Previous Clarification Q&A (if any)
{clarification_text}

---

Analyse the research request and return a JSON object with EXACTLY these keys:

{{
  "needs_clarification": <true | false>,
  "clarification_questions": ["<question 1>", "<question 2>"],
  "refined_topic": "<clear, specific research question in 1-2 sentences>",
  "depth_of_research": "<surface | intermediate | in-depth>",
  "audience": "<who will read this report>",
  "objective": "<what the report should achieve>",
  "domain": "<primary subject-matter domain>",
  "recency_scope": "<all_time | last_5_years | last_2_years | last_6_months | last_month>",
  "source_scope": ["web", "arxiv", "content_lake", "files"],
  "assumptions": ["<assumption 1>", "..."]
}}

CLARIFICATION RULES — set "needs_clarification": true if ANY apply:
  1. Topic is fewer than 4 words AND context_brief does not resolve the ambiguity.
  2. Topic is generic or overly broad (e.g. "AI", "Technology") AND no domain
     can be inferred from context.
  3. Multiple plausible research interpretations exist with no clear signal.
  4. No identifiable research objective.

When needs_clarification is true:
  - Provide 1-3 focused questions in "clarification_questions" (max 3).
  - Still fill in as many other fields as you CAN confidently infer from context.
  - Leave fields you cannot infer as empty strings / default values.

When needs_clarification is false:
  - "clarification_questions" must be an empty list [].
  - All other fields must be fully populated.

source_scope must only include sources from: web, arxiv, content_lake, files.
"web" is ALWAYS included.
Include "files" only if the user has uploaded files (context brief will mention them).
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_clarification(questions: list[str], answers: list[str]) -> str:
    if not answers:
        return "(none)"
    return "\n".join(
        f"Q: {q}\nA: {a}" for q, a in zip(questions, answers)
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
            llm.ainvoke(messages), timeout=settings.llm_timeout_seconds
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

    # Validate and coerce source_scope
    valid_sources: set[SourceType] = {"web", "arxiv", "content_lake", "files"}
    source_scope: list[SourceType] = [
        s for s in parsed.get("source_scope", ["web"]) if s in valid_sources
    ]
    if "web" not in source_scope:
        source_scope.insert(0, "web")
    if not uploaded_files and "files" in source_scope:
        source_scope.remove("files")

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
