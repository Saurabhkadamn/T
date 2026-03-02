"""
compressor — Execution Graph Node (Section Sub-graph, Node 4)

★ Critical node — see CLAUDE.md §Architecture for rationale.

Responsibility
--------------
Compress all accumulated raw_search_results for one section down to the
depth-appropriate token budget, and simultaneously build the citation list.

Token targets (depth-dependent, not flat 3-5K):
  surface:      2,500 tokens  (3 sources)
  intermediate: 5,000 tokens  (5 sources)
  in-depth:     8,000 tokens  (10 sources)

The target comes from state["compression_target_tokens"] which is set by
the supervisor when it builds each SectionResearchState via Send().

Compression strategy
--------------------
Each raw_search_result is labelled [S1], [S2], … before being passed to
the Flash model.  The model produces concise prose that:
  - Preserves key facts, statistics, and named entities.
  - Attributes claims to source labels (e.g. "According to [S3]…").
  - Respects the token budget by omitting redundant or lower-quality content.

Citation assembly
-----------------
After compression, this node creates one Citation per source that was
included in the prompt.  Citations carry a trust_tier based on source_type:
  files         → 1 (highest trust)
  content_lake  → 2
  arxiv         → 3
  web           → 4 (lowest trust)

Model: Gemini 2.5 Flash  (mechanical task — called once per section)

Node contract
-------------
Input  fields consumed: raw_search_results, verified_urls, section_title,
                        section_description, compression_target_tokens
Output fields written:  compressed_text, citations
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.config import settings
from app.llm_factory import get_llm
from graphs.execution.state import (
    Citation,
    RawSearchResult,
    SectionResearchState,
    SourceType,
    VerifiedUrl,
)

logger = logging.getLogger(__name__)

# Approx chars per token for budget estimation.  Used only for logging/
# truncation of input; the LLM enforces the actual token budget.
_CHARS_PER_TOKEN = 4

# Max chars per source fed to the compressor.  Prevents a single large web
# page from dominating the prompt.
_MAX_CHARS_PER_SOURCE = 8_000

_TRUST_TIER: dict[SourceType, int] = {
    "files": 1,
    "content_lake": 2,
    "arxiv": 3,
    "web": 4,
}


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a research analyst compressing raw search results into a concise, \
factual summary for an AI-generated research report.

Your task:
1. Read all labelled sources below.
2. Produce a dense, fact-rich summary that covers the most important findings.
3. Attribute key claims to their source label (e.g. "According to [S2]…").
4. Stay within the specified token budget by omitting redundant or \
low-quality information.
5. Do NOT include opinions, filler, or meta-commentary about the sources.
6. Output ONLY the compressed summary — no preamble, no markdown headers.
"""

_USER_TEMPLATE = """\
## Section to summarise
**Title:** {section_title}
**Description:** {section_description}

## Token budget
Target approximately {target_tokens} tokens in your response.

## Sources
{sources_block}

---

Produce the compressed summary now.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_sources_block(results: list[RawSearchResult]) -> str:
    """Format raw results as labelled source blocks for the prompt."""
    lines: list[str] = []
    for i, r in enumerate(results, start=1):
        label = f"[S{i}]"
        header = f"{label} {r['title'] or '(no title)'}"
        if r.get("url"):
            header += f"  ({r['url']})"
        if r.get("published_at"):
            header += f"  [{r['published_at']}]"
        # Truncate each source to avoid prompt explosion.
        content = r["content"][:_MAX_CHARS_PER_SOURCE]
        if len(r["content"]) > _MAX_CHARS_PER_SOURCE:
            content += "\n…[truncated]"
        lines.append(f"{header}\n{content}")
    return "\n\n---\n\n".join(lines)


def _url_is_authentic(url: str, verified: list[VerifiedUrl]) -> bool:
    """Return True if the URL passed verification (or has no URL)."""
    if not url:
        return True   # non-web sources are always authentic
    for v in verified:
        if v["url"] == url:
            return v["is_authentic"]
    return True       # not found in verified list → treat as authentic


def _make_citations(results: list[RawSearchResult]) -> list[Citation]:
    """Build one Citation per source included in the compression prompt."""
    citations: list[Citation] = []
    for r in results:
        snippet = r["content"][:300].replace("\n", " ").strip()
        citations.append(
            Citation(
                citation_id=str(uuid.uuid4()),
                section_id="",       # filled in by the calling node's return dict
                source_type=r["source_type"],
                title=r["title"],
                url=r["url"],
                authors="",          # populated for arxiv by scorer if needed
                published_at=r.get("published_at", ""),
                snippet=snippet,
                trust_tier=_TRUST_TIER.get(r["source_type"], 4),
            )
        )
    return citations


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def compressor(
    state: SectionResearchState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """LangGraph node: compress raw search results to the depth-appropriate budget.

    Filters out inauthentic web URLs before compression so the model never
    sees paywall traps or 404 pages.  Non-web sources always pass through.

    Returns:
        compressed_text: str  — compressed narrative for this section
        citations:       list[Citation]  — one entry per source used
    """
    section_id: str = state.get("section_id", "")
    section_title: str = state.get("section_title", "")
    section_description: str = state.get("section_description", "")
    target_tokens: int = state.get("compression_target_tokens", 5_000)
    raw_results: list[RawSearchResult] = state.get("raw_search_results") or []
    verified_urls: list[VerifiedUrl] = state.get("verified_urls") or []

    logger.info(
        "compressor: %d raw result(s), target=%d tokens (section_id=%s)",
        len(raw_results),
        target_tokens,
        section_id,
    )

    if not raw_results:
        logger.warning(
            "compressor: no raw results to compress (section_id=%s)", section_id
        )
        return {"compressed_text": "", "citations": []}

    # Filter out inauthentic web URLs — keep non-web regardless.
    usable = [
        r for r in raw_results
        if r.get("source_type") != "web" or _url_is_authentic(r["url"], verified_urls)
    ]

    if not usable:
        logger.warning(
            "compressor: all %d web result(s) failed verification (section_id=%s)",
            len(raw_results),
            section_id,
        )
        # Fall back to unfiltered results to avoid a blank section.
        usable = raw_results

    # Sort by trust_tier (ascending = higher trust first) then score.
    usable = sorted(
        usable,
        key=lambda r: (_TRUST_TIER.get(r["source_type"], 4), -r["score"]),
    )

    sources_block = _build_sources_block(usable)
    user_prompt = _USER_TEMPLATE.format(
        section_title=section_title,
        section_description=section_description,
        target_tokens=target_tokens,
        sources_block=sources_block,
    )

    llm = get_llm("compression", 0.1)  # near-deterministic — factual task
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    try:
        response = await asyncio.wait_for(
            llm.ainvoke(messages), timeout=settings.llm_timeout_seconds
        )
        compressed_text: str = response.content.strip()
    except asyncio.TimeoutError:
        logger.error(
            "compressor: LLM timed out after %ds (section_id=%s)",
            settings.llm_timeout_seconds, section_id,
        )
        compressed_text = "\n\n".join(
            f"[S{i+1}] {r['title']}: {r['content'][:500]}"
            for i, r in enumerate(usable[:5])
        )
    except Exception as exc:
        logger.error(
            "compressor: LLM call failed — %s (section_id=%s)", exc, section_id
        )
        # Fallback: join first 500 chars of each source.
        compressed_text = "\n\n".join(
            f"[S{i+1}] {r['title']}: {r['content'][:500]}"
            for i, r in enumerate(usable[:5])
        )

    token_estimate = len(compressed_text.split()) * 4 // 3  # rough char→token
    logger.info(
        "compressor: compressed to ~%d tokens (section_id=%s)", token_estimate, section_id
    )

    # Build citations, injecting the section_id.
    citations = _make_citations(usable)
    for c in citations:
        c["section_id"] = section_id

    return {
        "compressed_text": compressed_text,
        "citations": citations,
    }
