"""
clarifier — Planning Graph Node (Layer 1, Node 3)

Responsibility
--------------
When query_analyzer sets needs_clarification=True, this node generates
specific, targeted questions to send back to the user.

The node does NOT wait for answers — it writes questions into state and
sets status="needs_clarification".  The API layer surfaces these questions
to the user, collects answers, and re-invokes the graph with the answers
appended to clarification_answers.

Loop guard
----------
clarification_count is checked by the graph router BEFORE this node runs.
If clarification_count >= settings.loops.clarification_max the router
bypasses this node and sends the graph to plan_creator using whatever
context is available.  This node never needs to check the loop limit itself.

Model: Gemini 2.5 Pro  (configured via settings.models)
  — Pro is appropriate here because poor questions waste user time.

Node contract
-------------
Input  fields consumed: refined_topic, domain, chat_history, file_contents,
                        clarification_answers, clarification_count,
                        assumptions, tenant_id
Output fields written:  clarification_questions, clarification_count, status
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.llm_factory import get_llm
from graphs.planning.state import PlanningState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a research assistant helping to clarify an ambiguous research request
before planning begins.  Your questions will be shown directly to the user.

Output a JSON array of question strings — no markdown, no commentary.
Maximum 3 questions.  Each question must be:
- Specific and answerable in 1-2 sentences
- Directly affecting the scope or direction of the research plan
- Not answerable from information already provided
"""

_USER_PROMPT_TEMPLATE = """\
## Research Topic
{refined_topic}

## Domain
{domain}

## Context Already Available
Chat history turns: {history_len}
Uploaded files: {file_count}
Previous clarification rounds: {clarification_count}

## Previous Answers (if any)
{previous_qa}

## Assumptions Already Made
{assumptions_text}

---

Generate clarifying questions that, when answered, would remove
the most critical ambiguities from this research request.

Do NOT ask about things already covered by the assumptions above.
Do NOT ask about formatting or length preferences.

Return a JSON array:
["<question 1>", "<question 2>", ...]
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_previous_qa(questions: list[str], answers: list[str]) -> str:
    if not answers:
        return "(none)"
    return "\n".join(
        f"Q{i+1}: {q}\nA{i+1}: {a}"
        for i, (q, a) in enumerate(zip(questions, answers))
    )


def _parse_questions(raw: str) -> list[str]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    questions = json.loads(text)
    if not isinstance(questions, list):
        raise ValueError("Expected a JSON array of strings")
    return [str(q).strip() for q in questions if str(q).strip()][:3]


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def clarifier(state: PlanningState, config: RunnableConfig | None = None) -> dict[str, Any]:
    """LangGraph node: generate clarifying questions for the user.

    After this node runs the graph pauses and the API returns the questions
    to the caller.  The graph resumes when the user provides answers.
    """
    prev_questions = state.get("clarification_questions") or []
    prev_answers = state.get("clarification_answers") or []
    clarification_count = state.get("clarification_count", 0)
    assumptions = state.get("assumptions") or []
    chat_history = state.get("chat_history") or []
    uploaded_files = state.get("uploaded_files") or []

    user_prompt = _USER_PROMPT_TEMPLATE.format(
        refined_topic=state.get("refined_topic") or state.get("original_topic", ""),
        domain=state.get("domain", ""),
        history_len=len(chat_history),
        file_count=len(uploaded_files),
        clarification_count=clarification_count,
        previous_qa=_format_previous_qa(prev_questions, prev_answers),
        assumptions_text="\n".join(f"- {a}" for a in assumptions) if assumptions else "(none)",
    )

    # Clarifier inherits query_analysis tier (Pro) — bad questions degrade UX.
    llm = get_llm("query_analysis", 0.3)
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    logger.info(
        "clarifier: generating questions (round %d) for topic_id=%s",
        clarification_count + 1,
        state.get("topic_id"),
    )

    try:
        response = await llm.ainvoke(messages)
    except Exception as exc:
        logger.error("clarifier: LLM call failed — %s", exc)
        return {"status": "failed", "error": f"clarifier: LLM call failed — {exc}"}

    try:
        questions = _parse_questions(response.content)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("clarifier: failed to parse questions — %s", exc)
        return {
            "status": "failed",
            "error": f"clarifier produced unparseable output: {exc}",
        }

    logger.info(
        "clarifier: produced %d questions (topic_id=%s)",
        len(questions),
        state.get("topic_id"),
    )

    return {
        "clarification_questions": questions,
        "clarification_count": clarification_count + 1,
        "status": "needs_clarification",
    }
