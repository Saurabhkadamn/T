"""
serper_search — Tool Layer (Layer 3)

Direct Serper REST API client for web search (Google-powered fallback).
Used by searcher.py when Tavily returns 0 results or errors.
Returns list[RawSearchResult] with source_type="web".

No MCP — direct httpx POST to google.serper.dev.
"""

from __future__ import annotations

import logging
import uuid

import httpx

from app.config import settings
from graphs.execution.state import RawSearchResult

logger = logging.getLogger(__name__)

_SERPER_URL = "https://google.serper.dev/search"
_TIMEOUT = 30.0


async def serper_search(
    query: str,
    max_results: int = 5,
) -> list[RawSearchResult]:
    """Call Serper (Google) search API and return normalised RawSearchResult list.

    Serper returns snippets only (no full page text). The content field is the
    organic result snippet, which is shorter than Tavily's raw_content. This is
    acceptable for the fallback role — the compressor handles short content.

    Args:
        query:       Search query string.
        max_results: Number of organic results to request.

    Returns:
        List of RawSearchResult dicts. Empty list on error.
    """
    headers = {
        "X-API-KEY": settings.serper_api_key,
        "Content-Type": "application/json",
    }
    payload = {"q": query, "num": max_results}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(_SERPER_URL, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.error("serper_search: request failed — %s (query=%r)", exc, query)
        return []

    organic: list[dict] = data.get("organic") or []
    total = len(organic)
    results: list[RawSearchResult] = []
    for i, item in enumerate(organic):
        # Position-based score: rank 1 → 1.0, last rank → approaches 0.0.
        # Formula: 1.0 - (i / total) gives an even spread across [0, 1).
        score = round(1.0 - (i / max(total, 1)), 4)
        results.append(
            RawSearchResult(
                result_id=str(uuid.uuid4()),
                source_type="web",
                query=query,
                url=item.get("link", ""),
                title=item.get("title", ""),
                content=item.get("snippet", ""),
                published_at=item.get("date", ""),
                score=score,
            )
        )

    logger.info(
        "serper_search: %d result(s) for query=%r", len(results), query
    )
    return results
