"""
app/api/models.py — Pydantic request/response models for the Deep Research API.

All models are pure Pydantic v2 — no LangGraph / MongoDB imports here.
State conversion (Pydantic → TypedDict) is done inside each route handler.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared sub-models
# ---------------------------------------------------------------------------

class UploadedFileModel(BaseModel):
    """File metadata mirroring graphs/planning/state.py UploadedFile TypedDict."""
    object_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    filename: str
    mime_type: str


class ChatMessageModel(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ToolsEnabledModel(BaseModel):
    web: bool = True
    arxiv: bool = True
    content_lake: bool = False
    files: bool = False


# ---------------------------------------------------------------------------
# POST /api/deepresearch/plan
# ---------------------------------------------------------------------------

class PlanRequest(BaseModel):
    """Request body for the planning endpoint."""
    topic: str = Field(..., min_length=1, max_length=5000, description="Research topic / question")
    tenant_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    user_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    chat_bot_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    chat_history: list[ChatMessageModel] = Field(default_factory=list)
    uploaded_files: list[UploadedFileModel] = Field(default_factory=list)
    # Populated on second call (after user answers clarifying questions)
    clarification_answers: list[str] = Field(default_factory=list)
    # Echo the questions received in the previous PlanResponse so the graph
    # doesn't re-ask them on re-invocation.
    clarification_questions: list[str] = Field(default_factory=list)
    # Re-invoke token — equals the job_id from the first PlanResponse.
    # None on first call; client sends back the same job_id on clarification round.
    job_id: Optional[str] = Field(
        default=None,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    )


class PlanResponse(BaseModel):
    """Response body for the planning endpoint.

    The client checks needs_clarification first:
    - True  → surface clarification_questions to user, re-submit with answers
    - False → present plan to user for approval, then call /run
    """
    job_id: str
    status: str                                         # "plans_ready" | "clarification_needed"
    needs_clarification: bool
    clarification_questions: list[str] = Field(default_factory=list)
    refined_topic: Optional[str] = None
    analysis: Optional[dict[str, Any]] = None           # {depth_of_research, audience, objective,
                                                        #  domain, recency_scope, source_scope, assumptions}
    plan: Optional[dict[str, Any]] = None               # ResearchPlan as plain dict
    checklist: list[str] = Field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# POST /api/deepresearch/run
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    """Request body for the run (execution) endpoint.

    Loads the approved plan from deep_research_jobs by job_id and queues execution.
    """
    job_id: str
    tenant_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    user_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    action: Literal["approve"] = "approve"


class RunResponse(BaseModel):
    job_id: str
    report_id: str
    status: str = "research_queued"


# ---------------------------------------------------------------------------
# GET /api/deepresearch/status/{job_id}
# ---------------------------------------------------------------------------

class StatusResponse(BaseModel):
    job_id: str
    status: str
    progress: int = 0
    section: Optional[str] = None      # current section being researched
    started_at: Optional[str] = None
    updated_at: Optional[str] = None


# ---------------------------------------------------------------------------
# GET /api/deepresearch/reports[/{report_id}]
# ---------------------------------------------------------------------------

class ReportListItem(BaseModel):
    report_id: str
    job_id: str
    tenant_id: str
    user_id: str
    topic: str
    title: str
    status: str
    depth_of_research: str
    section_count: int = 0
    citation_count: int = 0
    summary: Optional[str] = None
    llm_model: Optional[str] = None
    executed_at: Optional[str] = None
    created_at: str


class ReportListResponse(BaseModel):
    items: list[ReportListItem]
    total: int
    skip: int
    limit: int


class ReportDetailResponse(BaseModel):
    report_id: str
    job_id: str
    tenant_id: str
    user_id: str
    topic: str
    title: str
    status: str
    depth_of_research: str
    section_count: int = 0
    citation_count: int = 0
    summary: Optional[str] = None
    llm_model: Optional[str] = None
    executed_at: Optional[str] = None
    created_at: str
    html_url: Optional[str] = None              # S3 presigned URL, 1hr TTL; None if not yet uploaded
    html_url_expires_at: Optional[str] = None   # ISO timestamp of URL expiry
    citations: list[dict[str, Any]] = Field(default_factory=list)  # [{id, title, url, source_type}]
