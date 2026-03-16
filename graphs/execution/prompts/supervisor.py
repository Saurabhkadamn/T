"""
Prompts for supervisor node — execution graph.

Template variables
------------------
_REFLECTION_USER_TEMPLATE:
  {refined_topic}    : state["refined_topic"] or state["topic"]
  {sections_list}    : _format_sections_list(state["sections"])
  {findings_text}    : _format_findings(state["compressed_findings"])
  {failed_sections}  : _format_failed_sections(state["section_results"])
"""
from __future__ import annotations

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
