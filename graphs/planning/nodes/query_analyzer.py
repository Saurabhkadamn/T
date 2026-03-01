"""
query_analyzer — Planning Graph Node (Layer 1, Node 2)

Responsibility
--------------
Take the raw user topic + context (chat history, uploaded file contents,
clarification answers if any) and produce:

  - refined_topic        — normalised, de-ambiguated research question
  - needs_clarification  — True if the query is too ambiguous to plan
  - depth_of_research    — surface / intermediate / in-depth
  - audience             — who will read the report
  - objective            — what the report should achieve
  - domain               — subject-matter domain
  - recency_scope        — how fresh the sources need to be
  - source_scope         — which source integrations to activate
  - assumptions          — list of assumptions made when resolving ambiguity

Model: Gemini 2.5 Pro  (configured via settings.models)

Node contract
-------------
Input  fields consumed: original_topic, chat_history, file_contents,
                        clarification_answers, clarification_count, tenant_id
Output fields written:  refined_topic, needs_clarification, depth_of_research,
                        audience, objective, domain, recency_scope,
                        source_scope, assumptions, status
"""

from __future__ import annotations

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
Your job is to interpret a user's research request and extract structured
parameters that will guide an AI research pipeline.

You will output a single JSON object — no markdown fences, no commentary.
"""

_USER_PROMPT_TEMPLATE = """\
## Research Request

**Topic:** {topic}

## Chat History (last {history_len} turns)
{chat_history_text}

## Uploaded File Summaries
{file_summary_text}

## Clarification Answers (if any)
{clarification_text}

---

Analyse the research request above and return a JSON object with exactly
these keys:

{{
  "refined_topic": "<clear, specific research question in 1-2 sentences>",
  "needs_clarification": <true | false>,
  "depth_of_research": "<surface | intermediate | in-depth>",
  "audience": "<who will read this report>",
  "objective": "<what the report should achieve>",
  "domain": "<primary subject-matter domain>",
  "recency_scope": "<all_time | last_5_years | last_2_years | last_6_months | last_month>",
  "source_scope": ["web", "arxiv", "content_lake", "files"],
  "assumptions": ["<assumption 1>", "..."]
}}

Rules:
You must decide whether the topic is sufficiently specific to produce
a high-quality research plan.

Set "needs_clarification": true if ANY of the following apply:

- Topic is fewer than 4 words.
- Topic is generic or overly broad (e.g., "AI", "Technology").
- No clear timeframe.
- No specific domain or subfield mentioned.
- Multiple plausible interpretations exist.
- No identifiable research objective.

If in doubt, err on the side of asking clarification questions.
Do NOT guess missing constraints.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_chat_history(chat_history: list[dict]) -> str:
    if not chat_history:
        return "(none)"
    lines = []
    for msg in chat_history[-6:]:  # keep last 6 turns to stay within context
        role = msg.get("role", "user").capitalize()
        lines.append(f"{role}: {msg.get('content', '')}")
    return "\n".join(lines)


def _format_file_summaries(file_contents: dict[str, str], uploaded_files: list[dict]) -> str:
    if not uploaded_files:
        return "(no files uploaded)"
    lines = []
    for f in uploaded_files:
        oid = f["object_id"]
        content = file_contents.get(oid, "")
        snippet = content[:300].replace("\n", " ") if content else "(content unavailable)"
        lines.append(f"- {f['filename']}: {snippet}…")
    return "\n".join(lines)


def _format_clarification(answers: list[str], questions: list[str]) -> str:
    if not answers:
        return "(no clarification provided yet)"
    pairs = zip(questions, answers)
    return "\n".join(f"Q: {q}\nA: {a}" for q, a in pairs)


def _parse_response(raw: str) -> dict:
    """Strip any accidental markdown fences and parse JSON."""
    text = raw.strip()
    if text.startswith("```"):
        # Remove ```json ... ``` or ``` ... ```
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def query_analyzer(state: PlanningState, config: RunnableConfig | None = None) -> dict[str, Any]:
    """LangGraph node: analyse and enrich the research query.

    Returns a partial state dict; LangGraph merges it into the full state.
    """
    topic = state.get("original_topic", "").strip()
    chat_history = state.get("chat_history") or []
    file_contents = state.get("file_contents") or {}
    uploaded_files = state.get("uploaded_files") or []
    clarification_questions = state.get("clarification_questions") or []
    clarification_answers = state.get("clarification_answers") or []

    user_prompt = _USER_PROMPT_TEMPLATE.format(
        topic=topic,
        history_len=len(chat_history),
        chat_history_text=_format_chat_history(chat_history),
        file_summary_text=_format_file_summaries(file_contents, uploaded_files),
        clarification_text=_format_clarification(clarification_answers, clarification_questions),
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
        response = await llm.ainvoke(messages)
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

    # Validate and coerce source_scope to the correct Literal type
    valid_sources: set[SourceType] = {"web", "arxiv", "content_lake", "files"}
    source_scope: list[SourceType] = [
        s for s in parsed.get("source_scope", ["web"]) if s in valid_sources
    ]
    if "web" not in source_scope:
        source_scope.insert(0, "web")  # web is always included

    # If no files were uploaded, remove "files" from scope
    if not uploaded_files and "files" in source_scope:
        source_scope.remove("files")

    result: dict[str, Any] = {
        "refined_topic": parsed.get("refined_topic", topic),
        "needs_clarification": bool(parsed.get("needs_clarification", False)),
        "depth_of_research": parsed.get("depth_of_research", "intermediate"),
        "audience": parsed.get("audience", ""),
        "objective": parsed.get("objective", ""),
        "domain": parsed.get("domain", ""),
        "recency_scope": parsed.get("recency_scope", "last_2_years"),
        "source_scope": source_scope,
        "assumptions": parsed.get("assumptions") or [],
        "status": "needs_clarification" if parsed.get("needs_clarification") else "planning",
    }

    logger.info(
        "query_analyzer: complete — needs_clarification=%s depth=%s topic_id=%s",
        result["needs_clarification"],
        result["depth_of_research"],
        state.get("topic_id"),
    )

    return result
