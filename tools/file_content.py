"""
file_content — Tool Layer (Layer 3)

Redis-first lookup for user-uploaded file text content, with LOR API → S3 fallback.

Key schema (production):
  extracted_text_{object_id}   — cached full text
  extracted_url_{object_id}    — cached S3 URL (avoids repeated LOR calls)

Fallback chain per object_id:
  1. Redis GET extracted_text_{object_id}   → cache hit → return immediately
  2. Redis GET extracted_url_{object_id}    → have S3 URL → skip to step 4
  3. LOR API GET /objects/{object_id}       → parse extracted_text_file_url
                                            → cache in extracted_url_{object_id}
  4. boto3 S3 GetObject                    → decode text
                                           → cache in extracted_text_{object_id}

Bearer token is a placeholder ("") until token fetching is implemented.
"""

from __future__ import annotations

import asyncio
import logging

import boto3
import httpx
import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Redis key helpers
# ---------------------------------------------------------------------------

def _text_key(object_id: str) -> str:
    return f"extracted_text_{object_id}"


def _url_key(object_id: str) -> str:
    return f"extracted_url_{object_id}"


# ---------------------------------------------------------------------------
# S3 helpers (sync, run in executor)
# ---------------------------------------------------------------------------

def _fetch_from_s3_sync(s3_url: str) -> str | None:
    """Download text content from an s3:// URL using boto3."""
    try:
        path = s3_url.removeprefix("s3://")
        bucket, key = path.split("/", 1)
        s3 = boto3.client("s3", region_name=settings.s3_region)
        obj = s3.get_object(Bucket=bucket, Key=key)
        return obj["Body"].read().decode("utf-8")
    except Exception as exc:
        logger.error("file_content: S3 fetch failed — %s (url=%s)", exc, s3_url)
        return None


async def _fetch_from_s3(s3_url: str) -> str | None:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch_from_s3_sync, s3_url)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_file_content(
    object_id: str,
    bearer_token: str = "",
    redis_client: aioredis.Redis | None = None,
) -> str | None:
    """Fetch file text content for one object_id.

    Follows the Redis → LOR API → S3 fallback chain described in the module
    docstring.  Returns None if the content cannot be retrieved.

    Args:
        object_id:    Platform-wide unique identifier for the uploaded file.
        bearer_token: Auth token for LOR API; "" until token fetching is wired.
        redis_client: Optional shared async Redis client.  If None, creates a
                      temporary connection closed automatically after the call.
    """
    owns_client = redis_client is None
    if owns_client:
        redis_client = aioredis.Redis(
            host=settings.redis_host,
            password=settings.redis_password or None,
            decode_responses=True,
        )

    try:
        # Step 1 — cached text
        cached_text = await redis_client.get(_text_key(object_id))
        if cached_text:
            logger.debug("file_content: text cache hit — object_id=%s", object_id)
            return cached_text

        # Step 2 — cached S3 URL
        s3_url: str | None = await redis_client.get(_url_key(object_id))

        # Step 3 — LOR API for S3 URL
        if not s3_url and settings.lor_endpoint:
            lor_url = f"{settings.lor_endpoint.rstrip('/')}/objects/{object_id}"
            headers: dict[str, str] = {}
            if bearer_token:
                headers["Authorization"] = bearer_token
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(lor_url, headers=headers)
                    if resp.status_code == 200:
                        obj_data = resp.json()
                        s3_url = (
                            (obj_data.get("data") or {})
                            .get("system_metadata", {})
                            .get("extracted_text_file_url")
                        )
                        if s3_url:
                            await redis_client.set(_url_key(object_id), s3_url)
                            logger.debug(
                                "file_content: LOR returned S3 URL — object_id=%s",
                                object_id,
                            )
                    else:
                        logger.warning(
                            "file_content: LOR API returned %d — object_id=%s",
                            resp.status_code,
                            object_id,
                        )
            except Exception as exc:
                logger.error(
                    "file_content: LOR API call failed — %s (object_id=%s)",
                    exc,
                    object_id,
                )

        # Step 4 — download from S3 and cache
        if s3_url and s3_url != "failure":
            text = await _fetch_from_s3(s3_url)
            if text:
                await redis_client.set(_text_key(object_id), text)
                return text

        logger.warning("file_content: cache miss, no fallback succeeded — object_id=%s", object_id)
        return None

    except Exception as exc:
        logger.error(
            "file_content: unexpected error — %s (object_id=%s)", exc, object_id
        )
        return None
    finally:
        if owns_client:
            await redis_client.aclose()


async def get_multiple_file_contents(
    object_ids: list[str],
    bearer_token: str = "",
    redis_client: aioredis.Redis | None = None,
) -> dict[str, str]:
    """Fetch text content for multiple object_ids concurrently.

    Args:
        object_ids:   List of platform-wide object identifiers.
        bearer_token: Auth token forwarded to each get_file_content call.
        redis_client: Optional shared async Redis client.

    Returns:
        Dict mapping object_id → content for all successfully retrieved files.
        Missing files are omitted (no KeyError raised).
    """
    if not object_ids:
        return {}

    owns_client = redis_client is None
    if owns_client:
        redis_client = aioredis.Redis(
            host=settings.redis_host,
            password=settings.redis_password or None,
            decode_responses=True,
        )

    try:
        tasks = [
            get_file_content(oid, bearer_token=bearer_token, redis_client=redis_client)
            for oid in object_ids
        ]
        values = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if owns_client:
            await redis_client.aclose()

    result: dict[str, str] = {}
    for oid, val in zip(object_ids, values):
        if isinstance(val, str) and val:
            result[oid] = val
        elif isinstance(val, Exception):
            logger.error(
                "file_content: get_multiple error for object_id=%s — %s", oid, val
            )

    logger.info(
        "file_content: %d/%d file(s) retrieved", len(result), len(object_ids)
    )
    return result
