"""
file_content — Tool Layer (Layer 3)

Redis cache lookup by object_id for user-uploaded file content.
Key schema: file:{object_id}:content  (platform-wide, no tenant prefix)

In normal operation, file_contents are pre-loaded into SectionResearchState
by the worker before the execution graph starts. This module provides a
fallback for dynamic lookups (e.g., if the worker restarts mid-job and
needs to re-fetch a specific file from the cache).
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)


def _redis_key(object_id: str) -> str:
    return f"file:{object_id}:content"


async def get_file_content(object_id: str) -> str | None:
    """Fetch file text content from Redis by object_id.

    Args:
        object_id: Platform-wide unique identifier for the uploaded file.

    Returns:
        Extracted text content as a string, or None if not found in cache.
    """
    key = _redis_key(object_id)
    try:
        client = aioredis.Redis(
            host=settings.redis_host,
            password=settings.redis_password or None,
            decode_responses=True,
        )
        async with client:
            content = await client.get(key)
        if content is None:
            logger.warning("file_content: cache miss — object_id=%s", object_id)
            return None
        logger.debug("file_content: cache hit — object_id=%s", object_id)
        return content
    except Exception as exc:
        logger.error(
            "file_content: Redis error — %s (object_id=%s)", exc, object_id
        )
        return None


async def get_multiple_file_contents(object_ids: list[str]) -> dict[str, str]:
    """Fetch multiple files from Redis in a single MGET pipeline.

    Args:
        object_ids: List of platform-wide object identifiers.

    Returns:
        Dict mapping object_id → content for all cache hits.
        Missing / expired files are omitted (no KeyError raised).
    """
    if not object_ids:
        return {}

    keys = [_redis_key(oid) for oid in object_ids]
    try:
        client = aioredis.Redis(
            host=settings.redis_host,
            password=settings.redis_password or None,
            decode_responses=True,
        )
        async with client:
            values = await client.mget(*keys)
    except Exception as exc:
        logger.error("file_content: Redis MGET error — %s", exc)
        return {}

    result: dict[str, str] = {}
    for oid, val in zip(object_ids, values):
        if val is not None:
            result[oid] = val

    logger.info(
        "file_content: %d/%d cache hit(s)", len(result), len(object_ids)
    )
    return result
