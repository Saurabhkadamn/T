"""
PlanningState — the shared state TypedDict for the Planning Graph.

This module also defines all auxiliary types (ChatMessage, UploadedFile,
PlanSection, ResearchPlan) so that every node imports types from one place.

Design rules:
- All fields are present in every state snapshot (no total=False).
  Fields that are not yet populated carry a typed empty value (None / [] / {}).
- No LLM calls, no I/O — this file is pure Python and independently testable.
- Optional[X] means "not yet populated"; the graph routing must guard against
  accessing None fields before the node that produces them has run.
"""

from __future__ import annotations

from typing import Literal, Optional, TypedDict


# ---------------------------------------------------------------------------
# Literal types
# ---------------------------------------------------------------------------

DepthLevel = Literal["surface", "intermediate", "in-depth"]

SourceType = Literal["web", "tavily", "arxiv", "content_lake", "files"]

PlanningStatus = Literal[
    "pending",             # initial state, not yet started
    "analyzing",           # query_analyzer running
    "needs_clarification", # query_analyzer returned questions, waiting for answers
    "planning",            # plan_creator running
    "awaiting_approval",   # plan ready, waiting for human approval via /run
    "failed",              # unrecoverable error
]


# ---------------------------------------------------------------------------
# Auxiliary TypedDicts
# ---------------------------------------------------------------------------

class ChatMessage(TypedDict):
    """A single turn in the conversation history.

    role: "user" for human turns, "assistant" for Kadal AI turns.
    content: raw text of the message (may contain markdown).
    """
    role: Literal["user", "assistant"]
    content: str


class UploadedFile(TypedDict):
    """Metadata for a file the user attached to their research request.

    object_id: platform-wide identifier used to look up cached content
               in Redis (key: file:{object_id}:content).
    filename:  original filename for display / citation purposes.
    mime_type: e.g. "application/pdf", "text/plain".
    """
    object_id: str
    filename: str
    mime_type: str


class PlanSection(TypedDict):
    """A single research section within a ResearchPlan.

    section_id:        stable UUID assigned at plan creation time.
                       Used as the LangGraph thread_id suffix for the
                       section sub-graph and as the MongoDB section key.
    title:             short human-readable heading.
    description:       1-3 sentence summary of what this section covers.
    search_strategy:   comma-separated source types the searcher should
                       prioritise for this section, e.g. "arxiv,web".
                       The searcher uses this to weight tool calls.
    """
    section_id: str
    title: str
    description: str
    search_strategy: str


class ResearchPlan(TypedDict):
    """The structured research plan produced by plan_creator.

    title:             report title (used as the HTML <h1>).
    summary:           2-4 sentence executive summary of the planned report.
    sections:          ordered list of PlanSection objects.
    estimated_sources: rough total source count across all sections
                       (used by the UI to set progress-bar expectations).
    """
    title: str
    summary: str
    sections: list[PlanSection]
    estimated_sources: int


# ---------------------------------------------------------------------------
# PlanningState
# ---------------------------------------------------------------------------

class PlanningState(TypedDict):
    """Full shared state for the Planning Graph.

    Field groups
    ------------
    Identity        — who triggered this research session
    Input context   — raw inputs passed by the API caller
    Context         — LLM-generated prose summary produced by context_builder
    Topic           — original and LLM-refined topic strings
    Clarification   — loop state for the clarifier ↔ caller exchange
    Parameters      — resolved research configuration (depth, audience, …)
    Plan            — the structured plan and its review lifecycle
    Lifecycle       — status + error for graph routing and API responses
    """

    # ------------------------------------------------------------------
    # Identity  (set once at graph entry, never mutated)
    # ------------------------------------------------------------------
    topic_id: str               # unique ID for this research topic / session
    tenant_id: str              # multi-tenancy partition key — MUST be on every DB query
    user_id: str
    chat_bot_id: str            # which Kadal chatbot widget triggered the request

    # ------------------------------------------------------------------
    # Input context  (set once at graph entry, never mutated)
    # ------------------------------------------------------------------
    chat_history: list[ChatMessage]     # conversation turns passed in at request time
    uploaded_files: list[UploadedFile]  # file metadata; content loaded in context_builder
    file_contents: dict[str, str]       # object_id → extracted text (populated by context_builder)

    # ------------------------------------------------------------------
    # Context brief  (produced by context_builder via LLM)
    # ------------------------------------------------------------------
    context_brief: str          # LLM-generated prose summary of chat history + file contents

    # ------------------------------------------------------------------
    # Topic
    # ------------------------------------------------------------------
    original_topic: str             # verbatim user query
    refined_topic: str              # query_analyzer's normalised / de-ambiguated version

    # ------------------------------------------------------------------
    # Clarification loop
    # ------------------------------------------------------------------
    needs_clarification: bool           # True when query_analyzer decides questions are required
    clarification_questions: list[str]  # questions returned to the API caller
    clarification_answers: list[str]    # answers provided by the user (parallel list)
    clarification_count: int            # number of completed clarification rounds

    # ------------------------------------------------------------------
    # Research parameters  (extracted by query_analyzer)
    # ------------------------------------------------------------------
    depth_of_research: DepthLevel       # controls source count + compression target
    audience: str                       # e.g. "undergraduate students", "C-suite executives"
    objective: str                      # e.g. "understand current state", "compare approaches"
    domain: str                         # e.g. "machine learning", "corporate finance"
    recency_scope: str                  # e.g. "last_6_months", "last_2_years", "all_time"
    source_scope: list[SourceType]      # which source integrations to enable

    # Assumptions the LLM made when the user query was ambiguous.
    # Surfaced to the user in the plan approval UI.
    assumptions: list[str]

    # ------------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------------
    plan: Optional[ResearchPlan]    # None until plan_creator produces a plan
    plan_revision_count: int        # incremented each time user rejects plan via Mode 3
    plan_feedback: Optional[str]    # user's rejection feedback text (None on fresh creation)
    checklist: list[str]            # quality gates produced by plan_creator (3-5 items)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    status: PlanningStatus
    error: Optional[str]            # set on status="failed"; None otherwise
