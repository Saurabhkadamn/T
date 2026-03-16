"""
Prompts for knowledge_fusion node — execution graph.

Template variables
------------------
_USER_TEMPLATE:
  {topic}            : state["refined_topic"] or state["topic"]
  {sections_overview}: _format_sections_overview(state["sections"])
  {checklist}        : _format_checklist(state["checklist"])
  {knowledge_gaps}   : _format_knowledge_gaps(state["knowledge_gaps"])
  {findings_block}   : _format_findings(compressed_findings, citations, source_scores)
"""
from __future__ import annotations

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
5. Attribute key claims to their source type (e.g. "Per internal docs...", \
"Academic research shows...").
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
