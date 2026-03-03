"""
Planning Graph — graphs/planning/graph.py

Wires the three planning nodes into a compiled LangGraph StateGraph.

Flow
----

  START
    │
    ▼  (conditional: _route_entry)
    ├─► [no context_brief]        → context_builder
    ├─► [context_brief, no plan]  → query_analyzer
    └─► [context_brief + plan]    → plan_creator   (Mode 3 revision)

  context_builder
    │
    ▼
  query_analyzer
    │
    ├─► [status=failed]                          → END
    ├─► [needs_clarification AND count < cap]    → END  (surface questions)
    └─► [otherwise]                              → plan_creator

  plan_creator
    │
    ▼
  END  (status=awaiting_approval — human reviews via /run)

Invocation modes (determined by plan.py before calling ainvoke):
  Mode 1 (fresh):       state has no context_brief → enters at context_builder
  Mode 2 (clarify):     state has context_brief, no plan → enters at query_analyzer
  Mode 3 (revise plan): state has context_brief + plan → enters at plan_creator

Loop caps (from settings.loops):
  - clarification_max:  clarification_count < clarification_max
    (plan_revision_max is enforced in plan.py BEFORE the graph runs)
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.config import settings
from graphs.planning.nodes.context_builder import context_builder
from graphs.planning.nodes.query_analyzer import query_analyzer
from graphs.planning.nodes.plan_creator import plan_creator
from graphs.planning.state import PlanningState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Entry routing
# ---------------------------------------------------------------------------

def _route_entry(state: PlanningState) -> str:
    """Determine which node to enter based on state shape.

    Mode 1 — no context_brief yet (fresh request)   → context_builder
    Mode 2 — brief exists, no plan yet               → query_analyzer
    Mode 3 — brief + plan present (plan revision)    → plan_creator
    """
    if not state.get("context_brief"):
        return "context_builder"
    if state.get("plan") is None:
        return "query_analyzer"
    return "plan_creator"


# ---------------------------------------------------------------------------
# Post-node routing
# ---------------------------------------------------------------------------

def _route_after_query_analyzer(state: PlanningState) -> str:
    """After query_analyzer: surface clarification questions or proceed."""
    if state.get("status") == "failed":
        return END

    needs = state.get("needs_clarification", False)
    count = state.get("clarification_count", 0)
    cap = settings.loops.clarification_max

    if needs and count < cap:
        logger.debug(
            "route: needs_clarification=True (count=%d/%d) → END (surface questions)",
            count, cap,
        )
        return END  # graph pauses; API returns questions to caller

    if needs and count >= cap:
        logger.info(
            "route: clarification cap reached (%d/%d) → plan_creator with best-effort context",
            count, cap,
        )

    return "plan_creator"


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def _build_planning_graph() -> Any:
    """Construct and compile the Planning StateGraph.

    Returns a compiled LangGraph runnable.  The caller (FastAPI endpoint)
    invokes it with::

        result = await planning_graph.ainvoke(
            initial_state,
            config={"configurable": {
                "thread_id": job_id,
                "redis_client": redis,
                "mongo_client": mongo,
            }},
        )
    """
    graph = StateGraph(PlanningState)

    # Register nodes (3 total)
    graph.add_node("context_builder", context_builder)
    graph.add_node("query_analyzer", query_analyzer)
    graph.add_node("plan_creator", plan_creator)

    # Conditional entry from START based on state shape
    graph.add_conditional_edges(
        START,
        _route_entry,
        {
            "context_builder": "context_builder",
            "query_analyzer": "query_analyzer",
            "plan_creator": "plan_creator",
        },
    )

    # context_builder always feeds query_analyzer
    graph.add_edge("context_builder", "query_analyzer")

    # query_analyzer: clarify (END) or proceed to plan_creator
    graph.add_conditional_edges(
        "query_analyzer",
        _route_after_query_analyzer,
        {
            "plan_creator": "plan_creator",
            END: END,
        },
    )

    # plan_creator always exits (human reviews externally)
    graph.add_edge("plan_creator", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Module-level compiled graph — import this in the API layer
# ---------------------------------------------------------------------------
# Usage in FastAPI endpoint:
#
#   from graphs.planning.graph import planning_graph
#
#   result = await planning_graph.ainvoke(
#       initial_state,
#       config={"configurable": {
#           "thread_id": job_id,
#           "redis_client": redis,
#           "mongo_client": mongo,
#       }},
#   )

planning_graph = _build_planning_graph()
