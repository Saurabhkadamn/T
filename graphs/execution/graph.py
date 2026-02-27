"""
graphs/execution/graph.py — Execution Graph (async, runs in EKS Worker)

Wires all main-pipeline nodes and the section_subgraph into a compiled
StateGraph with AsyncMongoDBSaver checkpointing for crash recovery.

Pipeline topology
-----------------
START
  └── supervisor
        ├── [Send("section_subgraph", …) × N]   ← Phase 1 / gap fill-in
        │     └── section_subgraph (compiled sub-graph)
        │           └──► supervisor               ← fan-in
        └── [knowledge_fusion]                   ← no more gaps / cap reached
              └── report_writer
                    └── report_reviewer
                          ├── [report_writer]     ← needs_revision (≤ 1 time)
                          └── [exporter]          ← approved
                                └── END

Checkpointing
-------------
Uses langgraph.checkpoint.mongodb.aio.AsyncMongoDBSaver.
Thread ID = job_id — so the graph can resume from the last completed node
if the EKS Job is restarted mid-execution.

Usage (from worker/executor.py):
    from graphs.execution.graph import build_execution_graph

    graph, checkpointer = await build_execution_graph()
    config = {"configurable": {"thread_id": job_id}}
    async for event in graph.astream(initial_state, config=config):
        ...  # publish events to Redis pub/sub
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.checkpoint.mongodb import MongoDBSaver
from langgraph.graph import END, START, StateGraph
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from graphs.execution.nodes.exporter import exporter
from graphs.execution.nodes.knowledge_fusion import knowledge_fusion
from graphs.execution.nodes.report_reviewer import report_reviewer, should_revise
from graphs.execution.nodes.report_writer import report_writer
from graphs.execution.nodes.supervisor import dispatch_sections, supervisor
from graphs.execution.section_subgraph import section_subgraph
from graphs.execution.state import ExecutionState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

def _build_graph() -> StateGraph:
    """Assemble the execution StateGraph (without checkpointer — added at compile)."""
    graph = StateGraph(ExecutionState)

    # ------------------------------------------------------------------
    # Main-pipeline nodes
    # ------------------------------------------------------------------
    graph.add_node("supervisor", supervisor)
    graph.add_node("section_subgraph", section_subgraph)
    graph.add_node("knowledge_fusion", knowledge_fusion)
    graph.add_node("report_writer", report_writer)
    graph.add_node("report_reviewer", report_reviewer)
    graph.add_node("exporter", exporter)

    # ------------------------------------------------------------------
    # Edges
    # ------------------------------------------------------------------
    # Entry
    graph.add_edge(START, "supervisor")

    # Supervisor → dispatch (fan-out) or knowledge_fusion (no more gaps)
    graph.add_conditional_edges(
        "supervisor",
        dispatch_sections,
        {"knowledge_fusion": "knowledge_fusion"},
    )

    # Fan-in: each section_subgraph completes → supervisor (reflection phase)
    graph.add_edge("section_subgraph", "supervisor")

    # Main pipeline after all sections are researched
    graph.add_edge("knowledge_fusion", "report_writer")
    graph.add_edge("report_writer", "report_reviewer")

    # Revision loop or proceed to export
    graph.add_conditional_edges(
        "report_reviewer",
        should_revise,
        {
            "report_writer": "report_writer",
            "exporter": "exporter",
        },
    )

    graph.add_edge("exporter", END)

    return graph


# ---------------------------------------------------------------------------
# Public factory (async — opens MongoDB connection for checkpointer)
# ---------------------------------------------------------------------------

async def build_execution_graph() -> tuple[Any, MongoDBSaver]:
    """Build and compile the execution graph with AsyncMongoDBSaver.

    Returns:
        (compiled_graph, checkpointer)

    The checkpointer must be kept alive for the duration of the job.
    Call await checkpointer.aclose() when the worker exits.

    Usage::
        graph, checkpointer = await build_execution_graph()
        try:
            config = {"configurable": {"thread_id": job_id}}
            async for event in graph.astream(initial_state, config=config):
                await publish_event(event)
        finally:
            await checkpointer.aclose()
    """
    mongo_client = AsyncIOMotorClient(settings.mongo_uri)
    checkpointer = MongoDBSaver(
        client=mongo_client,
        db_name="kadal_platform",
        collection_name="langgraph_checkpoints",
    )

    compiled = _build_graph().compile(checkpointer=checkpointer)

    logger.info("build_execution_graph: compiled execution graph with AsyncMongoDBSaver")
    return compiled, checkpointer


# ---------------------------------------------------------------------------
# Convenience: module-level compiled graph WITHOUT checkpointer (for tests)
# ---------------------------------------------------------------------------

def build_execution_graph_no_checkpointer() -> Any:
    """Compile without a checkpointer — for unit tests and local dev.

    Usage::
        graph = build_execution_graph_no_checkpointer()
        result = await graph.ainvoke(mock_state)
    """
    return _build_graph().compile()
