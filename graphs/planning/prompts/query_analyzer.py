"""
Prompts for query_analyzer node — planning graph.

Template variables
------------------
_USER_PROMPT_TEMPLATE:
  {topic}               : state["original_topic"]
  {context_brief}       : state["context_brief"] or fallback string
  {clarification_text}  : formatted Q&A from previous clarification rounds
"""
from __future__ import annotations

_SYSTEM_PROMPT = """\
You are the Query Analyzer for Kadal AI's deep research system.
You receive a context brief (a prose summary of the user's conversation history
and uploaded files) plus the user's research topic and any clarification answers.

Your job: decide whether the request is specific enough to plan, or whether
clarifying questions are needed first.

Output a single JSON object — no markdown fences, no commentary.
"""

_USER_PROMPT_TEMPLATE = """\
## Research Topic
{topic}

## Context Brief (summary of conversation and uploaded files)
{context_brief}

## Previous Clarification Q&A (if any)
{clarification_text}

---

Analyse the research request and return a JSON object with EXACTLY these keys:

{{
  "needs_clarification": <true | false>,
  "clarification_questions": ["<question 1>", "<question 2>"],
  "refined_topic": "<clear, specific research question in 1-2 sentences>",
  "depth_of_research": "<surface | intermediate | in-depth>",
  "audience": "<who will read this report>",
  "objective": "<what the report should achieve>",
  "domain": "<primary subject-matter domain>",
  "recency_scope": "<all_time | last_5_years | last_2_years | last_6_months | last_month>",
  "source_scope": ["web", "arxiv", "content_lake", "files"],
  "assumptions": ["<assumption 1>", "..."]
}}

CLARIFICATION RULES — set "needs_clarification": true if ANY apply:
  1. Topic is fewer than 4 words AND context_brief does not resolve the ambiguity.
  2. Topic is generic or overly broad (e.g. "AI", "Technology") AND no domain
     can be inferred from context.
  3. Multiple plausible research interpretations exist with no clear signal.
  4. No identifiable research objective.

When needs_clarification is true:
  - Provide 1-3 focused questions in "clarification_questions" (max 3).
  - Still fill in as many other fields as you CAN confidently infer from context.
  - Leave fields you cannot infer as empty strings / default values.

When needs_clarification is false:
  - "clarification_questions" must be an empty list [].
  - All other fields must be fully populated.

source_scope must only include sources from: web, tavily, arxiv, content_lake, files.
"web" is ALWAYS included.
"tavily" is a premium web search provider — include it only if it was already in the
  user's requested tools (do not add it on your own).
Include "files" only if the user has uploaded files (context brief will mention them).
"""
