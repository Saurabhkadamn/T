"""
context_builder — Planning Graph Node (Layer 1, Node 1)

Responsibility
--------------
Mode 1 only (fresh research request).  Does three things in sequence:

1. Fetch uploaded file contents from Redis.
2. Use ``state["chat_history"]`` passed directly in the request payload.
3. Call the LLM (Flash) to produce a concise ``context_brief`` — a
   2000-3000 token prose summary of the chat context and file contents
   that all downstream nodes read instead of raw inputs.

Why an LLM call here?
----------------------
query_analyzer used to receive raw chat_history + file_contents separately.
The new spec collapses those into a single ``context_brief`` so that
query_analyzer only needs one input field and its prompt stays predictable
regardless of how much raw context exists.

Failure behaviour
-----------------
- Redis miss for a file → degrade gracefully (empty string for that file).
- LLM failure           → returns status="failed" so the graph can surface
                          the error to the API caller immediately.

Node contract
-------------
Input  fields consumed: uploaded_files, tenant_id, topic_id, chat_history,
                        original_topic
Output fields written:  file_contents, context_brief
                        On error: status="failed", error=<message>
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import redis.asyncio as aioredis
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.config import settings
from app.llm_factory import get_llm
from graphs.planning.state import ChatMessage, PlanningState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Redis key helper  (matches schema in CLAUDE.md § Redis Key Schema)
# ---------------------------------------------------------------------------

def _file_key(object_id: str) -> str:
    return f"file:{object_id}:content"


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a context summariser for Kadal AI's deep research system.
Your job is to read a user's research topic, their recent conversation history,
and any uploaded file excerpts, then produce a single cohesive prose summary
(the "context brief") that will be passed to the research planning pipeline.

The context brief must:
- Explain what the user is trying to research and why (based on conversation).
- Highlight any constraints, preferences, or domain knowledge from the conversation.
- Summarise key facts or themes from uploaded files that are relevant to the topic.
- Be written in third-person neutral style, 300-500 words.
- NOT include clarification questions — only summarise what is already known.

Output plain prose only — no markdown headers, no bullet lists.
"""

_USER_PROMPT_TEMPLATE = """\
## Research Topic
{topic}

## Recent Conversation History ({history_len} turns)
{chat_history_text}

## Uploaded File Excerpts
{file_summary_text}

---

Write the context brief now.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_chat_history(history: list[ChatMessage]) -> str:
    if not history:
        return "(no conversation history)"
    lines = []
    for msg in history:
        role = msg.get("role", "user").capitalize()
        lines.append(f"{role}: {msg.get('content', '')}")
    return "\n".join(lines)


def _format_file_excerpts(file_contents: dict[str, str], uploaded_files: list[dict]) -> str:
    if not uploaded_files:
        return "(no uploaded files)"
    lines = []
    for f in uploaded_files:
        oid = f["object_id"]
        content = file_contents.get(oid, "")
        if content:
            snippet = content[:500].replace("\n", " ")
            lines.append(f"[{f['filename']}]: {snippet}…")
        else:
            lines.append(f"[{f['filename']}]: (content unavailable)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def context_builder(
    state: PlanningState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """LangGraph node: build context_brief from chat history + file contents.

    Parameters
    ----------
    state:  current PlanningState snapshot (read-only inside the node).
    config: LangGraph run-time config dict.
            Pass shared clients via config["configurable"]:
              redis_client  — aioredis.Redis  (avoids opening a new connection)
              mongo_client  — AsyncIOMotorClient (for chat history fetch)

    Returns
    -------
    Partial state dict with keys: file_contents, context_brief.
    On LLM failure: also writes status="failed", error=<message>.
    """
    topic_id = state.get("topic_id", "")
    uploaded_files = state.get("uploaded_files") or []
    original_topic = state.get("original_topic", "")
    chat_history: list[ChatMessage] = state.get("chat_history") or []

    configurable: dict = (config or {}).get("configurable", {}) if config else {}
    shared_redis: aioredis.Redis | None = configurable.get("redis_client")

    # ── Step 1: Load file contents from Redis ──────────────────────────────
    file_contents: dict[str, str] = {}

    if uploaded_files:
        owns_redis = shared_redis is None
        redis_client: aioredis.Redis = shared_redis or aioredis.Redis(
            host=settings.redis_host,
            password=settings.redis_password or None,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
        try:
            for file_meta in uploaded_files:
                object_id: str = file_meta["object_id"]
                try:
                    content: str | None = await redis_client.get(_file_key(object_id))
                except Exception as redis_exc:
                    logger.warning(
                        "context_builder: Redis error for object_id=%s — %s (treating as cache miss)",
                        object_id, redis_exc,
                    )
                    content = None
                if content is None:
                    logger.warning(
                        "context_builder: cache miss for object_id=%s (file=%s)",
                        object_id,
                        file_meta.get("filename", "<unknown>"),
                    )
                    file_contents[object_id] = ""
                else:
                    file_contents[object_id] = content
        finally:
            if owns_redis:
                await redis_client.aclose()

        logger.info(
            "context_builder: loaded %d/%d file contents (topic_id=%s)",
            sum(1 for v in file_contents.values() if v),
            len(uploaded_files),
            topic_id,
        )

    logger.info(
        "context_builder: using %d chat turns from request payload (topic_id=%s)",
        len(chat_history), topic_id,
    )

    # ── Step 2: Call LLM to produce context_brief ─────────────────────────
    user_prompt = _USER_PROMPT_TEMPLATE.format(
        topic=original_topic,
        history_len=len(chat_history),
        chat_history_text=_format_chat_history(chat_history),
        file_summary_text=_format_file_excerpts(file_contents, uploaded_files),
    )

    llm = get_llm("context_building", 0.3)
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    logger.info(
        "context_builder: invoking %s for context_brief (topic_id=%s)",
        settings.models.model_for("context_building"),
        topic_id,
    )

    try:
        response = await asyncio.wait_for(
            llm.ainvoke(messages, config=config), timeout=settings.llm_timeout_seconds
        )
    except asyncio.TimeoutError:
        logger.error(
            "context_builder: LLM timed out after %ds (topic_id=%s)",
            settings.llm_timeout_seconds,
            topic_id,
        )
        return {
            "file_contents": file_contents,
            "context_brief": "",
            "status": "failed",
            "error": "context_builder: LLM call timed out",
        }
    except Exception as exc:
        logger.error("context_builder: LLM call failed — %s", exc)
        return {
            "file_contents": file_contents,
            "context_brief": "",
            "status": "failed",
            "error": f"context_builder: LLM call failed — {exc}",
        }

    context_brief: str = response.content.strip()
    logger.info(
        "context_builder: context_brief produced (%d chars, topic_id=%s)",
        len(context_brief),
        topic_id,
    )

    return {
        "file_contents": file_contents,
        "context_brief": context_brief,
    }
