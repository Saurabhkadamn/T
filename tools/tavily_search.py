"""
tavily_search — Tool Layer (Layer 3)

Direct Tavily REST API client for web search.
Returns list[RawSearchResult] suitable for appending to
SectionResearchState["raw_search_results"].

No MCP — direct httpx POST to api.tavily.com.
Serper is used as fallback when this returns 0 results (see searcher.py).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

from app.config import settings
from graphs.execution.state import RawSearchResult

logger = logging.getLogger(__name__)

_TAVILY_URL = "https://api.tavily.com/search"
_TIMEOUT = 30.0


async def tavily_search(
    query: str,
    max_results: int = 5,
    *,
    search_depth: str = "advanced",
) -> list[RawSearchResult]:
    """Call Tavily search API and return normalised RawSearchResult list.

    Args:
        query:        Search query string.
        max_results:  Maximum number of results to return (1–20).
        search_depth: "basic" or "advanced" (advanced = richer content).

    Returns:
        List of RawSearchResult dicts. Empty list on error (non-fatal).
    """
    payload: dict[str, Any] = {
        "api_key": settings.tavily_api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
        "include_answer": False,
        "include_raw_content": True,
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(_TAVILY_URL, json=payload)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.error("tavily_search: request failed — %s (query=%r)", exc, query)
        return []

    results: list[RawSearchResult] = []
    for item in data.get("results") or []:
        # Prefer raw_content (full page text) over snippet if available.
        content = item.get("raw_content") or item.get("content") or ""
        results.append(
            RawSearchResult(
                result_id=str(uuid.uuid4()),
                source_type="web",
                query=query,
                url=item.get("url", ""),
                title=item.get("title", ""),
                content=content,
                published_at=item.get("published_date", ""),
                score=float(item.get("score", 0.0)),
            )
        )

    logger.info(
        "tavily_search: %d result(s) for query=%r", len(results), query
    )
    return results
