"""
app/api/routes/plan.py — POST /api/deepresearch/plan

Implements three invocation modes:

  Mode 1 (fresh):      job_id absent → build fresh state, run from context_builder
  Mode 2 (clarify):    job_id + clarification_answers → load state from DB, run from query_analyzer
  Mode 3 (plan revise):job_id + plan_feedback → load state from DB, run plan_creator

MongoDB state is saved after EVERY invocation so the graph can resume
from the correct point on subsequent calls.

Flow
----
1. Determine mode from request shape.
2. Rate-limit pre-check (read-only).
3. Build or reconstruct PlanningState.
4. Run planning_graph.ainvoke with redis + mongo injected via configurable.
5. Consume rate-limit slot.
6. Persist planning_state subdoc to Deep_Research_Jobs (upsert, every call).
7. Return PlanResponse.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorClient
from redis.asyncio import Redis

from app.api.models import PlanRequest, PlanResponse
from app.config import settings
from app.dependencies import get_mongo, get_redis, get_tenant_id
from graphs.planning.graph import planning_graph
from graphs.planning.state import PlanningState

logger = logging.getLogger(__name__)

router = APIRouter()

_RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local current = redis.call('INCR', key)
if current == 1 then
    redis.call('EXPIRE', key, ttl)
end
if current > limit then
    return 0
end
return 1
"""


# ---------------------------------------------------------------------------
# Rate limiting helpers
# ---------------------------------------------------------------------------

async def _check_rate_limit_readonly(redis: Redis, tenant_id: str, user_id: str) -> None:
    limit = settings.rate_limit_plan_per_hour
    if limit is None:
        return
    key = f"rate:plan:{tenant_id}:{user_id}"
    try:
        current_str = await redis.get(key)
        current = int(current_str or 0)
        if current >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Plan rate limit exceeded. Try again later.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("plan: rate-limit Redis error in pre-check (failing open) — %s", exc)


async def _consume_rate_limit(redis: Redis, tenant_id: str, user_id: str) -> None:
    limit = settings.rate_limit_plan_per_hour
    if limit is None:
        return
    key = f"rate:plan:{tenant_id}:{user_id}"
    ttl = settings.redis_ttls.rate_limit_plan_ttl_seconds
    try:
        allowed = await redis.eval(_RATE_LIMIT_SCRIPT, 1, key, limit, ttl)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Plan rate limit exceeded. Try again later.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("plan: rate-limit Redis error in consume (failing open) — %s", exc)


# ---------------------------------------------------------------------------
# State persistence helpers
# ---------------------------------------------------------------------------

_STATE_FIELDS_TO_SAVE = [
    "context_brief", "file_contents", "clarification_questions", "clarification_count",
    "clarification_answers", "needs_clarification", "refined_topic", "depth_of_research",
    "audience", "objective", "domain", "recency_scope", "source_scope", "assumptions",
    "plan", "checklist", "plan_revision_count", "plan_feedback", "status",
    # identity fields needed to reconstruct state
    "topic_id", "tenant_id", "user_id", "chat_bot_id", "original_topic", "uploaded_files",
    "error",
]


def _extract_planning_state_subdoc(result: dict[str, Any]) -> dict[str, Any]:
    """Pull the fields we need to persist for graph re-entry."""
    return {k: result.get(k) for k in _STATE_FIELDS_TO_SAVE if k in result}


async def _save_job_doc(
    mongo: AsyncIOMotorClient,
    job_id: str,
    tenant_id: str,
    user_id: str,
    chat_bot_id: str,
    topic: str,
    result: dict[str, Any],
    llm_model: str,
    now_iso: str,
    existing_doc: dict | None,
) -> None:
    """Upsert the Deep_Research_Jobs document after every invocation."""
    graph_status = result.get("status", "unknown")

    # Top-level job status: plans_ready only when we have an approved plan
    if graph_status == "awaiting_approval":
        job_status = "plans_ready"
    elif graph_status == "needs_clarification":
        job_status = "needs_clarification"
    else:
        job_status = graph_status

    # Preserve created_at from existing doc
    created_at = (existing_doc or {}).get("created_at", now_iso)

    job_doc = {
        "job_id": job_id,
        "report_id": (existing_doc or {}).get("report_id"),
        "tenant_id": tenant_id,
        "user_id": user_id,
        "chat_bot_id": chat_bot_id,
        "topic": topic,
        "refined_topic": result.get("refined_topic", ""),
        "llm_model": llm_model,
        "status": job_status,
        "progress": 0,
        "plan": copy.deepcopy(result["plan"]) if result.get("plan") else None,
        "checklist": result.get("checklist", []),
        "analysis": {
            "depth_of_research": result.get("depth_of_research", "intermediate"),
            "audience": result.get("audience", ""),
            "objective": result.get("objective", ""),
            "domain": result.get("domain", ""),
            "recency_scope": result.get("recency_scope", ""),
            "source_scope": result.get("source_scope", []),
            "assumptions": result.get("assumptions", []),
        },
        "uploaded_files": result.get("uploaded_files", []),
        "tools_enabled": {
            "web": "web" in (result.get("source_scope") or []),
            "arxiv": "arxiv" in (result.get("source_scope") or []),
            "content_lake": "content_lake" in (result.get("source_scope") or []),
            "files": "files" in (result.get("source_scope") or []),
        },
        # Subdoc with all planning graph fields needed for Mode 2 / 3 re-entry
        "planning_state": _extract_planning_state_subdoc(result),
        "metadata": (existing_doc or {}).get("metadata", {
            "otel_trace_id": "",
            "total_tokens_used": 0,
            "total_cost_usd": 0.0,
            "pro_calls": 0,
            "flash_calls": 0,
        }),
        "started_at": None,
        "completed_at": None,
        "error": result.get("error"),
        "created_at": created_at,
        "updated_at": now_iso,
    }

    db = mongo[settings.mongo_db_name]
    await db["Deep_Research_Jobs"].replace_one(
        {"job_id": job_id, "tenant_id": tenant_id},
        job_doc,
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/plan",
    response_model=PlanResponse,
    summary="Run planning graph — returns plan or clarification questions",
)
async def plan(
    body: PlanRequest,
    tenant_id: str = Depends(get_tenant_id),
    redis: Redis = Depends(get_redis),
    mongo: AsyncIOMotorClient = Depends(get_mongo),
) -> PlanResponse:

    # ── Tenant consistency ─────────────────────────────────────────────────
    if body.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tenant_id in body must match X-Tenant-ID header",
        )

    # ── Rate limiting (pre-flight, read-only) ──────────────────────────────
    await _check_rate_limit_readonly(redis, tenant_id, body.user_id)

    # ── Determine mode ─────────────────────────────────────────────────────
    if body.job_id is None:
        mode = "mode1"
    elif body.clarification_answers:
        mode = "mode2"
    elif body.plan_feedback:
        mode = "mode3"
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "job_id is present but neither clarification_answers nor plan_feedback "
                "was provided. Supply one to continue the session."
            ),
        )

    db = mongo[settings.mongo_db_name]
    existing_doc: dict | None = None

    # ── Build PlanningState ────────────────────────────────────────────────
    if mode == "mode1":
        job_id = str(uuid.uuid4())
        initial_state: PlanningState = {
            "topic_id": job_id,
            "tenant_id": tenant_id,
            "user_id": body.user_id,
            "chat_bot_id": body.chat_bot_id,  # guaranteed by model validator
            "uploaded_files": [
                {"object_id": f.object_id, "filename": f.filename, "mime_type": f.mime_type}
                for f in body.uploaded_files
            ],
            "file_contents": {},
            "context_brief": "",           # context_builder will populate
            "original_topic": body.topic,  # guaranteed by model validator
            "refined_topic": "",
            "needs_clarification": False,
            "clarification_questions": [],
            "clarification_answers": [],
            "clarification_count": 0,
            "depth_of_research": "intermediate",
            "audience": "",
            "objective": "",
            "domain": "",
            "recency_scope": "",
            "source_scope": [],
            "assumptions": [],
            "plan": None,
            "plan_revision_count": 0,
            "plan_feedback": None,
            "checklist": [],
            "status": "pending",
            "error": None,
        }

    else:
        # Modes 2 & 3: load saved state from MongoDB
        job_id = body.job_id
        existing_doc = await db["Deep_Research_Jobs"].find_one(
            {"job_id": job_id, "tenant_id": tenant_id},
            {"_id": 0},
        )
        if existing_doc is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job {job_id} not found for this tenant.",
            )

        saved_state: dict = existing_doc.get("planning_state") or {}
        if not saved_state:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Job {job_id} has no saved planning state to resume from.",
            )

        if mode == "mode2":
            # Validate job is awaiting clarification — plans_ready means the plan was
            # already accepted; those jobs must use Mode 3 (plan_feedback) or /run.
            if existing_doc.get("status") != "needs_clarification":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Job {job_id} is in status '{existing_doc.get('status')}' "
                        "and cannot accept clarification answers. "
                        "Use plan_feedback to revise an existing plan."
                    ),
                )
            # Merge clarification answers into saved state
            initial_state = {**saved_state}  # type: ignore[assignment]
            initial_state["clarification_answers"] = list(body.clarification_answers)
            initial_state["clarification_questions"] = list(
                body.clarification_questions or saved_state.get("clarification_questions", [])
            )
            initial_state["plan"] = None  # force re-planning after clarification

        else:
            # mode3 — plan revision
            revision_count = saved_state.get("plan_revision_count", 0)
            if revision_count >= settings.loops.plan_revision_max:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Maximum plan revisions ({settings.loops.plan_revision_max}) reached. "
                        "Please start a new research session."
                    ),
                )

            initial_state = {**saved_state}  # type: ignore[assignment]
            initial_state["plan_feedback"] = body.plan_feedback
            initial_state["plan_revision_count"] = revision_count + 1

    # ── Run planning graph ─────────────────────────────────────────────────
    try:
        result = await asyncio.wait_for(
            planning_graph.ainvoke(
                initial_state,
                config={
                    "configurable": {
                        "thread_id": job_id,
                        "redis_client": redis,
                        "mongo_client": mongo,
                    },
                },
            ),
            timeout=settings.planning_timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        logger.error(
            "plan: planning graph timed out after %ds (job_id=%s mode=%s)",
            settings.planning_timeout_seconds,
            job_id,
            mode,
        )
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Planning timed out. Please try again.",
        ) from exc
    except Exception as exc:
        logger.exception("plan: planning graph raised an exception (job_id=%s)", job_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Planning graph error: {exc}",
        ) from exc

    # ── Consume rate-limit slot ────────────────────────────────────────────
    await _consume_rate_limit(redis, tenant_id, body.user_id)

    # ── Map final state → error check ──────────────────────────────────────
    if result.get("status") == "failed":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.get("error") or "Planning graph failed with unknown error",
        )

    # ── Persist state to MongoDB (EVERY invocation) ────────────────────────
    now_iso = datetime.now(timezone.utc).isoformat()
    llm_model = (
        settings.llm_provider
        + "/"
        + settings.models.model_for_provider("plan_creation", settings.llm_provider)
    )
    # Merge uploaded_files into result for persistence
    result_for_save = {
        **result,
        "uploaded_files": initial_state.get("uploaded_files", []),
        "topic_id": job_id,
        "tenant_id": tenant_id,
        "user_id": body.user_id,
        "chat_bot_id": initial_state.get("chat_bot_id", body.chat_bot_id or ""),
        "original_topic": initial_state.get("original_topic", body.topic or ""),
    }

    try:
        await _save_job_doc(
            mongo=mongo,
            job_id=job_id,
            tenant_id=tenant_id,
            user_id=body.user_id,
            chat_bot_id=initial_state.get("chat_bot_id") or body.chat_bot_id or "",
            topic=initial_state.get("original_topic") or body.topic or "",
            result=result_for_save,
            llm_model=llm_model,
            now_iso=now_iso,
            existing_doc=existing_doc,
        )
    except Exception as exc:
        logger.error("plan: failed to save planning state to MongoDB — %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist planning state. Please try again.",
        ) from exc

    # ── Build response ─────────────────────────────────────────────────────
    graph_status: str = result.get("status", "unknown")
    needs_clarification: bool = result.get("needs_clarification", False)

    analysis_dict: dict | None = None
    if result.get("depth_of_research") or result.get("audience"):
        analysis_dict = {
            "depth_of_research": result.get("depth_of_research", "intermediate"),
            "audience": result.get("audience", ""),
            "objective": result.get("objective", ""),
            "domain": result.get("domain", ""),
            "recency_scope": result.get("recency_scope", ""),
            "source_scope": result.get("source_scope", []),
            "assumptions": result.get("assumptions", []),
        }

    return PlanResponse(
        job_id=job_id,
        status=graph_status,
        needs_clarification=needs_clarification,
        clarification_questions=result.get("clarification_questions") or [],
        refined_topic=result.get("refined_topic") or None,
        analysis=analysis_dict,
        plan=dict(result["plan"]) if result.get("plan") else None,
        checklist=result.get("checklist") or [],
        error=result.get("error"),
    )
