"""
ExecutionState — shared state TypedDicts for the Execution Graph.

This module defines all types needed by execution nodes. Every node imports
from this file. The planning types (DepthLevel, SourceType, UploadedFile,
ResearchPlan) are re-used directly from the planning graph to avoid duplication.

Design rules (mirrors graphs/planning/state.py conventions):
- All fields present in every snapshot — no total=False.
- Optional[X] means "not yet populated at graph entry"; guard before access.
- Non-optional list/dict/int/str fields start as [] / {} / 0 / "".
- No LLM calls, no I/O — pure Python, independently testable.
- The four reducer fields in ExecutionState use Annotated[list[T], operator.add]
  so that parallel section sub-graphs can write results without overwriting.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, Optional, TypedDict

from graphs.planning.state import (
    DepthLevel,   # noqa: F401 — re-exported for node convenience
    ResearchPlan,
    SourceType,   # noqa: F401 — re-exported for node convenience
    UploadedFile,
)


# ---------------------------------------------------------------------------
# ExecutionStatus Literal
# ---------------------------------------------------------------------------

ExecutionStatus = Literal[
    "queued",           # job created, worker not yet started
    "initializing",     # worker started, loading file contents
    "researching",      # section sub-graphs running in parallel
    "reflecting",       # supervisor reflection round
    "fusing",           # knowledge_fusion node running
    "writing",          # report_writer streaming HTML
    "reviewing",        # report_reviewer checking quality
    "exporting",        # exporter writing to S3 + MongoDB
    "completed",        # full success
    "partial_success",  # some sections failed but report still generated
    "failed",           # unrecoverable error, no report produced
]


# ---------------------------------------------------------------------------
# ToolsEnabled TypedDict
# ---------------------------------------------------------------------------

class ToolsEnabled(TypedDict):
    """Which search integrations are active for this research job.

    Typed struct (not dict[str, bool]) so mypy can catch missing keys.
    Mirrors the four SourceType values: web, arxiv, content_lake, files.
    """
    web: bool
    arxiv: bool
    content_lake: bool
    files: bool


# ---------------------------------------------------------------------------
# RawSearchResult TypedDict
# ---------------------------------------------------------------------------

class RawSearchResult(TypedDict):
    """One search result returned by a tool, before verification or compression.

    result_id:    UUID assigned by the searcher node for deduplication.
    source_type:  which tool produced this result.
    query:        the search query that produced this result.
    url:          source URL (empty string for file/arxiv results with no URL).
    title:        document title.
    content:      raw extracted text (may be 50K+ tokens for web pages).
    published_at: ISO-8601 date string or empty string if unknown.
    score:        relevance score returned by the search tool (0.0–1.0).
    """
    result_id: str
    source_type: SourceType
    query: str
    url: str
    title: str
    content: str
    published_at: str
    score: float


# ---------------------------------------------------------------------------
# VerifiedUrl TypedDict
# ---------------------------------------------------------------------------

class VerifiedUrl(TypedDict):
    """Output of source_verifier.py for a single web URL.

    is_authentic: False if the URL is a redirect trap, paywalled, or 404.
    url_hash:     SHA-256 of the normalised URL — used as the Redis/Mongo key.
    domain:       registered domain (e.g. "arxiv.org") for domain-level scoring.
    """
    url: str
    is_authentic: bool
    url_hash: str
    domain: str


# ---------------------------------------------------------------------------
# Citation TypedDict
# ---------------------------------------------------------------------------

class Citation(TypedDict):
    """A citable source reference embedded in the final report.

    citation_id:  UUID assigned by the node that produces the citation.
    section_id:   which section this citation belongs to.
    source_type:  origin tool.
    title:        document title for the bibliography entry.
    url:          canonical URL (empty string for file citations).
    authors:      author list as a single formatted string, or empty string.
    published_at: ISO-8601 date string, or empty string.
    snippet:      short verbatim extract used as the inline quote.
    trust_tier:   knowledge fusion priority encoded as int:
                    1 = files (highest trust)
                    2 = content_lake
                    3 = arxiv
                    4 = web (lowest trust)
                  Stored as int so report_writer can sort without branching.
    """
    citation_id: str
    section_id: str
    source_type: SourceType
    title: str
    url: str
    authors: str
    published_at: str
    snippet: str
    trust_tier: int


# ---------------------------------------------------------------------------
# SourceScore TypedDict
# ---------------------------------------------------------------------------

class SourceScore(TypedDict):
    """Scoring record for one source, written to the source_scores collection.

    tenant_id is embedded here so exporter.py never needs a partial state
    read when bulk-inserting into MongoDB (every query must include tenant_id).

    url_hash:            SHA-256 of normalised URL — matches VerifiedUrl.url_hash.
    authenticity_score:  0.0–1.0 from source_verifier (web) or 1.0 for files.
    relevance_score:     0.0–1.0 from scorer node.
    recency_score:       0.0–1.0 derived from published_at vs recency_scope.
    composite_score:     weighted average used for trust_tier assignment.
    scored_at:           ISO-8601 timestamp of scoring.
    """
    url_hash: str
    url: str
    domain: str
    tenant_id: str
    section_id: str
    source_type: SourceType
    authenticity_score: float
    relevance_score: float
    recency_score: float
    composite_score: float
    scored_at: str


# ---------------------------------------------------------------------------
# CompressedFinding TypedDict
# ---------------------------------------------------------------------------

class CompressedFinding(TypedDict):
    """Compressed research output for one section, produced by compressor.py.

    Compression target is depth-dependent (not flat 3-5K):
      surface:      ~2,500 tokens  (3 sources)
      intermediate: ~5,000 tokens  (5 sources)
      in-depth:     ~8,000 tokens  (10 sources)

    source_types_used: lets knowledge_fusion tag claims by origin without
                       re-parsing the compressed text.
    token_estimate:    approximate token count after compression (for logging
                       and cost tracking — not enforced at runtime).
    """
    section_id: str
    section_title: str
    compressed_text: str
    source_types_used: list[SourceType]
    token_estimate: int


# ---------------------------------------------------------------------------
# SectionResult TypedDict
# ---------------------------------------------------------------------------

class SectionResult(TypedDict):
    """Summary returned by a section sub-graph to the supervisor node.

    status values:
      "completed" — all searches ran and compression succeeded.
      "partial"   — some searches failed but usable findings were produced.
      "failed"    — no usable output; supervisor may flag a knowledge gap.

    error is None when status is "completed".
    """
    section_id: str
    section_title: str
    status: Literal["completed", "partial", "failed"]
    source_count: int
    search_iterations_used: int
    error: Optional[str]


# ---------------------------------------------------------------------------
# SectionResearchState TypedDict
# ---------------------------------------------------------------------------

class SectionResearchState(TypedDict):
    """Isolated state for one section sub-graph.

    Spawned by the supervisor via LangGraph's Send() API. Runs completely
    independently — no shared mutable state with other section sub-graphs.

    Field groups
    ------------
    Identity / Config  — set by Send(), treated as immutable inside the sub-graph
    Search             — accumulated across search iterations
    Processing         — intermediate outputs (verification, compression, scoring)
    Output             — final fields returned to ExecutionState via reducers

    Note: compressed_findings is str here (single-section output).
          In ExecutionState it is Annotated[list[CompressedFinding], operator.add].
    """

    # ------------------------------------------------------------------
    # Identity  (set by Send, immutable inside sub-graph)
    # ------------------------------------------------------------------
    section_id: str
    section_title: str
    section_description: str
    search_strategy: str            # e.g. "arxiv,web" — searcher weights tools by this

    # ------------------------------------------------------------------
    # Config  (set by Send, immutable inside sub-graph)
    # ------------------------------------------------------------------
    tenant_id: str                  # propagated from ExecutionState for content_lake calls
    max_search_iterations: int
    max_sources: int
    compression_target_tokens: int  # depth-dependent budget: 2500 / 5000 / 8000
    max_chars_per_source: int       # per-source input cap for compressor (depth-dependent)
    file_contents: dict[str, str]   # object_id → extracted text (read-only copy)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    search_queries: list[str]               # generated by search_query_gen node
    raw_search_results: list[RawSearchResult]
    search_iteration: int                   # current iteration count (0-indexed)

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------
    verified_urls: list[VerifiedUrl]        # output of source_verifier (web only)
    compressed_text: str                    # single string — output of compressor node
    source_scores: list[SourceScore]        # output of scorer node
    citations: list[Citation]               # assembled during compression / scoring

    # ------------------------------------------------------------------
    # Output  (merged into ExecutionState reducer fields via output schema)
    # ------------------------------------------------------------------
    section_findings: str                   # final narrative (same as compressed_text)
    section_citations: list[Citation]
    section_source_scores: list[SourceScore]


# ---------------------------------------------------------------------------
# ExecutionState TypedDict
# ---------------------------------------------------------------------------

class ExecutionState(TypedDict):
    """Full shared state for the Execution Graph.

    Field groups
    ------------
    Identity        — job / report / tenant / user IDs
    Topic           — topic strings from planning output
    Plan            — the approved ResearchPlan and derived fields
    Attachments     — user-uploaded files and tool configuration
    Config          — depth-derived runtime parameters
    Reducers        — fields written by parallel section sub-graphs
    Supervisor      — reflection loop state
    Knowledge       — fused output from knowledge_fusion node
    Report          — HTML report lifecycle
    Lifecycle       — status, progress, error
    Observability   — distributed tracing

    Reducer fields (Annotated with operator.add)
    --------------------------------------------
    All four use operator.add (list concatenation) so that section sub-graphs
    spawned in parallel via Send() can each append their results without
    overwriting each other:
      section_results, compressed_findings, citations, source_scores

    Optional vs non-optional
    ------------------------
    Optional:     fused_knowledge, report_html, review_feedback, s3_key, error
                  — genuinely absent at graph entry, None until produced.
    Non-optional: all list / dict / int / str fields
                  — carry empty defaults ([] / {} / 0 / "") at graph entry.
    """

    # ------------------------------------------------------------------
    # Identity  (set once at graph entry, never mutated)
    # ------------------------------------------------------------------
    job_id: str         # primary key for this execution; also used as LangGraph thread_id
    report_id: str      # pre-assigned report document ID
    tenant_id: str      # multi-tenancy partition key — MUST be on every DB query
    user_id: str

    # ------------------------------------------------------------------
    # Topic  (from planning output)
    # ------------------------------------------------------------------
    topic: str          # original user query
    refined_topic: str  # query_analyzer's normalised version

    # ------------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------------
    plan: ResearchPlan
    checklist: list[str]    # quality checklist from plan_reviewer
    sections: list[dict]    # typed as list[dict] — checkpointer restores raw dicts,
                            # not PlanSection TypedDicts; nodes cast as needed

    # ------------------------------------------------------------------
    # Attachments + tools
    # ------------------------------------------------------------------
    attachments: list[UploadedFile]
    file_contents: dict[str, str]   # object_id → extracted text (populated by worker init)
    tools_enabled: ToolsEnabled

    # ------------------------------------------------------------------
    # Config  (set from settings.get_depth_config(depth))
    # ------------------------------------------------------------------
    max_search_iterations: int
    max_sources_per_section: int
    max_chars_per_source: int       # per-source input cap passed to compressor via Send()
    report_revision_max: int        # depth-dependent revision cap: surface=1, inter=2, depth=3

    # ------------------------------------------------------------------
    # Reducers  (Annotated with operator.add for parallel section writes)
    # ------------------------------------------------------------------
    section_results: Annotated[list[SectionResult], operator.add]
    compressed_findings: Annotated[list[CompressedFinding], operator.add]
    citations: Annotated[list[Citation], operator.add]
    source_scores: Annotated[list[SourceScore], operator.add]

    # ------------------------------------------------------------------
    # Supervisor
    # ------------------------------------------------------------------
    reflection_count: int           # number of completed reflection rounds (max 2)
    knowledge_gaps: list[str]       # gaps identified by supervisor reflection

    # ------------------------------------------------------------------
    # Knowledge fusion
    # ------------------------------------------------------------------
    fused_knowledge: Optional[str]  # None until knowledge_fusion node runs

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    report_html: Optional[str]      # None until report_writer produces output
    review_feedback: Optional[str]  # None if report_reviewer approves without feedback
    revision_count: int             # number of writer → reviewer revision loops (max 1)
    s3_key: Optional[str]           # None until exporter uploads to S3

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    progress: int               # 0–100 percentage, published to Redis on each update
    status: ExecutionStatus
    error: Optional[str]        # None unless status is "failed" or "partial_success"

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------
    otel_trace_id: str          # OpenTelemetry trace ID; "" if tracing disabled
