"""
arxiv_search — Tool Layer (Layer 3)

Arxiv Python SDK client for academic paper search.
No API key required.
Returns list[RawSearchResult] with source_type="arxiv".

The arxiv SDK is synchronous; this module wraps it with
asyncio.get_event_loop().run_in_executor to stay non-blocking.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timezone

import arxiv

from graphs.execution.state import RawSearchResult

logger = logging.getLogger(__name__)


async def arxiv_search(
    query: str,
    max_results: int = 5,
) -> list[RawSearchResult]:
    """Search Arxiv for academic papers and return normalised RawSearchResult list.

    Content is the paper title + abstract. URL is the canonical abs page
    (entry_id), e.g. "https://arxiv.org/abs/2301.00001".

    Args:
        query:       Academic search query (technical terminology preferred).
        max_results: Maximum number of papers to retrieve.

    Returns:
        List of RawSearchResult dicts. Empty list on error.
    """

    def _sync_search() -> list[RawSearchResult]:
        client = arxiv.Client()
        search = arxiv.Search(
            query=query,
            max_results=max_results,
            sort_by=arxiv.SortCriterion.Relevance,
        )
        results: list[RawSearchResult] = []
        for paper in client.results(search):
            # Combine title + abstract as the searchable content block.
            content = f"Title: {paper.title}\n\nAbstract: {paper.summary}"

            # Format author list; cap at 5 then append "et al."
            authors = ", ".join(a.name for a in paper.authors[:5])
            if len(paper.authors) > 5:
                authors += " et al."

            published_at = ""
            if paper.published:
                published_at = paper.published.astimezone(timezone.utc).strftime(
                    "%Y-%m-%d"
                )

            results.append(
                RawSearchResult(
                    result_id=str(uuid.uuid4()),
                    source_type="arxiv",
                    query=query,
                    url=paper.entry_id,
                    title=paper.title,
                    # Embed author info in content so compressor can cite it.
                    content=f"{content}\n\nAuthors: {authors}",
                    published_at=published_at,
                    # Arxiv results are pre-screened for topic relevance.
                    score=0.7,
                )
            )
        return results

    try:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, _sync_search)
    except Exception as exc:
        logger.error("arxiv_search: failed — %s (query=%r)", exc, query)
        return []

    logger.info("arxiv_search: %d paper(s) for query=%r", len(results), query)
    return results
