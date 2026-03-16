"""
Prompts for compressor node — execution graph.

Template variables
------------------
_USER_TEMPLATE:
  {section_title}       : state["section_title"]
  {section_description} : state["section_description"]
  {target_tokens}       : state["compression_target_tokens"]
  {sources_block}       : _build_sources_block(usable, max_chars_per_source)
"""
from __future__ import annotations

_SYSTEM_PROMPT = """\
You are a research analyst compressing raw search results into a concise, \
factual summary for an AI-generated research report.

Your task:
1. Read all labelled sources below.
2. Produce a dense, fact-rich summary that covers the most important findings.
3. Attribute key claims to their source label (e.g. "According to [S2]...").
4. Stay within the specified token budget by omitting redundant or \
low-quality information.
5. Do NOT include opinions, filler, or meta-commentary about the sources.
6. Output ONLY the compressed summary — no preamble, no markdown headers.
"""

_USER_TEMPLATE = """\
## Section to summarise
**Title:** {section_title}
**Description:** {section_description}

## Token budget
Target approximately {target_tokens} tokens in your response.

## Sources
{sources_block}

---

Produce the compressed summary now.
"""
