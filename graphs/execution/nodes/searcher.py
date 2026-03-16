"""
searcher — Execution Graph Node (Section Sub-graph, Node 2)

Responsibility
--------------
Execute search queries in parallel across all enabled sources for one
section iteration.  Appends new RawSearchResult entries to the state list
and increments search_iteration.

Source dispatching (search_strategy controls which tools run):
  "web"          → Tavily primary; Serper fallback if Tavily returns 0 results
  "arxiv"        → arxiv Python SDK
  "content_lake" → internal content lake API (requires tenant_id)
  "files"        → user-uploaded file contents already in state["file_contents"];
                   converted to RawSearchResult entries on iteration 0 only

All tool calls within a single iteration run concurrently via asyncio.gather.
Duplicate URLs are deduplicated before the results are stored.

Iteration counter:
  This node OWNS search_iteration and increments it by 1 on every call.
  search_query_gen does NOT touch this counter.

Node contract
-------------
Input  fields consumed: search_queries, search_strategy, tenant_id,
                        max_sources, file_contents, tools_enabled (implicit
                        via search_strategy), raw_search_results (previous),
                        search_iteration
Output fields written:  raw_search_results (appended), search_iteration (+1)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from langchain_core.runnables import RunnableConfig

from graphs.execution.state import RawSearchResult, SectionResearchState
from tools.arxiv_search import arxiv_search
from tools.content_lake import content_lake_search
from tools.serper_search import serper_search
from tools.tavily_search import tavily_search

logger = logging.getLogger(__name__)

# Maximum total results per tool call per query.  The searcher caps the final
# list at max_sources after aggregation, so this can be generous.
_RESULTS_PER_QUERY = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_strategy(search_strategy: str) -> list[str]:
    """Split "web,arxiv" → ["web", "arxiv"]."""
    return [t.strip().lower() for t in search_strategy.split(",") if t.strip()]


def _file_results(
    file_contents: dict[str, str], section_title: str
) -> list[RawSearchResult]:
    """Convert pre-loaded file contents into RawSearchResult entries.

    Called once (on iteration 0) when "files" is in the search strategy.
    Each uploaded file becomes one RawSearchResult with trust_tier=1 semantics
    (set later by scorer based on source_type="files").
    """
    results: list[RawSearchResult] = []
    for object_id, content in file_contents.items():
        if not content:
            continue
        # Use first 100 chars of content as the title approximation.
        title_hint = content[:100].replace("\n", " ").strip()
        results.append(
            RawSearchResult(
                result_id=str(uuid.uuid4()),
                source_type="files",
                query=section_title,
                url="",                      # no URL for uploaded files
                title=f"Uploaded file: {object_id[:12]}… — {title_hint}",
                content=content,
                published_at="",             # upload date not tracked here
                score=0.8,                   # high trust but allows relevant web results to compete at cap
            )
        )
    return results


def _deduplicate(
    existing: list[RawSearchResult],
    new_results: list[RawSearchResult],
) -> list[RawSearchResult]:
    """Append new_results, skipping any URL already present in existing."""
    seen_urls: set[str] = {r["url"] for r in existing if r["url"]}
    unique: list[RawSearchResult] = []
    for r in new_results:
        url = r["url"]
        if not url:
            # Non-URL results (files, some arxiv) — always include.
            unique.append(r)
        elif url not in seen_urls:
            seen_urls.add(url)
            unique.append(r)
    return unique


async def _web_search(query: str, n: int, *, use_tavily: bool = False) -> list[RawSearchResult]:
    """Execute web search.

    use_tavily=True  → Tavily (primary) with Serper as fallback on 0 results.
    use_tavily=False → Serper only (default when Tavily not in user's tools).
    """
    if use_tavily:
        results = await tavily_search(query, max_results=n)
        if not results:
            logger.info(
                "searcher: Tavily returned 0 results — falling back to Serper (query=%r)",
                query,
            )
            results = await serper_search(query, max_results=n)
        return results
    return await serper_search(query, max_results=n)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def searcher(
    state: SectionResearchState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """LangGraph node: execute search queries in parallel for one iteration.

    Reads search_queries from state (set by search_query_gen).
    Appends results to raw_search_results and increments search_iteration.
    """
    section_id: str = state.get("section_id", "")
    section_title: str = state.get("section_title", "")
    search_queries: list[str] = state.get("search_queries") or []
    search_strategy: str = state.get("search_strategy", "web")
    tenant_id: str = state.get("tenant_id", "")
    object_ids: list[str] = state.get("object_ids") or []
    bearer_token: str = state.get("bearer_token", "")
    max_sources: int = state.get("max_sources", 3)
    file_contents: dict[str, str] = state.get("file_contents") or {}
    existing_results: list[RawSearchResult] = state.get("raw_search_results") or []
    search_iteration: int = state.get("search_iteration", 0)

    strategies = _parse_strategy(search_strategy)

    logger.info(
        "searcher: iteration %d — %d query/queries, strategy=%r (section_id=%s)",
        search_iteration + 1,
        len(search_queries),
        search_strategy,
        section_id,
    )

    # ------------------------------------------------------------------
    # Build coroutines for all queries × all enabled tools
    # ------------------------------------------------------------------
    tasks: list[asyncio.Task] = []

    use_tavily = "tavily" in strategies
    has_web = "web" in strategies or use_tavily  # "tavily" alone also triggers web search

    for query in search_queries:
        if has_web:
            tasks.append(asyncio.create_task(
                _web_search(query, _RESULTS_PER_QUERY, use_tavily=use_tavily)
            ))
        if "arxiv" in strategies:
            tasks.append(
                asyncio.create_task(arxiv_search(query, max_results=_RESULTS_PER_QUERY))
            )
        if "content_lake" in strategies and tenant_id:
            tasks.append(
                asyncio.create_task(
                    content_lake_search(
                        query,
                        tenant_id,
                        object_ids=object_ids,
                        bearer_token=bearer_token,
                        max_results=_RESULTS_PER_QUERY,
                    )
                )
            )

    # Include file contents on the first iteration only (they don't change).
    new_file_results: list[RawSearchResult] = []
    if "files" in strategies and search_iteration == 0 and file_contents:
        new_file_results = _file_results(file_contents, section_title)
        logger.info(
            "searcher: added %d file result(s) (section_id=%s)",
            len(new_file_results),
            section_id,
        )

    # ------------------------------------------------------------------
    # Run all tool calls in parallel
    # ------------------------------------------------------------------
    gathered: list[RawSearchResult] = list(new_file_results)
    if tasks:
        results_lists = await asyncio.gather(*tasks, return_exceptions=True)
        for outcome in results_lists:
            if isinstance(outcome, Exception):
                logger.error(
                    "searcher: tool call failed — %s (section_id=%s)",
                    outcome,
                    section_id,
                )
            elif isinstance(outcome, list):
                gathered.extend(outcome)

    # ------------------------------------------------------------------
    # Deduplicate and cap at max_sources
    # ------------------------------------------------------------------
    unique_new = _deduplicate(existing_results, gathered)
    all_results = existing_results + unique_new

    # Cap: never exceed max_sources total (not per-iteration).
    if len(all_results) > max_sources:
        # Keep highest-scored results within the cap.
        all_results = sorted(all_results, key=lambda r: r["score"], reverse=True)
        all_results = all_results[:max_sources]

    logger.info(
        "searcher: iteration %d — %d new result(s), %d total (cap=%d) "
        "(section_id=%s)",
        search_iteration + 1,
        len(unique_new),
        len(all_results),
        max_sources,
        section_id,
    )

    return {
        "raw_search_results": all_results,
        "search_iteration": search_iteration + 1,
    }
