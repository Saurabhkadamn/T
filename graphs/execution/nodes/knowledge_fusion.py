"""
knowledge_fusion — Execution Graph Node (Main pipeline, Node 2)

Responsibility
--------------
Synthesise all section compressed findings into a single coherent knowledge
base, resolving conflicts between sources according to the trust hierarchy:
  1. files (highest trust — user-uploaded, organisation-specific)
  2. content_lake (verified internal org documentation)
  3. arxiv (peer-reviewed academic)
  4. web (broadest coverage, lowest verified trust)

When sources conflict, the higher-trust source is cited as authoritative;
the lower-trust source is preserved for context with an explicit conflict note.

Output is a single fused_knowledge string used by report_writer as its
primary input.  The string is structured prose — not JSON — so the report
writer can render it directly into HTML sections.

Model: Gemini 2.5 Pro (reasoning task — conflict resolution and synthesis)

Node contract
-------------
Input  fields consumed: compressed_findings (list[CompressedFinding]),
                        citations (list[Citation]), source_scores
                        (list[SourceScore]), knowledge_gaps (from supervisor
                        reflection), plan, checklist, refined_topic, topic
Output fields written:  fused_knowledge, status="fusing"
"""

from __future__ import annotations
import asyncio
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.config import settings
from app.llm_factory import get_llm
from app.tracing import node_span
from graphs.execution.state import (
    Citation,
    CompressedFinding,
    ExecutionState,
    SourceType,
)

logger = logging.getLogger(__name__)

_TRUST_LABELS: dict[SourceType, str] = {
    "files": "uploaded file (highest trust)",
    "content_lake": "internal content lake",
    "arxiv": "peer-reviewed academic paper",
    "web": "web source",
}

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
You are a senior research analyst synthesising multi-source research findings \
into a unified knowledge base for an AI-generated research report.

Your responsibilities:
1. Integrate findings from all sections into coherent, well-structured prose.
2. When sources conflict, apply the trust hierarchy (files > content_lake \
> arxiv > web):
   - Cite the higher-trust source as authoritative.
   - Preserve the lower-trust claim with a note, e.g. "[Web sources suggest X, \
but internal documentation confirms Y]".
3. Explicitly address any knowledge gaps identified by the research supervisor.
4. Organise output by research section — use clear section headers.
5. Attribute key claims to their source type (e.g. "Per internal docs…", \
"Academic research shows…").
6. Do NOT fabricate facts or fill gaps with speculation.

Output structured prose with section headers (use ## for section names).
Do not include preamble or meta-commentary — start directly with the content.
"""

_USER_TEMPLATE = """\
## Research Topic
{topic}

## Research Plan — Section Overview
{sections_overview}

## Quality Checklist (must be addressed in the final report)
{checklist}

## Knowledge Gaps to Address (from supervisor reflection)
{knowledge_gaps}

## Research Findings by Section
(Trust hierarchy: files=1 > content_lake=2 > arxiv=3 > web=4)

{findings_block}

---

Synthesise the above findings into a unified knowledge base.
Resolve conflicts using the trust hierarchy.
Address all knowledge gaps where possible.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_sections_overview(sections: list[dict]) -> str:
    if not sections:
        return "(no sections)"
    return "\n".join(
        f"{i+1}. **{s.get('title', '')}** — {s.get('description', '')}"
        for i, s in enumerate(sections)
    )


def _format_checklist(checklist: list[str]) -> str:
    if not checklist:
        return "(no checklist provided)"
    return "\n".join(f"- {item}" for item in checklist)


def _format_knowledge_gaps(gaps: list[str]) -> str:
    if not gaps:
        return "None — research is considered comprehensive."
    return "\n".join(f"- {gap}" for gap in gaps)


def _format_findings(
    findings: list[CompressedFinding],
    citations: list[Citation],
) -> str:
    """Format compressed findings with their trust tiers and citation context."""
    if not findings:
        return "(no findings available)"

    # Build a citation index per section for quick lookup.
    cites_by_section: dict[str, list[Citation]] = {}
    for c in citations:
        cites_by_section.setdefault(c["section_id"], []).append(c)

    # Sort findings so higher-trust sources appear first within each section.
    def _min_trust(f: CompressedFinding) -> int:
        tiers = [
            _TRUST_TIER.get(st, 4)
            for st in f.get("source_types_used", [])
        ]
        return min(tiers) if tiers else 4

    sorted_findings = sorted(findings, key=_min_trust)

    blocks: list[str] = []
    for f in sorted_findings:
        types_used = f.get("source_types_used", [])
        trust_labels = ", ".join(
            _TRUST_LABELS.get(t, t) for t in sorted(types_used, key=lambda x: _TRUST_TIER.get(x, 4))
        )
        header = f"### {f['section_title']}  (sources: {trust_labels})"
        content = f["compressed_text"]

        # Append a brief citation summary.
        sec_cites = cites_by_section.get(f["section_id"], [])
        if sec_cites:
            cite_lines = [
                f"  [{c['trust_tier']}] {c['title'] or c['url'] or '(unnamed)'}"
                for c in sorted(sec_cites, key=lambda x: x["trust_tier"])[:5]
            ]
            cites_str = "Key sources:\n" + "\n".join(cite_lines)
            blocks.append(f"{header}\n{content}\n\n{cites_str}")
        else:
            blocks.append(f"{header}\n{content}")

    return "\n\n---\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

@node_span("knowledge_fusion")
async def knowledge_fusion(
    state: ExecutionState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """LangGraph node: synthesise all section findings into fused knowledge.

    Reads compressed_findings (list[CompressedFinding]) from ExecutionState —
    these were accumulated by the section sub-graph reducer (operator.add).
    """
    job_id: str = state.get("job_id", "")
    topic: str = state.get("refined_topic") or state.get("topic", "")
    sections: list[dict] = state.get("sections") or []
    checklist: list[str] = state.get("checklist") or []
    knowledge_gaps: list[str] = state.get("knowledge_gaps") or []
    compressed_findings: list[CompressedFinding] = state.get("compressed_findings") or []
    citations: list[Citation] = state.get("citations") or []

    logger.info(
        "knowledge_fusion: synthesising %d section finding(s) (job_id=%s)",
        len(compressed_findings),
        job_id,
    )

    if not compressed_findings:
        logger.error(
            "knowledge_fusion: no findings to fuse — aborting (job_id=%s)", job_id
        )
        return {
            "fused_knowledge": None,
            "status": "failed",
            "error": "knowledge_fusion: no compressed findings available",
        }

    user_prompt = _USER_TEMPLATE.format(
        topic=topic,
        sections_overview=_format_sections_overview(sections),
        checklist=_format_checklist(checklist),
        knowledge_gaps=_format_knowledge_gaps(knowledge_gaps),
        findings_block=_format_findings(compressed_findings, citations),
    )

    llm = get_llm("knowledge_fusion", 0.3)
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    try:
        response = await asyncio.wait_for(
            llm.ainvoke(messages, config=config), timeout=settings.llm_timeout_seconds
        )
        fused_knowledge: str = response.content.strip()
    except asyncio.TimeoutError:
        logger.error(
            "knowledge_fusion: LLM timed out after %ds (job_id=%s)",
            settings.llm_timeout_seconds, job_id,
        )
        fused_knowledge = "\n\n".join(
            f"## {f['section_title']}\n{f['compressed_text']}"
            for f in compressed_findings
        )
    except Exception as exc:
        logger.error(
            "knowledge_fusion: LLM call failed — %s (job_id=%s)", exc, job_id
        )
        # Fallback: concatenate compressed findings as-is.
        fused_knowledge = "\n\n".join(
            f"## {f['section_title']}\n{f['compressed_text']}"
            for f in compressed_findings
        )

    logger.info(
        "knowledge_fusion: produced %d chars of fused knowledge (job_id=%s)",
        len(fused_knowledge),
        job_id,
    )

    return {
        "fused_knowledge": fused_knowledge,
        "status": "writing",
    }
