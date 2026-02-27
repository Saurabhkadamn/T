"""
content_lake — Tool Layer (Layer 3)

Internal content lake HTTP API client.
Only active when tools_enabled.content_lake is True AND
settings.content_lake_url is configured.

Returns list[RawSearchResult] with source_type="content_lake".
tenant_id is required on every request for data isolation.
"""

from __future__ import annotations

import logging
import uuid

import httpx

from app.config import settings
from graphs.execution.state import RawSearchResult

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0


async def content_lake_search(
    query: str,
    tenant_id: str,
    max_results: int = 5,
) -> list[RawSearchResult]:
    """Query the internal content lake API for org-specific documents.

    The content lake holds pre-indexed internal documents (wikis, runbooks,
    product specs). Results carry trust_tier=2 (lower than user files,
    higher than academic/web).

    Args:
        query:       Search query string.
        tenant_id:   Tenant partition key — MUST be included on every request.
        max_results: Maximum number of documents to return.

    Returns:
        List of RawSearchResult dicts. Empty list on error or misconfiguration.
    """
    if not settings.content_lake_url:
        logger.warning(
            "content_lake_search: CONTENT_LAKE_URL not configured — skipping"
        )
        return []

    url = f"{settings.content_lake_url.rstrip('/')}/search"
    params = {
        "query": query,
        "tenant_id": tenant_id,
        "limit": max_results,
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.error(
            "content_lake_search: request failed — %s (query=%r, tenant_id=%s)",
            exc,
            query,
            tenant_id,
        )
        return []

    results: list[RawSearchResult] = []
    for item in data.get("results") or []:
        results.append(
            RawSearchResult(
                result_id=str(uuid.uuid4()),
                source_type="content_lake",
                query=query,
                url=item.get("url", ""),
                title=item.get("title", ""),
                content=item.get("content", ""),
                published_at=item.get("published_at", ""),
                # Content lake results are org-specific and curated — high base score.
                score=float(item.get("score", 0.8)),
            )
        )

    logger.info(
        "content_lake_search: %d result(s) for query=%r (tenant_id=%s)",
        len(results),
        query,
        tenant_id,
    )
    return results
