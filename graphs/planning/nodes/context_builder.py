"""
context_builder — Planning Graph Node (Layer 1, Node 1)

Responsibility
--------------
Load the text content of every uploaded file from the Redis cache and
write it into state["file_contents"].  This is a pure-Python, no-LLM node —
it does I/O only (Redis lookups) and normalises the uploaded_files list.

Why it exists before query_analyzer
------------------------------------
query_analyzer and all downstream nodes need file contents in state so
they can incorporate user-uploaded documents into their prompts.
Centralising the cache lookup here means every other node can assume
state["file_contents"] is fully populated.

Cache miss behaviour
---------------------
If a file is not in Redis the content is set to an empty string and a
warning is logged.  The node does NOT raise — a missing file should degrade
gracefully (the research continues without that file's content).

Node contract
-------------
Input  fields consumed: uploaded_files, tenant_id, topic_id
Output fields written:  file_contents
"""

from __future__ import annotations

import logging
from typing import Any

import redis.asyncio as aioredis
from langchain_core.runnables import RunnableConfig

from app.config import settings
from graphs.planning.state import PlanningState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Redis key helper  (matches schema in CLAUDE.md § Redis Key Schema)
# ---------------------------------------------------------------------------

def _file_key(object_id: str) -> str:
    """Return the Redis key for a cached file's extracted text content."""
    return f"file:{object_id}:content"


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def context_builder(state: PlanningState, config: RunnableConfig | None = None) -> dict[str, Any]:
    """LangGraph node: load file contents from Redis into state.

    Parameters
    ----------
    state:  current PlanningState snapshot (read-only inside the node).
    config: LangGraph run-time config dict (carries ``configurable`` sub-dict).

    Returns
    -------
    A partial state dict containing only the keys this node writes.
    LangGraph merges the returned dict into the full state.
    """
    uploaded_files = state.get("uploaded_files") or []

    if not uploaded_files:
        logger.debug(
            "context_builder: no uploaded files for topic_id=%s — skipping Redis lookup",
            state.get("topic_id"),
        )
        return {"file_contents": {}}

    # Build Redis client from settings — no singleton here; the worker's
    # lifespan manager should inject a shared client via config if desired,
    # but for now we open a short-lived connection per node call.
    redis_client: aioredis.Redis = aioredis.Redis(
        host=settings.redis_host,
        password=settings.redis_password or None,
        decode_responses=True,
    )

    file_contents: dict[str, str] = {}

    try:
        for file_meta in uploaded_files:
            object_id: str = file_meta["object_id"]
            key = _file_key(object_id)
            content: str | None = await redis_client.get(key)

            if content is None:
                logger.warning(
                    "context_builder: cache miss for object_id=%s (file=%s) — "
                    "proceeding without this file's content",
                    object_id,
                    file_meta.get("filename", "<unknown>"),
                )
                file_contents[object_id] = ""
            else:
                file_contents[object_id] = content
                logger.debug(
                    "context_builder: loaded %d chars for object_id=%s",
                    len(content),
                    object_id,
                )
    finally:
        await redis_client.aclose()

    logger.info(
        "context_builder: loaded content for %d/%d files (topic_id=%s)",
        sum(1 for v in file_contents.values() if v),
        len(uploaded_files),
        state.get("topic_id"),
    )

    return {"file_contents": file_contents}
