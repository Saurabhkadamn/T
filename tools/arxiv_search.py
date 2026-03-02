"""
Advanced arXiv Search Tool
Plan-aware academic retrieval adapter
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import List, Optional, Literal

import arxiv
from graphs.execution.state import RawSearchResult

SortType = Literal["relevance", "last_updated"]


def _build_query(
    query: str,
    categories: Optional[List[str]] = None,
    start_year: Optional[int] = None,
) -> str:
    """
    Build structured arXiv query.

    Supports:
        - category filtering
        - date filtering (lower bound only)
        - free text search
    """

    parts = []

    # Free-text search
    parts.append(f"all:{query}")

    # Category filter
    if categories:
        cat_query = " OR ".join([f"cat:{c}" for c in categories])
        parts.append(f"({cat_query})")

    # Date filter (lower bound only → start_year → now)
    if start_year:
        start = f"{start_year}01010000"
        end = datetime.utcnow().strftime("%Y%m%d%H%M")
        parts.append(f"submittedDate:[{start} TO {end}]")

    return " AND ".join(parts)


async def arxiv_search(
    query: str,
    max_results: int = 5,
    categories: Optional[List[str]] = None,
    sort_by: SortType = "relevance",
    start_year: Optional[int] = None,
) -> List[RawSearchResult]:
    """
    Advanced arXiv search aligned to planning graph.

    Args:
        query: Free text search string
        max_results: Number of results to return
        categories: e.g. ["cs.CL", "cs.AI"]
        sort_by: "relevance" or "last_updated"
        start_year: filter papers submitted since this year

    Returns:
        List[RawSearchResult]
    """

    structured_query = _build_query(
        query=query,
        categories=categories,
        start_year=start_year,
    )

    sort_mode = (
        arxiv.SortCriterion.LastUpdatedDate
        if sort_by == "last_updated"
        else arxiv.SortCriterion.Relevance
    )

    search = arxiv.Search(
        query=structured_query,
        max_results=max_results,
        sort_by=sort_mode,
    )

    client = arxiv.Client()

    loop = asyncio.get_running_loop()
    try:
        papers = await loop.run_in_executor(None, lambda: list(client.results(search)))
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error(
            "arxiv_search: API call failed — %s (query=%r)", exc, query
        )
        return []

    results: List[RawSearchResult] = []

    for paper in papers:
        results.append(RawSearchResult(
            result_id=str(uuid.uuid4()),
            source_type="arxiv",
            query=query,
            url=paper.entry_id,
            title=paper.title,
            content=f"Title: {paper.title}\n\nAbstract: {paper.summary}",
            published_at=paper.published.isoformat() if paper.published else "",
            score=0.7,
        ))

    return results


# -------------------------------------------------------------------------
# Local Test Runner
# -------------------------------------------------------------------------

async def _test():
    print("🔎 Testing Advanced arXiv Search...\n")

    results = await arxiv_search(
        query="generative AI coding assistants productivity",
        max_results=3,
        categories=["cs.SE", "cs.AI"],
        sort_by="relevance",
        start_year=2022,
    )

    print(f"Returned {len(results)} result(s)\n")
    print("=" * 80)

    for i, r in enumerate(results, 1):
        print(f"[{i}] {r['title']}")
        print(f"URL: {r['url']}")
        print(f"Published: {r['published_at']}")
        print(f"Source type: {r['source_type']}")
        print()
        print(r["content"][:500])
        print("\n" + "=" * 80 + "\n")


if __name__ == "__main__":
    asyncio.run(_test())