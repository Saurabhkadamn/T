"""
scorer — Execution Graph Node (Section Sub-graph, Node 5 / final node)

Responsibility
--------------
Score each source used by this section on three dimensions:
  relevance_score:     How directly relevant is this source to the section?
  recency_score:       How recent is this source? (1.0 = current year, 0.0 = unknown)
  authenticity_score:  From verified_urls for web; 1.0 for non-web.

Combined into composite_score = 0.5·relevance + 0.3·recency + 0.2·authenticity.

Model: Gemini 2.5 Flash (mechanical scoring — high-volume)

The scorer also packages the section's final output fields, which get
merged into ExecutionState via the section_subgraph's output schema:
  section_results:      list[SectionResult]        (one entry)
  compressed_findings:  list[CompressedFinding]    (one entry wrapping compressed_text)
  citations:            list[Citation]              (from compressor)
  source_scores:        list[SourceScore]           (one per source)

Node contract
-------------
Input  fields consumed: raw_search_results, verified_urls, citations,
                        compressed_text, section_id, section_title,
                        section_description, search_iteration
Output fields written:  source_scores, section_findings, section_citations,
                        section_source_scores
                        + output-schema fields merged into ExecutionState:
                          section_results, compressed_findings (list),
                          citations, source_scores
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.llm_factory import get_llm
from graphs.execution.state import (
    Citation,
    CompressedFinding,
    RawSearchResult,
    SectionResearchState,
    SectionResult,
    SourceScore,
    SourceType,
    VerifiedUrl,
)

logger = logging.getLogger(__name__)

_TRUST_TIER: dict[SourceType, int] = {
    "files": 1,
    "content_lake": 2,
    "arxiv": 3,
    "web": 4,
}

# Composite score weights must sum to 1.0.
_W_RELEVANCE = 0.5
_W_RECENCY = 0.3
_W_AUTHENTICITY = 0.2


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a source quality evaluator for an AI research pipeline.
Score each source on relevance and recency.
Output a single JSON object — no markdown fences, no commentary.
"""

_USER_TEMPLATE = """\
## Section Topic
**Title:** {section_title}
**Description:** {section_description}

## Sources to Score
{sources_json}

---

For each source, return relevance (0.0–1.0) and recency (0.0–1.0):
  - relevance: how directly relevant is this source to the section topic?
  - recency: how recent?  1.0 = published in the last year, 0.5 = 3–5 years
    ago, 0.0 = unknown or >10 years.

Return:
{{
  "scores": [
    {{"id": <integer>, "relevance": <float>, "recency": <float>}},
    ...
  ]
}}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _url_hash(url: str) -> str:
    if not url:
        return hashlib.sha256(str(uuid.uuid4()).encode()).hexdigest()
    return hashlib.sha256(url.lower().rstrip("/").encode()).hexdigest()


def _extract_domain(url: str) -> str:
    if not url:
        return ""
    try:
        netloc = urlparse(url).netloc.lower().split(":")[0]
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def _authenticity_for(url: str, verified: list[VerifiedUrl]) -> float:
    """Return authenticity_score: 1.0 for non-web or verified; lower for failed."""
    if not url:
        return 1.0
    for v in verified:
        if v["url"] == url:
            return 1.0 if v["is_authentic"] else 0.0
    return 1.0  # not in verified list → benefit of the doubt


def _parse_llm_scores(raw: str) -> dict[int, dict[str, float]]:
    """Parse LLM response into {1-based index: {relevance, recency}}."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(text)
        return {
            int(s["id"]): {
                "relevance": float(s.get("relevance", 0.5)),
                "recency": float(s.get("recency", 0.5)),
            }
            for s in parsed.get("scores") or []
        }
    except Exception as exc:
        logger.error("scorer: failed to parse LLM response — %s", exc)
        return {}


def _sources_json(results: list[RawSearchResult]) -> str:
    """Build a compact JSON list of source metadata for the scoring prompt."""
    items = []
    for idx, r in enumerate(results, start=1):
        items.append({
            "id": idx,
            "title": r["title"][:120],
            "source_type": r["source_type"],
            "published_at": r.get("published_at", ""),
            "url": r.get("url", ""),
            "snippet": r["content"][:300],
        })
    return json.dumps(items, indent=2)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def scorer(
    state: SectionResearchState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """LangGraph node: score all sources and package section output.

    This is the final node in the section sub-graph.  It writes both the
    internal SectionResearchState output fields AND the fields that will be
    merged into ExecutionState via the output schema.
    """
    section_id: str = state.get("section_id", "")
    section_title: str = state.get("section_title", "")
    section_description: str = state.get("section_description", "")
    raw_results: list[RawSearchResult] = state.get("raw_search_results") or []
    verified_urls: list[VerifiedUrl] = state.get("verified_urls") or []
    citations: list[Citation] = state.get("citations") or []
    compressed_text: str = state.get("compressed_text", "")
    search_iteration: int = state.get("search_iteration", 0)

    logger.info(
        "scorer: scoring %d source(s) (section_id=%s)", len(raw_results), section_id
    )

    now_iso = datetime.now(tz=timezone.utc).isoformat()
    llm_scores: dict[str, dict[str, float]] = {}

    # ------------------------------------------------------------------
    # LLM scoring (skip if no results to score)
    # ------------------------------------------------------------------
    if raw_results:
        user_prompt = _USER_TEMPLATE.format(
            section_title=section_title,
            section_description=section_description,
            sources_json=_sources_json(raw_results),
        )
        llm = get_llm("scoring", 0.0)
        messages = [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=user_prompt),
        ]
        try:
            response = await llm.ainvoke(messages)
            llm_scores = _parse_llm_scores(response.content)
        except Exception as exc:
            logger.error(
                "scorer: LLM call failed — %s (section_id=%s)", exc, section_id
            )
            # Non-fatal: default scores applied below.

    # ------------------------------------------------------------------
    # Build SourceScore records
    # ------------------------------------------------------------------
    tenant_id: str = state.get("tenant_id", "")
    source_scores: list[SourceScore] = []

    for idx, r in enumerate(raw_results, start=1):
        llm_s = llm_scores.get(idx, {})
        relevance = float(llm_s.get("relevance", 0.5))
        recency = float(llm_s.get("recency", 0.5))
        authenticity = _authenticity_for(r.get("url", ""), verified_urls)
        composite = (
            _W_RELEVANCE * relevance
            + _W_RECENCY * recency
            + _W_AUTHENTICITY * authenticity
        )
        source_scores.append(
            SourceScore(
                url_hash=_url_hash(r.get("url", "")),
                url=r.get("url", ""),
                domain=_extract_domain(r.get("url", "")),
                tenant_id=tenant_id,
                section_id=section_id,
                source_type=r["source_type"],
                authenticity_score=authenticity,
                relevance_score=relevance,
                recency_score=recency,
                composite_score=composite,
                scored_at=now_iso,
            )
        )

    # ------------------------------------------------------------------
    # Determine section status
    # ------------------------------------------------------------------
    if not compressed_text:
        status = "failed"
        error: str | None = "compressor produced no output"
    elif len(raw_results) < state.get("max_sources", 1) // 2:
        status = "partial"
        error = f"only {len(raw_results)} of {state.get('max_sources', 0)} sources found"
    else:
        status = "completed"
        error = None

    section_result = SectionResult(
        section_id=section_id,
        section_title=section_title,
        status=status,
        source_count=len(raw_results),
        search_iterations_used=search_iteration,
        error=error,
    )

    # ------------------------------------------------------------------
    # Wrap compressed_text as CompressedFinding for ExecutionState reducer
    # ------------------------------------------------------------------
    source_types_used: list[SourceType] = list(
        {r["source_type"] for r in raw_results}
    )
    token_estimate = len(compressed_text.split()) * 4 // 3

    compressed_finding = CompressedFinding(
        section_id=section_id,
        section_title=section_title,
        compressed_text=compressed_text,
        source_types_used=source_types_used,
        token_estimate=token_estimate,
    )

    logger.info(
        "scorer: section status=%r, %d source(s), %d citation(s) (section_id=%s)",
        status,
        len(raw_results),
        len(citations),
        section_id,
    )

    return {
        # Internal SectionResearchState fields
        "source_scores": source_scores,
        "section_findings": compressed_text,
        "section_citations": citations,
        "section_source_scores": source_scores,
        # Output-schema fields — merged into ExecutionState via operator.add reducers
        "section_results": [section_result],
        "compressed_findings": [compressed_finding],
        "citations": citations,
    }
