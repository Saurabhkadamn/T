"""
Prompts for scorer node — execution graph.

Template variables
------------------
_USER_TEMPLATE:
  {section_title}       : state["section_title"]
  {section_description} : state["section_description"]
  {sources_json}        : _sources_json(raw_results)
"""
from __future__ import annotations

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

For each source, return relevance (0.0-1.0) and recency (0.0-1.0):
  - relevance: how directly relevant is this source to the section topic?
  - recency: how recent?  1.0 = published in the last year, 0.5 = 3-5 years
    ago, 0.0 = unknown or >10 years.

Return:
{{
  "scores": [
    {{"id": <integer>, "relevance": <float>, "recency": <float>}},
    ...
  ]
}}
"""
