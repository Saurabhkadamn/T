"""
Planning Graph — graphs/planning/graph.py

Wires the five planning nodes into a compiled LangGraph StateGraph.

Flow
----

  START
    │
    ▼
  context_builder          (loads file contents from Redis)
    │
    ▼
  query_analyzer           (enriches topic, sets needs_clarification)
    │
    ├─► [needs_clarification=True  AND  count < max]
    │         │
    │         ▼
    │       clarifier      (generates questions → API returns them to user)
    │         │
    │         └─► END      (graph pauses; re-invoked with answers)
    │
    └─► [needs_clarification=False OR count >= max]
              │
              ▼
            plan_creator   (creates / revises plan)
              │
              ▼
            plan_reviewer  (self-reviews, approve or revise)
              │
              ├─► [verdict=revise AND revision_count < max]
              │         │
              │         └─► plan_creator  (loop back)
              │
              └─► [verdict=approve OR revision_count >= max]
                        │
                        ▼
                      END  (status=awaiting_approval, plan returned to API)

Loop caps (from settings.loops):
  - clarification:    clarification_count  < clarification_max
  - plan revision:    plan_revision_count  < plan_refinement_max
  - plan self-review: self_review_count    < plan_self_review_max
    (self_review cap is checked inside the revision routing — if the
     reviewer has hit its cap, the graph accepts the plan as-is)
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.config import settings
from graphs.planning.nodes.context_builder import context_builder
from graphs.planning.nodes.query_analyzer import query_analyzer
from graphs.planning.nodes.clarifier import clarifier
from graphs.planning.nodes.plan_creator import plan_creator
from graphs.planning.nodes.plan_reviewer import plan_reviewer
from graphs.planning.state import PlanningState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Routing functions
# ---------------------------------------------------------------------------

def route_after_query_analyzer(state: PlanningState) -> str:
    """After query_analyzer: go to clarifier or plan_creator."""
    if state.get("status") == "failed":
        return END

    needs = state.get("needs_clarification", False)
    count = state.get("clarification_count", 0)
    cap = settings.loops.clarification_max

    if needs and count < cap:
        logger.debug(
            "route: needs_clarification=True (count=%d/%d) → clarifier",
            count, cap,
        )
        return "clarifier"

    if needs and count >= cap:
        logger.info(
            "route: clarification cap reached (%d/%d) → plan_creator with best-effort context",
            count, cap,
        )

    return "plan_creator"


def route_after_clarifier(state: PlanningState) -> str:
    """After clarifier: always END to surface questions to the API caller."""
    if state.get("status") == "failed":
        return END
    # Graph pauses here; the API returns questions to user.
    # Re-invoked with clarification_answers populated → resumes at START.
    return END


def route_after_plan_reviewer(state: PlanningState) -> str:
    """After plan_reviewer: loop back to plan_creator or finish."""
    if state.get("status") == "failed":
        return END

    if state.get("status") == "awaiting_approval":
        return END

    # status == "planning" means reviewer requested a revision
    revision_count = state.get("plan_revision_count", 0)
    self_review_count = state.get("self_review_count", 0)
    revision_cap = settings.loops.plan_refinement_max
    self_review_cap = settings.loops.plan_self_review_max

    if revision_count >= revision_cap or self_review_count >= self_review_cap:
        logger.info(
            "route: loop cap reached (revision=%d/%d, self_review=%d/%d) → END with current plan",
            revision_count, revision_cap,
            self_review_count, self_review_cap,
        )
        return END

    logger.debug(
        "route: plan_reviewer requested revision (round %d/%d) → plan_creator",
        revision_count, revision_cap,
    )
    return "plan_creator"


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def _build_planning_graph() -> Any:
    """Construct and compile the Planning StateGraph.

    Returns a compiled LangGraph runnable.  The caller (FastAPI endpoint)
    invokes it with:

        result = await planning_graph.ainvoke(
            initial_state,
            config={"configurable": {"thread_id": topic_id}},
        )
    """
    graph = StateGraph(PlanningState)

    # Register nodes
    graph.add_node("context_builder", context_builder)
    graph.add_node("query_analyzer", query_analyzer)
    graph.add_node("clarifier", clarifier)
    graph.add_node("plan_creator", plan_creator)
    graph.add_node("plan_reviewer", plan_reviewer)

    # Entry point
    graph.add_edge(START, "context_builder")
    graph.add_edge("context_builder", "query_analyzer")

    # Conditional routing after query_analyzer
    graph.add_conditional_edges(
        "query_analyzer",
        route_after_query_analyzer,
        {
            "clarifier": "clarifier",
            "plan_creator": "plan_creator",
            END: END,
        },
    )

    # Clarifier always exits (graph pauses for user input)
    graph.add_conditional_edges(
        "clarifier",
        route_after_clarifier,
        {END: END},
    )

    # plan_creator always feeds plan_reviewer
    graph.add_edge("plan_creator", "plan_reviewer")

    # plan_reviewer routes: approve → END, revise → plan_creator
    graph.add_conditional_edges(
        "plan_reviewer",
        route_after_plan_reviewer,
        {
            "plan_creator": "plan_creator",
            END: END,
        },
    )

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
#       config={"configurable": {"thread_id": topic_id}},
#   )

planning_graph = _build_planning_graph()
