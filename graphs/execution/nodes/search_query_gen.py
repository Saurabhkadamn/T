"""
search_query_gen — Execution Graph Node (Section Sub-graph, Node 1)

Responsibility
--------------
Generate a set of search queries for one section of the research plan.
Called at the start of each search iteration inside a section sub-graph.

On iteration 0 (first run):
  Generates fresh queries tailored to the section title, description, and
  search_strategy (which source types will be searched).

On iteration > 0 (subsequent runs):
  Generates DIVERSE queries that do not repeat previous searches.  The node
  receives a brief summary of result titles already collected so the model
  can explore different angles, terminology, and sub-topics.

Iteration counter management:
  This node does NOT increment search_iteration.  The searcher node owns that
  counter and increments it after each complete search round.

Model: Gemini 2.5 Flash  (mechanical task — high-volume, cost-sensitive)

Query count heuristic:
  ceil(max_sources / max_search_iterations), clamped to [2, 5].
  Examples:
    surface      (3 sources, 1 iter)  → 3 queries
    intermediate (5 sources, 2 iters) → 3 queries per iter
    in-depth     (10 sources, 3 iters)→ 4 queries per iter

Search strategy → query style:
  "web"          → conversational, SEO-friendly phrases
  "arxiv"        → technical, academic paper search terms
  "content_lake" → internal doc / product-specific phrasing
  "files"        → no queries generated (file content loaded directly)

Node contract
-------------
Input  fields consumed: section_title, section_description, search_strategy,
                        search_iteration, max_search_iterations, max_sources,
                        search_queries (previous rounds),
                        raw_search_results (for diversity on iter > 0)
Output fields written:  search_queries (replaces previous list — no reducer)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.config import settings
from app.llm_factory import get_llm
from graphs.execution.state import RawSearchResult, SectionResearchState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a research query specialist for an AI deep-research pipeline.
Your task is to generate precise, diverse search queries for one section of a \
research report.

Output a single JSON object — no markdown fences, no commentary.
"""

_USER_PROMPT_TEMPLATE = """\
## Section to Research
**Title:** {section_title}
**Description:** {section_description}

## Search Sources in Scope
{strategy_guidance}

## Search Context
- Iteration: {iteration} of {max_iterations}
- Target sources to find: {max_sources}
- Queries to generate this iteration: {query_count}

{previous_context}

---

Generate exactly {query_count} search queries for this section.

Query guidelines:
- Each query must be unique — no overlap in meaning or phrasing.
- Prioritise precision: specific queries outperform vague ones.
- Vary the angle: use synonyms, sub-topics, or complementary perspectives.
- Match the style to the source (web = conversational, arxiv = technical).

Return:
{{
  "queries": [
    "<query 1>",
    "<query 2>",
    ...
  ]
}}
"""

_PREVIOUS_CONTEXT_TEMPLATE = """\
## Already Searched (avoid repeating these angles)
Previous queries used:
{prev_queries}

Result titles already collected (do NOT generate queries that would return the same results):
{result_titles}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _query_count(max_sources: int, max_iterations: int) -> int:
    """Queries per iteration: ceil(max_sources / max_iterations), clamped [2, 5]."""
    raw = math.ceil(max_sources / max(1, max_iterations))
    return max(2, min(5, raw))


def _strategy_guidance(search_strategy: str) -> str:
    """Build a human-readable description of what source types are in scope."""
    types = [t.strip().lower() for t in search_strategy.split(",") if t.strip()]
    descriptions: dict[str, str] = {
        "web": "Web search (Tavily / Serper) — use conversational, SEO-friendly queries "
               "as a person would type into a search engine.",
        "arxiv": "Arxiv academic papers — use technical terminology, field-specific jargon, "
                 "and paper-title-like phrasing.",
        "content_lake": "Internal content lake (org documentation) — use product names, "
                        "internal terminology, and process-specific language.",
        "files": "Uploaded files — file content is loaded directly; no queries needed for files.",
    }
    active = [descriptions[t] for t in types if t in descriptions]
    if not active:
        return "- Web search (default)"
    return "\n".join(f"- {d}" for d in active)


def _format_previous_context(
    prev_queries: list[str],
    raw_results: list[RawSearchResult],
) -> str:
    """Format what has already been searched for diversity prompting."""
    if not prev_queries and not raw_results:
        return ""  # Iteration 0 — no context needed

    query_lines = "\n".join(f"  {i + 1}. {q}" for i, q in enumerate(prev_queries)) or "  (none)"
    title_lines = "\n".join(
        f"  - {r.get('title', '(no title)')[:120]}"
        for r in raw_results[:15]   # cap at 15 to stay within prompt budget
    ) or "  (none)"

    return _PREVIOUS_CONTEXT_TEMPLATE.format(
        prev_queries=query_lines,
        result_titles=title_lines,
    )


def _parse_response(raw: str) -> list[str]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    parsed = json.loads(text)
    queries = parsed.get("queries") or []
    return [str(q).strip() for q in queries if str(q).strip()]


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def search_query_gen(
    state: SectionResearchState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """LangGraph node: generate search queries for one section iteration.

    Returns a partial SectionResearchState dict with updated search_queries.
    Does NOT increment search_iteration — that is the searcher's responsibility.
    """
    section_id = state.get("section_id", "")
    section_title = state.get("section_title", "")
    section_description = state.get("section_description", "")
    search_strategy: str = state.get("search_strategy", "web")
    search_iteration: int = state.get("search_iteration", 0)
    max_iterations: int = state.get("max_search_iterations", 1)
    max_sources: int = state.get("max_sources", 3)
    prev_queries: list[str] = state.get("search_queries") or []
    raw_results: list[RawSearchResult] = state.get("raw_search_results") or []

    query_count = _query_count(max_sources, max_iterations)
    strategy_guide = _strategy_guidance(search_strategy)
    previous_context = _format_previous_context(prev_queries, raw_results)

    user_prompt = _USER_PROMPT_TEMPLATE.format(
        section_title=section_title,
        section_description=section_description,
        strategy_guidance=strategy_guide,
        iteration=search_iteration + 1,     # 1-indexed for human readability
        max_iterations=max_iterations,
        max_sources=max_sources,
        query_count=query_count,
        previous_context=previous_context,
    )

    llm = get_llm("query_gen", 0.4)  # slightly higher — diversity is desirable
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    logger.info(
        "search_query_gen: iteration %d/%d — generating %d queries "
        "(section_id=%s title=%r)",
        search_iteration + 1,
        max_iterations,
        query_count,
        section_id,
        section_title,
    )

    try:
        response = await asyncio.wait_for(
            llm.ainvoke(messages), timeout=settings.llm_timeout_seconds
        )
        queries = _parse_response(response.content)
    except asyncio.TimeoutError:
        logger.error(
            "search_query_gen: LLM timed out after %ds (section_id=%s)",
            settings.llm_timeout_seconds, section_id,
        )
        queries = [section_title] if section_title else []
    except Exception as exc:
        logger.error(
            "search_query_gen: failed to generate queries — %s (section_id=%s)",
            exc,
            section_id,
        )
        # Fallback: use the section title as a single query so the searcher
        # can still attempt to find something useful.
        queries = [section_title] if section_title else []

    # Enforce the target count: truncate if LLM returned too many.
    if len(queries) > query_count:
        queries = queries[:query_count]

    logger.info(
        "search_query_gen: produced %d query/queries (section_id=%s): %s",
        len(queries),
        section_id,
        queries,
    )

    return {"search_queries": queries}
