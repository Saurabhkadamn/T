"""
Prompts for search_query_gen node — execution graph.

Template variables
------------------
_USER_PROMPT_TEMPLATE:
  {section_title}       : state["section_title"]
  {section_description} : state["section_description"]
  {strategy_guidance}   : _strategy_guidance(state["search_strategy"])
  {iteration}           : state["search_iteration"] + 1  (1-indexed)
  {max_iterations}      : state["max_search_iterations"]
  {max_sources}         : state["max_sources"]
  {query_count}         : _query_count(max_sources, max_iterations)
  {previous_context}    : _format_previous_context(...) or empty string

_PREVIOUS_CONTEXT_TEMPLATE:
  {prev_queries}        : formatted numbered list of previous queries
  {result_titles}       : formatted list of result titles already collected
"""
from __future__ import annotations

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
