"""
supervisor — Execution Graph Node (Layer 1, Node 1)

Responsibility
--------------
Orchestrates the parallel section research phase in two operating modes:

Phase 1 — Initial Dispatch  (section_results is empty)
  No LLM call.  Updates status to "researching".  The dispatch_sections
  routing function (also in this module) reads state["sections"] and returns
  a list[Send] to fan-out one section_subgraph per plan section.

Phase 2 — Reflection  (section_results is populated)
  Calls Gemini 2.5 Pro to analyse the compressed findings and identify
  knowledge gaps.  Returns updated reflection_count and knowledge_gaps.
  The dispatch_sections routing function then either:
    a) spawns targeted fill-in section_subgraphs (one per gap), or
    b) routes to "knowledge_fusion" when no more gaps are found or the
       loop cap is reached.

  Reflection is automatically skipped for surface / intermediate depth
  (max_sources_per_section < depth_in_depth threshold).  The supervisor node
  sets knowledge_gaps=[] and the routing function proceeds to knowledge_fusion.

Model: Gemini 2.5 Pro (reflection only — Phase 1 uses no LLM)

Loop cap: settings.loops.supervisor_reflection_max (default 2)
  Enforced inside dispatch_sections — the supervisor node itself does not
  guard the cap; it always runs the reflection when called in Phase 2.

Node contract
-------------
Input  fields consumed: section_results, compressed_findings, sections,
                        refined_topic, topic, reflection_count, knowledge_gaps,
                        max_sources_per_section, max_search_iterations,
                        file_contents, tools_enabled, tenant_id, job_id
Output fields written:  status (both phases)
                        reflection_count, knowledge_gaps (Phase 2 only)

dispatch_sections (conditional edge function exported from this module)
  Reads: section_results, knowledge_gaps, reflection_count, sections,
         max_search_iterations, max_sources_per_section, file_contents
  Returns: list[Send] targeting "section_subgraph", or "knowledge_fusion"

Graph wiring (graph.py):
  graph.add_node("supervisor", supervisor)
  graph.add_conditional_edges(
      "supervisor",
      dispatch_sections,
      {"knowledge_fusion": "knowledge_fusion"},
  )
  graph.add_edge("section_subgraph", "supervisor")   # fan-in
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Send

from app.config import settings
from app.llm_factory import get_llm
from graphs.execution.state import (
    CompressedFinding,
    ExecutionState,
    SectionResearchState,
    SectionResult,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_REFLECTION_SYSTEM_PROMPT = """\
You are a senior research director reviewing an AI-generated multi-source \
research report that is currently in progress.

You have access to compressed research findings per section and the completion \
status of each section.  Your task is to identify knowledge gaps — topics that \
are missing or insufficiently covered — that would meaningfully improve the \
final report.

Output a single JSON object — no markdown fences, no commentary.
"""

_REFLECTION_USER_TEMPLATE = """\
## Research Topic
{refined_topic}

## Planned Sections
{sections_list}

## Completed Research Findings
{findings_text}

## Failed or Partial Sections
{failed_sections}

---

Identify knowledge gaps in the current findings.

A knowledge gap is a topic or sub-topic that:
1. Is clearly relevant to the research topic.
2. Is missing or only superficially mentioned in the current findings.
3. Could realistically be addressed with additional targeted web / arxiv searches.

Return:
{{
  "knowledge_gaps": [
    "<gap 1: specific, 1-2 sentences describing what is missing and why it matters>",
    "..."
  ]
}}

Constraints:
- Maximum 3 gaps.  Prioritise the most impactful omissions.
- Return an empty list [] if the current findings are comprehensive.
- Do NOT invent gaps — only report genuine absences visible in the findings.
- Each gap description must be specific enough to guide a targeted search query.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_compression_target(max_sources: int) -> int:
    """Map max_sources to the depth-appropriate compression token budget."""
    if max_sources <= settings.depth_surface.max_sources_per_section:
        return settings.depth_surface.compression_target_tokens
    if max_sources <= settings.depth_intermediate.max_sources_per_section:
        return settings.depth_intermediate.compression_target_tokens
    return settings.depth_in_depth.compression_target_tokens


def _parse_reflection(raw: str) -> list[str]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    parsed = json.loads(text)
    return parsed.get("knowledge_gaps") or []


def _format_sections_list(sections: list[dict]) -> str:
    if not sections:
        return "(no sections)"
    return "\n".join(
        f"{i + 1}. {s.get('title', '')} — {s.get('description', '')}"
        for i, s in enumerate(sections)
    )


def _format_findings(findings: list[CompressedFinding]) -> str:
    if not findings:
        return "No findings available yet."
    lines = []
    for f in findings:
        snippet = f["compressed_text"][:1_500]
        truncated = "…" if len(f["compressed_text"]) > 1_500 else ""
        lines.append(
            f"### {f['section_title']}\n"
            f"Sources used: {', '.join(f['source_types_used'])}\n"
            f"{snippet}{truncated}"
        )
    return "\n\n".join(lines)


def _format_failed_sections(results: list[SectionResult]) -> str:
    failed = [r for r in results if r["status"] != "completed"]
    if not failed:
        return "None — all sections completed successfully."
    lines = []
    for r in failed:
        suffix = f" — {r['error']}" if r.get("error") else ""
        lines.append(f"- {r['section_title']}: {r['status']}{suffix}")
    return "\n".join(lines)


def _build_initial_section_state(
    state: ExecutionState, section: dict
) -> SectionResearchState:
    """Build the initial SectionResearchState for one plan section.

    The section dict comes directly from state["sections"] — it may be a raw
    dict (restored by the checkpointer) rather than a typed PlanSection.
    All field access uses .get() with safe defaults.
    """
    max_sources = state["max_sources_per_section"]
    return {
        "section_id": section.get("section_id") or str(uuid.uuid4()),
        "section_title": section.get("title", ""),
        "section_description": section.get("description", ""),
        "search_strategy": section.get("search_strategy", "web"),
        "tenant_id": state["tenant_id"],
        "max_search_iterations": state["max_search_iterations"],
        "max_sources": max_sources,
        "compression_target_tokens": _infer_compression_target(max_sources),
        "file_contents": state["file_contents"],
        "search_queries": [],
        "raw_search_results": [],
        "search_iteration": 0,
        "verified_urls": [],
        "compressed_text": "",
        "source_scores": [],
        "citations": [],
        "section_findings": "",
        "section_citations": [],
        "section_source_scores": [],
    }


def _build_gap_section_state(
    state: ExecutionState, gap: str, index: int
) -> SectionResearchState:
    """Build a targeted SectionResearchState for a knowledge gap fill-in.

    Gap sections use:
    - max_search_iterations reduced by 1 (minimum 1) — they are focused,
      not exhaustive.
    - max_sources halved (minimum 2) — fewer sources, higher precision.
    - search_strategy "web,arxiv" — broadest coverage for unknown gaps.
    """
    max_sources = max(2, state["max_sources_per_section"] // 2)
    return {
        "section_id": f"gap-{uuid.uuid4().hex[:8]}",
        "section_title": f"Gap Fill-in {index + 1}",
        "section_description": gap,
        "search_strategy": "web,arxiv",
        "tenant_id": state["tenant_id"],
        "max_search_iterations": max(1, state["max_search_iterations"] - 1),
        "max_sources": max_sources,
        "compression_target_tokens": _infer_compression_target(max_sources),
        "file_contents": state["file_contents"],
        "search_queries": [],
        "raw_search_results": [],
        "search_iteration": 0,
        "verified_urls": [],
        "compressed_text": "",
        "source_scores": [],
        "citations": [],
        "section_findings": "",
        "section_citations": [],
        "section_source_scores": [],
    }


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def supervisor(state: ExecutionState, config: RunnableConfig | None = None) -> dict[str, Any]:
    """LangGraph node — orchestrates parallel section research.

    Phase 1 (section_results empty):
        No LLM call.  Returns status="researching".  The dispatch_sections
        routing function handles Send() fan-out.

    Phase 2 (section_results populated):
        Calls Gemini 2.5 Pro to identify knowledge gaps in the collected
        findings.  Returns updated reflection_count and knowledge_gaps.
        dispatch_sections then spawns targeted sections or routes to
        knowledge_fusion based on the gaps.

        Reflection is skipped for surface / intermediate depth jobs
        (max_sources_per_section below the in-depth threshold).
    """
    job_id = state.get("job_id", "")
    section_results: list[SectionResult] = state.get("section_results") or []

    # ------------------------------------------------------------------
    # Phase 1: initial entry — no sections have run yet
    # ------------------------------------------------------------------
    if not section_results:
        sections = state.get("sections") or []
        logger.info(
            "supervisor: Phase 1 — %d section(s) to dispatch (job_id=%s)",
            len(sections),
            job_id,
        )
        if not sections:
            return {
                "status": "failed",
                "error": "supervisor: plan has no sections to research",
            }
        return {"status": "researching"}

    # ------------------------------------------------------------------
    # Phase 2: sections have completed — reflection round
    # ------------------------------------------------------------------
    reflection_count: int = state.get("reflection_count", 0)
    max_reflection: int = settings.loops.supervisor_reflection_max

    logger.info(
        "supervisor: Phase 2 — reflection round %d/%d (job_id=%s)",
        reflection_count + 1,
        max_reflection,
        job_id,
    )

    # Skip reflection for surface / intermediate depth to save cost.
    # Infer depth from max_sources_per_section vs the in-depth threshold.
    in_depth_threshold = settings.depth_in_depth.max_sources_per_section
    if state.get("max_sources_per_section", 0) < in_depth_threshold:
        logger.info(
            "supervisor: reflection skipped — not in-depth depth (job_id=%s)", job_id
        )
        return {
            "reflection_count": reflection_count + 1,
            "knowledge_gaps": [],
            "status": "fusing",
        }

    # In-depth: run LLM reflection
    sections = state.get("sections") or []
    user_prompt = _REFLECTION_USER_TEMPLATE.format(
        refined_topic=state.get("refined_topic") or state.get("topic", ""),
        sections_list=_format_sections_list(sections),
        findings_text=_format_findings(state.get("compressed_findings") or []),
        failed_sections=_format_failed_sections(section_results),
    )

    llm = get_llm("supervisor_reflection", 0.3)
    messages = [
        SystemMessage(content=_REFLECTION_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    try:
        response = await asyncio.wait_for(
            llm.ainvoke(messages), timeout=settings.llm_timeout_seconds
        )
        knowledge_gaps = _parse_reflection(response.content)
    except asyncio.TimeoutError:
        logger.error(
            "supervisor: reflection LLM timed out after %ds (job_id=%s)",
            settings.llm_timeout_seconds, job_id,
        )
        knowledge_gaps = []
    except Exception as exc:
        # Non-fatal: log and continue without gaps so the pipeline completes.
        logger.error(
            "supervisor: reflection LLM call failed — %s (job_id=%s)", exc, job_id
        )
        knowledge_gaps = []

    logger.info(
        "supervisor: reflection found %d gap(s) (job_id=%s): %s",
        len(knowledge_gaps),
        job_id,
        knowledge_gaps,
    )

    return {
        "reflection_count": reflection_count + 1,
        "knowledge_gaps": knowledge_gaps,
        "status": "reflecting",
    }


# ---------------------------------------------------------------------------
# Routing function (used as conditional edge in graph.py)
# ---------------------------------------------------------------------------

def dispatch_sections(state: ExecutionState) -> list[Send] | str:
    """Conditional edge function called after the supervisor node runs.

    Phase 1 (section_results empty):
        Fan-out — one Send("section_subgraph", ...) per plan section.

    Phase 2 (knowledge_gaps found, reflection cap not reached):
        Targeted fan-out — one Send("section_subgraph", ...) per gap.

    Otherwise:
        Return "knowledge_fusion" to continue the main pipeline.

    graph.py registers this as:
        graph.add_conditional_edges(
            "supervisor",
            dispatch_sections,
            {"knowledge_fusion": "knowledge_fusion"},
        )

    The string key "knowledge_fusion" must be present in the path_map.
    Send objects are dispatched directly by LangGraph without a path_map entry.
    """
    section_results: list[dict] = state.get("section_results") or []

    # ------------------------------------------------------------------
    # Phase 1: no sections have run yet — fan-out to all plan sections
    # ------------------------------------------------------------------
    if not section_results:
        sections = state.get("sections") or []
        if not sections:
            logger.warning(
                "dispatch_sections: empty sections list — routing to knowledge_fusion"
            )
            return "knowledge_fusion"

        sends = [
            Send("section_subgraph", _build_initial_section_state(state, section))
            for section in sections
        ]
        logger.info(
            "dispatch_sections: Phase 1 — dispatching %d section(s)", len(sends)
        )
        return sends

    # ------------------------------------------------------------------
    # Phase 2+: check if reflection produced actionable gaps
    # ------------------------------------------------------------------
    knowledge_gaps: list[str] = state.get("knowledge_gaps") or []
    reflection_count: int = state.get("reflection_count", 0)
    max_reflection: int = settings.loops.supervisor_reflection_max

    if knowledge_gaps and reflection_count < max_reflection:
        sends = [
            Send("section_subgraph", _build_gap_section_state(state, gap, i))
            for i, gap in enumerate(knowledge_gaps)
        ]
        logger.info(
            "dispatch_sections: Phase 2 — dispatching %d targeted section(s) for gaps",
            len(sends),
        )
        return sends

    # No further research needed — proceed to knowledge fusion
    logger.info(
        "dispatch_sections: no further dispatch (gaps=%d, reflection=%d/%d) "
        "— routing to knowledge_fusion",
        len(knowledge_gaps),
        reflection_count,
        max_reflection,
    )
    return "knowledge_fusion"
