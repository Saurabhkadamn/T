"""
app/api/models.py — Pydantic request/response models for the Deep Research API.

All models are pure Pydantic v2 — no LangGraph / MongoDB imports here.
State conversion (Pydantic → TypedDict) is done inside each route handler.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Shared sub-models
# ---------------------------------------------------------------------------

class ChatMessageModel(BaseModel):
    """A single conversation turn passed directly in the request payload."""
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=10000)


# ---------------------------------------------------------------------------
# POST /api/deepresearch/plan
# ---------------------------------------------------------------------------

class PlanRequest(BaseModel):
    """Request body for the planning endpoint.

    Three invocation modes:
      Mode 1 (fresh):   job_id=None, topic required.
      Mode 2 (clarify): job_id set, clarification_answers populated.
      Mode 3 (revise):  job_id set, plan_feedback populated.

    Auth (tenant_id + user_id) comes from the Keycloak Bearer token,
    not from the request body.
    """
    # Required on Mode 1; optional on Modes 2 & 3 (loaded from DB by job_id)
    topic: Optional[str] = Field(
        default=None, min_length=1, max_length=5000, description="Research topic / question"
    )

    @field_validator("topic", mode="before")
    @classmethod
    def strip_topic(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                raise ValueError("topic cannot be blank or whitespace-only")
        return v

    # Conversation history passed directly by the caller (not fetched from MongoDB)
    chat_history: list[ChatMessageModel] = Field(default_factory=list)
    # Attached files: [{object_id, filename, mime_type, ...}]
    objects: list[dict[str, str]] = Field(default_factory=list)
    # Tool selection: ["web", "arxiv", "content_lake", "files"]
    tools: list[str] = Field(default_factory=list)

    # Mode 2: answers to the clarification questions from the previous response
    clarification_answers: list[str] = Field(default_factory=list)
    # Echo of the questions from the previous PlanResponse (for graph re-entry)
    clarification_questions: list[str] = Field(default_factory=list)
    # Mode 3: human reviewer's plain-text feedback on the plan
    plan_feedback: Optional[str] = Field(default=None, max_length=5000)
    # Re-invoke token — None on first call; echoed back by client on Modes 2 & 3
    # Format: MongoDB ObjectId (24-char lowercase hex)
    job_id: Optional[str] = Field(
        default=None,
        pattern=r"^[0-9a-f]{24}$",
    )

    @model_validator(mode="after")
    def validate_mode_requirements(self) -> "PlanRequest":
        """Mode 1 requires topic; Modes 2/3 require job_id."""
        if self.job_id is None:
            # Mode 1
            if not self.topic:
                raise ValueError("topic is required when job_id is not provided (Mode 1)")
        return self


class PlanResponse(BaseModel):
    """Response body for the planning endpoint.

    The client checks needs_clarification first:
    - True  → surface clarification_questions to user, re-submit with answers
    - False → present plan to user for approval, then call /run
    """
    job_id: str
    status: str                                         # free-form — graph-defined status strings
    needs_clarification: bool
    clarification_questions: list[str] = Field(default_factory=list)
    refined_topic: Optional[str] = None
    analysis: Optional[dict[str, Any]] = None           # {depth_of_research, audience, objective,
                                                        #  domain, recency_scope, source_scope, assumptions}
    plan: Optional[dict[str, Any]] = None               # ResearchPlan as plain dict
    checklist: list[str] = Field(default_factory=list)
    max_revisions_reached: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# POST /api/deepresearch/run
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    """Request body for the run (execution) endpoint.

    Loads the approved plan from deep_research_jobs by job_id and queues execution.
    Auth (tenant_id + user_id) comes from the Keycloak Bearer token.
    """
    job_id: str
    action: Literal["approve", "cancel"] = "approve"


class RunResponse(BaseModel):
    job_id: str
    report_id: Optional[str] = None
    status: Literal["QUEUED", "PLAN_CANCELED"] = "QUEUED"


# ---------------------------------------------------------------------------
# GET /api/deepresearch/status/{job_id}
# ---------------------------------------------------------------------------

class StatusResponse(BaseModel):
    job_id: str
    status: Literal[
        # DB planning statuses
        "PLAN_INITIATED", "NEED_CLARIFICATION", "PLAN_READY",
        "PLAN_EDIT_REQUESTED", "PLAN_CANCELED",
        # DB execution statuses
        "QUEUED", "RUNNING", "COMPLETED", "FAILED",
        # streaming-only statuses (worker → Redis → client, not stored in MongoDB)
        "initializing", "researching", "fusing", "writing", "reviewing", "exporting",
        "unknown",
    ]
    progress: int = 0
    section: Optional[str] = None      # current section being researched
    started_at: Optional[str] = None
    updated_at: Optional[str] = None


# ---------------------------------------------------------------------------
# GET /api/deepresearch/reports[/{report_id}]
# ---------------------------------------------------------------------------

_ReportStatus = Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "unknown"]


class ReportListItem(BaseModel):
    report_id: str
    job_id: str
    tenant_id: str
    user_id: str
    topic: str
    title: str
    status: _ReportStatus
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
    status: _ReportStatus
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
