"""
content_lake — Tool Layer (Layer 3)

Internal content lake POST /v2/search semantic chunk search.
Only active when tools_enabled.content_lake is True AND
settings.content_lake_url (or settings.cl_search_endpoint) is configured.

Returns list[RawSearchResult] with source_type="content_lake".
tenant_id is required on every request for data isolation.
object_ids scopes the search to specific uploaded objects.
"""

from __future__ import annotations

import json
import logging
import uuid

import httpx

from app.config import settings
from graphs.execution.state import RawSearchResult

logger = logging.getLogger(__name__)


async def content_lake_search(
    query: str,
    tenant_id: str,
    object_ids: list[str] | None = None,
    bearer_token: str = "",
    max_results: int = 5,
) -> list[RawSearchResult]:
    """Query the internal content lake API for org-specific documents.

    Uses POST /v2/search with semantic chunk ranking.  Results are filtered
    to ``object_ids`` when provided so each section only sees relevant files.

    Args:
        query:        Search query string.
        tenant_id:    Tenant partition key — MUST be included on every request.
        object_ids:   Object IDs to scope the search to (from payload attachments).
                      Empty list or None → no object_id filter.
        bearer_token: Bearer auth token; "" until token fetching is implemented.
        max_results:  Maximum number of chunk results to return.

    Returns:
        List of RawSearchResult dicts.  Empty list on error or misconfiguration.
    """
    # Resolve endpoint: prefer explicit CL_SEARCH_END_POINT, fall back to
    # content_lake_url + /v2/search.
    base_url = settings.cl_search_endpoint
    if not base_url:
        if not settings.content_lake_url:
            logger.warning(
                "content_lake_search: neither CL_SEARCH_END_POINT nor "
                "CONTENT_LAKE_URL configured — skipping"
            )
            return []
        base_url = f"{settings.content_lake_url.rstrip('/')}/v2/search"

    params = {
        "query_string": query,
        "page_size": max_results,
        "re_ranked": "true",
    }
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if bearer_token:
        headers["Authorization"] = bearer_token

    filter_payload = {
        "tenant_id": tenant_id,
        "tags": [],
        "folders": [],
        "object_ids": object_ids or [],
        "repos": [],
        "kvp": {},
        "taxonomy": {},
    }

    try:
        async with httpx.AsyncClient(timeout=settings.content_lake_timeout) as client:
            response = await client.post(
                base_url,
                params=params,
                headers=headers,
                content=json.dumps(filter_payload),
            )
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

    api_data = data.get("data") or {}
    objects_by_id: dict[str, dict] = {
        obj["object_id"]: obj for obj in (api_data.get("object") or [])
    }
    text_chunks: list[dict] = api_data.get("text_chunks") or []

    results: list[RawSearchResult] = []
    for chunk in text_chunks:
        score = float(chunk.get("score", 0.0))
        if score <= 0:
            continue
        obj_meta = objects_by_id.get(chunk.get("or_object_id", ""), {})
        chunk_section_title = (chunk.get("metadata") or {}).get("title", "")
        title_parts = [obj_meta.get("title", ""), chunk_section_title]
        title = " — ".join(p for p in title_parts if p) or obj_meta.get("file_name", "")
        results.append(
            RawSearchResult(
                result_id=str(uuid.uuid4()),
                source_type="content_lake",
                query=query,
                url=obj_meta.get("uri", ""),
                title=title,
                content=chunk.get("text", ""),
                published_at="",
                score=min(score, 1.0),
            )
        )

    results = results[:max_results]
    logger.info(
        "content_lake_search: %d result(s) from %d chunk(s) "
        "(query=%r, tenant_id=%s)",
        len(results),
        len(text_chunks),
        query,
        tenant_id,
    )
    return results
