"""
arxiv_search — Tool Layer (Layer 3)

Advanced arXiv search with full paper text extraction.

Uses LangChain's ArxivLoader (backed by PyMuPDF) to download and parse
full PDF content from arxiv.org — not just abstracts.  This gives the
compressor node real research findings, methodology, results, and
conclusions instead of a 200-word teaser.

Two-tier approach:
  1. Primary: ArxivLoader downloads PDFs and extracts full text via PyMuPDF.
  2. Fallback: Raw arxiv library returns metadata + abstract if PDF
     extraction fails (network issues, corrupted PDFs, timeouts).

The compressor already handles long content — it has its own per-source
truncation and token budget.  This tool returns the full paper and lets
downstream nodes decide what to keep.

Returns list[RawSearchResult] with source_type="arxiv".

Dependencies:
    pip install arxiv pymupdf langchain-community
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, List, Literal, Optional

from graphs.execution.state import RawSearchResult

logger = logging.getLogger(__name__)

SortType = Literal["relevance", "last_updated"]

# Timeout for the full load operation (search + PDF download + parse).
_LOAD_TIMEOUT_SECONDS = 60

# Retry config for transient arxiv API failures.
_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = 2


# ---------------------------------------------------------------------------
# Primary: Full-text via ArxivLoader (PDF download + PyMuPDF extraction)
# ---------------------------------------------------------------------------

def _load_full_text(query: str, max_results: int) -> list[dict[str, Any]]:
    """Synchronous — run inside run_in_executor.

    Downloads PDFs from arxiv and extracts full paper text.
    Returns list of dicts with content, title, authors, published, summary,
    entry_id.
    """
    from langchain_community.document_loaders import ArxivLoader

    loader = ArxivLoader(
        query=query,
        load_max_docs=max_results,
        load_all_available_meta=True,
    )

    docs = loader.load()

    results: list[dict[str, Any]] = []
    for doc in docs:
        meta = doc.metadata or {}
        # Metadata key casing is inconsistent in langchain-community:
        #   Title, Authors, Summary, Published  → capitalized
        #   entry_id, primary_category, links   → lowercase
        entry_id = meta.get("entry_id", "") or meta.get("Entry ID", "")
        links = meta.get("links", [])
        # Use entry_id as URL; fall back to first link if entry_id is empty
        url = entry_id or (links[0] if links else "")

        results.append({
            "content": doc.page_content,
            "title": meta.get("Title", ""),
            "authors": meta.get("Authors", ""),
            "summary": meta.get("Summary", ""),
            "published": str(meta.get("Published", "")),
            "entry_id": url,
            "categories": meta.get("categories", []),
        })

    return results


# ---------------------------------------------------------------------------
# Fallback: Abstract-only via raw arxiv library
# ---------------------------------------------------------------------------

def _load_abstracts_only(
    query: str,
    max_results: int,
    sort_by: SortType = "relevance",
    categories: Optional[List[str]] = None,
    start_year: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Synchronous fallback — metadata + abstract when PDF extraction fails.

    Supports advanced query features (categories, date filtering, sort)
    that ArxivLoader does not expose directly.
    """
    import arxiv

    # Build structured query
    parts = [f"all:{query}"]

    if categories:
        cat_query = " OR ".join(f"cat:{c}" for c in categories)
        parts.append(f"({cat_query})")

    if start_year:
        start = f"{start_year}01010000"
        end = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        parts.append(f"submittedDate:[{start} TO {end}]")

    structured_query = " AND ".join(parts)

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
    papers = list(client.results(search))

    results: list[dict[str, Any]] = []
    for paper in papers:
        authors = ", ".join(a.name for a in paper.authors) if paper.authors else ""

        results.append({
            "content": (
                f"Title: {paper.title}\n\n"
                f"Authors: {authors}\n\n"
                f"Abstract: {paper.summary}"
            ),
            "title": paper.title,
            "authors": authors,
            "summary": paper.summary or "",
            "published": paper.published.isoformat() if paper.published else "",
            "entry_id": paper.entry_id or "",
        })

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def arxiv_search(
    query: str,
    max_results: int = 5,
    categories: Optional[List[str]] = None,
    sort_by: SortType = "relevance",
    start_year: Optional[int] = None,
) -> List[RawSearchResult]:
    """Search arxiv and return full paper text where possible.

    Primary path: ArxivLoader downloads PDFs and extracts full text.
    Fallback path: Raw arxiv library returns abstract-only results.

    The fallback activates on any failure — timeout, import error,
    network issue, corrupted PDF.  This ensures the section subgraph
    always gets some content rather than an empty list.

    Args:
        query:       Free text search string.
        max_results: Number of papers to return.
        categories:  arXiv category filters, e.g. ["cs.CL", "cs.AI"].
                     Only used in fallback mode (ArxivLoader doesn't
                     support category filtering directly).
        sort_by:     "relevance" or "last_updated".
        start_year:  Filter papers submitted since this year.
                     Only used in fallback mode.

    Returns:
        List[RawSearchResult] with source_type="arxiv".
    """
    loop = asyncio.get_running_loop()

    # ── Primary: full text via ArxivLoader ─────────────────────────────
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            raw_results = await asyncio.wait_for(
                loop.run_in_executor(None, _load_full_text, query, max_results),
                timeout=_LOAD_TIMEOUT_SECONDS,
            )

            if raw_results:
                logger.info(
                    "arxiv_search: ArxivLoader returned %d paper(s) with full text "
                    "(query=%r)",
                    len(raw_results),
                    query,
                )
                return _to_raw_search_results(raw_results, query, full_text=True)

            # ArxivLoader returned empty — fall through to fallback
            logger.warning(
                "arxiv_search: ArxivLoader returned 0 results (query=%r)", query
            )
            break

        except asyncio.TimeoutError:
            logger.warning(
                "arxiv_search: ArxivLoader timed out after %ds (attempt %d/%d, "
                "query=%r)",
                _LOAD_TIMEOUT_SECONDS,
                attempt,
                _MAX_RETRIES,
                query,
            )
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * attempt)

        except Exception as exc:
            logger.warning(
                "arxiv_search: ArxivLoader failed — %s (attempt %d/%d, query=%r)",
                exc,
                attempt,
                _MAX_RETRIES,
                query,
            )
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * attempt)

    # ── Fallback: abstract-only via raw arxiv library ──────────────────
    logger.info(
        "arxiv_search: falling back to abstract-only mode (query=%r)", query
    )
    try:
        raw_results = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                _load_abstracts_only,
                query,
                max_results,
                sort_by,
                categories,
                start_year,
            ),
            timeout=_LOAD_TIMEOUT_SECONDS,
        )

        logger.info(
            "arxiv_search: fallback returned %d paper(s) (query=%r)",
            len(raw_results),
            query,
        )
        return _to_raw_search_results(raw_results, query, full_text=False)

    except Exception as exc:
        logger.error(
            "arxiv_search: fallback also failed — %s (query=%r)", exc, query
        )
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_raw_search_results(
    raw_results: list[dict[str, Any]],
    query: str,
    full_text: bool,
) -> List[RawSearchResult]:
    """Convert internal dicts to RawSearchResult TypedDicts.

    Score is based on result position (first result = highest relevance).
    This gives the searcher's deduplication and capping logic meaningful
    scores to rank by, unlike a flat 0.7 for everything.
    """
    results: List[RawSearchResult] = []
    total = len(raw_results)

    for idx, r in enumerate(raw_results):
        # Position-based score: first result gets 0.95, last gets ~0.5.
        # This preserves the relevance ordering from arxiv's search API.
        position_score = round(0.95 - (idx * 0.45 / max(total - 1, 1)), 2)

        content = r["content"]

        # Tag the content source so the compressor knows what it's working with.
        if full_text and content:
            content_prefix = f"[Full Paper Text — {len(content):,} chars]\n\n"
        else:
            content_prefix = "[Abstract Only]\n\n"

        results.append(
            RawSearchResult(
                result_id=str(uuid.uuid4()),
                source_type="arxiv",
                query=query,
                url=r.get("entry_id", ""),
                title=r.get("title", ""),
                content=content_prefix + content,
                published_at=r.get("published", ""),
                score=position_score,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Local Test Runner
# ---------------------------------------------------------------------------

async def _test() -> None:
    """Run sample queries to verify both primary and fallback paths."""

    print("=" * 80)
    print("TEST 1: Full text search (ArxivLoader path)")
    print("=" * 80)

    results = await arxiv_search(
        query="transformer attention mechanism",
        max_results=2,
    )

    print(f"\nReturned {len(results)} result(s)\n")

    for i, r in enumerate(results, 1):
        content_len = len(r["content"])
        is_full = "[Full Paper Text" in r["content"]

        print(f"[{i}] {r['title']}")
        print(f"    URL:        {r['url']}")
        print(f"    Published:  {r['published_at']}")
        print(f"    Score:      {r['score']}")
        print(f"    Full text:  {is_full}")
        print(f"    Content:    {content_len:,} chars")
        print()
        # Print first 500 and last 500 chars to verify it's real paper content
        print("    --- FIRST 500 CHARS ---")
        print(f"    {r['content'][:500]}")
        print()
        print("    --- LAST 500 CHARS ---")
        print(f"    {r['content'][-500:]}")
        print()
        print("-" * 80)

    print()
    print("=" * 80)
    print("TEST 2: With category filter (may use fallback if ArxivLoader")
    print("        does not support categories)")
    print("=" * 80)

    results2 = await arxiv_search(
        query="large language model code generation",
        max_results=3,
        categories=["cs.SE", "cs.AI"],
        start_year=2023,
    )

    print(f"\nReturned {len(results2)} result(s)\n")

    for i, r in enumerate(results2, 1):
        content_len = len(r["content"])
        is_full = "[Full Paper Text" in r["content"]

        print(f"[{i}] {r['title']}")
        print(f"    URL:        {r['url']}")
        print(f"    Published:  {r['published_at']}")
        print(f"    Score:      {r['score']}")
        print(f"    Full text:  {is_full}")
        print(f"    Content:    {content_len:,} chars")
        print()

    print()
    print("=" * 80)
    print("TEST 3: Score distribution check")
    print("=" * 80)

    results3 = await arxiv_search(
        query="reinforcement learning robotics",
        max_results=5,
    )

    print(f"\nReturned {len(results3)} result(s)\n")
    print(f"  {'#':<4} {'Score':<8} {'Chars':<10} {'Title'}")
    print(f"  {'-'*4} {'-'*8} {'-'*10} {'-'*40}")

    for i, r in enumerate(results3, 1):
        title_short = r["title"][:40] + "..." if len(r["title"]) > 40 else r["title"]
        print(f"  {i:<4} {r['score']:<8} {len(r['content']):<10,} {title_short}")

    print()
    print("All tests completed.")


if __name__ == "__main__":
    asyncio.run(_test())