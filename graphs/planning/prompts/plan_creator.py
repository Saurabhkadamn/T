"""
Prompts for plan_creator node — planning graph.

Template variables
------------------
_FRESH_PLAN_PROMPT:
  {context_brief}      : state["context_brief"] or fallback
  {refined_topic}      : state["refined_topic"] or state["original_topic"]
  {domain}             : state["domain"]
  {audience}           : state["audience"]
  {objective}          : state["objective"]
  {depth}              : state["depth_of_research"]
  {max_sections}       : from _DEPTH_GUIDANCE[depth]
  {max_sources}        : from _DEPTH_GUIDANCE[depth]
  {recency_scope}      : state["recency_scope"]
  {source_scope}       : ", ".join(state["source_scope"])
  {assumptions_text}   : formatted assumptions list
  {clarification_text} : formatted Q&A from clarification rounds

_REVISION_PROMPT:
  {context_brief}      : state["context_brief"] or fallback
  {current_plan_json}  : json.dumps(state["plan"])
  {revision_count}     : state["plan_revision_count"]
  {plan_feedback}      : state["plan_feedback"]
"""
from __future__ import annotations

_SYSTEM_PROMPT = """\
You are a senior research planner for Kadal AI's deep research system.
Your job is to design a structured research plan that an AI research pipeline
will execute to produce a comprehensive HTML report.

Output a single JSON object — no markdown fences, no commentary.
"""

_FRESH_PLAN_PROMPT = """\
## Context Brief
{context_brief}

## Research Parameters

- **Topic:** {refined_topic}
- **Domain:** {domain}
- **Audience:** {audience}
- **Objective:** {objective}
- **Depth:** {depth} ({max_sections} sections, {max_sources} sources/section)
- **Recency:** {recency_scope}
- **Sources available:** {source_scope}

## Assumptions Made
{assumptions_text}

## Clarifications From User
{clarification_text}

---

Design a research plan with the right number of sections for a {depth} report.
Guidelines:
- surface (1-3 sections): overview, key findings, conclusion
- intermediate (4-6 sections): intro + thematic sections + conclusion
- in-depth (7-10 sections): comprehensive coverage with subsections possible

Each section must have a distinct research focus with minimal overlap.
The search_strategy field must be a comma-separated list from: {source_scope}
Note: if "tavily" is in the available sources, include it alongside "web" for
sections that benefit from premium web search (e.g. "web,tavily,arxiv").

Also produce a checklist of 3-5 quality standards the research execution must meet
(e.g. "Each section must cite at least 2 unique sources",
"Include quantitative data or metrics where available",
"Prioritise peer-reviewed sources for scientific claims").
These will be enforced during the AI research execution phase.

Return this JSON:
{{
  "title": "<report title>",
  "summary": "<2-4 sentence executive summary of the planned report>",
  "sections": [
    {{
      "section_id": "<generate a UUID v4>",
      "title": "<section heading>",
      "description": "<1-3 sentences on what this section covers>",
      "search_strategy": "<comma-separated source types>"
    }}
  ],
  "estimated_sources": <integer total across all sections>,
  "checklist": ["<gate 1>", "<gate 2>", "<gate 3>"]
}}
"""

_REVISION_PROMPT = """\
## Context Brief
{context_brief}

## Current Plan (to be revised)
{current_plan_json}

## Human Reviewer Feedback (revision round {revision_count})
{plan_feedback}

---

Revise the plan above based on the reviewer's feedback.
Make only the changes needed to address the feedback — do NOT rewrite the
entire plan unless the feedback explicitly requires it.

Preserve section_ids for sections that are not changed.
Generate new UUID v4 section_ids only for newly added sections.

Also produce an updated checklist of 3-5 quality standards the research execution must meet
(same standard as the original: source counts, data quality, recency, verifiability).
These will be enforced during the AI research execution phase.

Return the same JSON structure:
{{
  "title": "<report title>",
  "summary": "<2-4 sentence executive summary>",
  "sections": [
    {{
      "section_id": "<existing or new UUID v4>",
      "title": "<section heading>",
      "description": "<1-3 sentences>",
      "search_strategy": "<comma-separated source types>"
    }}
  ],
  "estimated_sources": <integer>,
  "checklist": ["<gate 1>", "<gate 2>", "<gate 3>"]
}}
"""
