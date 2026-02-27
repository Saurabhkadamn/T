"""
section_subgraph — Execution Graph (Section Sub-graph)

Wires the five section-research nodes into a compiled StateGraph that runs
as an isolated unit per research section.  The supervisor spawns one
section_subgraph per plan section (plus gap fill-ins) via LangGraph's
Send() API.

Node pipeline
-------------
START
  └── search_query_gen     (generate queries for this iteration)
        └── searcher       (parallel tool calls; increments search_iteration)
              ├──[iteration < max]── search_query_gen   (loop back)
              └──[iteration >= max]── source_verifier   (web URL checks)
                    └── compressor   (Flash LLM — compress all results)
                          └── scorer (Flash LLM — score + package outputs)
                                └── END

Output schema
-------------
When the subgraph reaches END, only the fields in SectionSubgraphOutput are
merged into the parent ExecutionState.  All four fields map directly to
ExecutionState reducer fields (Annotated with operator.add), so outputs from
parallel sections accumulate without overwriting each other.

  section_results:     list[SectionResult]       → ExecutionState.section_results
  compressed_findings: list[CompressedFinding]   → ExecutionState.compressed_findings
  citations:           list[Citation]            → ExecutionState.citations
  source_scores:       list[SourceScore]         → ExecutionState.source_scores

Graph wiring in parent (graphs/execution/graph.py):
  graph.add_node("section_subgraph", section_subgraph)
  graph.add_edge("section_subgraph", "supervisor")   # fan-in
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from graphs.execution.nodes.compressor import compressor
from graphs.execution.nodes.scorer import scorer
from graphs.execution.nodes.search_query_gen import search_query_gen
from graphs.execution.nodes.searcher import searcher
from graphs.execution.nodes.source_verifier import source_verifier
from graphs.execution.state import (
    Citation,
    CompressedFinding,
    SectionResearchState,
    SectionResult,
    SourceScore,
)


# ---------------------------------------------------------------------------
# Output schema — merged into ExecutionState by the parent graph
# ---------------------------------------------------------------------------

class SectionSubgraphOutput(TypedDict):
    """Output schema for the section_subgraph.

    Plain lists — reducer annotations (operator.add) belong only in
    ExecutionState, not in the output schema.  LangGraph uses this TypedDict
    to know which fields to extract from the subgraph's final state when
    merging into the parent ExecutionState via its Annotated reducer fields.
    """
    section_results: list[SectionResult]
    compressed_findings: list[CompressedFinding]
    citations: list[Citation]
    source_scores: list[SourceScore]


# ---------------------------------------------------------------------------
# Routing function (search iteration loop)
# ---------------------------------------------------------------------------

def _should_continue_searching(state: SectionResearchState) -> str:
    """Route back to search_query_gen if more iterations remain; else verify."""
    if state.get("search_iteration", 0) < state.get("max_search_iterations", 1):
        return "search_query_gen"
    return "source_verifier"


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

def _build_section_graph() -> StateGraph:
    graph = StateGraph(SectionResearchState, output=SectionSubgraphOutput)

    # Nodes
    graph.add_node("search_query_gen", search_query_gen)
    graph.add_node("searcher", searcher)
    graph.add_node("source_verifier", source_verifier)
    graph.add_node("compressor", compressor)
    graph.add_node("scorer", scorer)

    # Entry
    graph.add_edge(START, "search_query_gen")
    graph.add_edge("search_query_gen", "searcher")

    # Iteration loop or proceed to verification
    graph.add_conditional_edges(
        "searcher",
        _should_continue_searching,
        {
            "search_query_gen": "search_query_gen",
            "source_verifier": "source_verifier",
        },
    )

    # Final pipeline (runs once after all iterations)
    graph.add_edge("source_verifier", "compressor")
    graph.add_edge("compressor", "scorer")
    graph.add_edge("scorer", END)

    return graph


# Compile once at import time.
_graph = _build_section_graph()
section_subgraph = _graph.compile()
