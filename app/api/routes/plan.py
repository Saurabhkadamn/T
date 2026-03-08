"""
app/api/routes/plan.py — POST /api/deepresearch/plan

Implements three invocation modes:

  Mode 1 (fresh):      job_id absent → build fresh state, run from context_builder
  Mode 2 (clarify):    job_id + clarification_answers → load state from DB, run from query_analyzer
  Mode 3 (plan revise):job_id + plan_feedback → load state from DB, run plan_creator

Auth: Keycloak Bearer token introspection via get_current_user().
Rate limiting: daily Redis hash — key: rate:plan:day:{user_id}:{YYYY-MM-DD}
  Each entry: job_id → "planning"|"completed"|"failed"
  Failed entries don't count toward the limit.

MongoDB state is saved after EVERY invocation so the graph can resume
from the correct point on subsequent calls.

Flow
----
1. Validate Keycloak token (get_current_user).
2. Determine mode from request shape.
3. Rate-limit pre-check (read-only count of non-failed entries).
4. Build or reconstruct PlanningState.
5. Consume rate-limit slot (add job_id → "planning" to Redis hash).
6. Run planning_graph.ainvoke with redis injected via configurable.
7. Persist planning_state subdoc to Deep_Research_Jobs (upsert, every call).
8. Return PlanResponse.
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
from app.dependencies import TokenUser, get_current_user, get_mongo, get_redis
from app.tracing import get_callback_handler, inject_trace_carrier
from graphs.planning.graph import planning_graph
from graphs.planning.state import PlanningState

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Daily rate limiting helpers
# ---------------------------------------------------------------------------

# Lua script: atomically count non-failed entries and add new job_id → "planning".
# Returns 0 if daily limit exceeded, 1 if allowed.
_DAILY_RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local job_id = ARGV[2]
local ttl = tonumber(ARGV[3])
local entries = redis.call('HGETALL', key)
local count = 0
for i = 2, #entries, 2 do
    if entries[i] ~= 'failed' then
        count = count + 1
    end
end
if count >= limit then
    return 0
end
redis.call('HSET', key, job_id, 'planning')
if redis.call('TTL', key) == -1 then
    redis.call('EXPIRE', key, ttl)
end
return 1
"""


def _daily_rate_key(user_id: str) -> str:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"rate:plan:day:{user_id}:{date_str}"


async def _check_rate_limit_readonly(redis: Redis | None, user_id: str) -> None:
    """Pre-flight read-only check: count non-failed entries."""
    limit = settings.rate_limit_plan_per_day
    if limit is None or redis is None:
        return
    key = _daily_rate_key(user_id)
    try:
        entries = await redis.hgetall(key)
        count = sum(1 for v in entries.values() if v != "failed")
        if count >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Daily plan limit reached. Try again tomorrow.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("plan: rate-limit Redis error in pre-check (failing open) — %s", exc)


async def _consume_rate_limit(
    redis: Redis | None, user_id: str, job_id: str
) -> None:
    """Atomic Lua: add job_id → 'planning' to daily hash, reject if over limit."""
    limit = settings.rate_limit_plan_per_day
    if limit is None or redis is None:
        return
    key = _daily_rate_key(user_id)
    ttl = settings.rate_limit_plan_daily_ttl_seconds
    try:
        allowed = await redis.eval(_DAILY_RATE_LIMIT_SCRIPT, 1, key, limit, job_id, ttl)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Daily plan limit reached. Try again tomorrow.",
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
    "topic_id", "tenant_id", "user_id", "chat_bot_id", "original_topic",
    "uploaded_files", "chat_history",
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
    topic: str,
    result: dict[str, Any],
    llm_model: str,
    now_iso: str,
    existing_doc: dict | None,
    otel_carrier: dict | None = None,
) -> None:
    """Upsert the Deep_Research_Jobs document after every invocation."""
    graph_status = result.get("status", "unknown")

    if graph_status == "awaiting_approval":
        job_status = "plans_ready"
    elif graph_status == "needs_clarification":
        job_status = "needs_clarification"
    else:
        job_status = graph_status

    created_at = (existing_doc or {}).get("created_at", now_iso)
    # Store UTC date for executor rate-limit updates (avoids timezone issues)
    created_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    job_doc = {
        "job_id": job_id,
        "report_id": (existing_doc or {}).get("report_id"),
        "tenant_id": tenant_id,
        "user_id": user_id,
        "chat_bot_id": "",          # deprecated; kept for schema compat
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
        "planning_state": _extract_planning_state_subdoc(result),
        "metadata": {
            **(existing_doc or {}).get("metadata", {
                "otel_trace_id": "",
                "total_tokens_used": 0,
                "total_cost_usd": 0.0,
                "pro_calls": 0,
                "flash_calls": 0,
            }),
            **({"otel_carrier": otel_carrier, "otel_trace_id": (otel_carrier or {}).get("traceparent", "")} if otel_carrier else {}),
        },
        "started_at": None,
        "completed_at": None,
        "error": result.get("error"),
        "created_at": created_at,
        "created_date": created_date,   # UTC date string for executor rate-limit updates
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
    current_user: TokenUser = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
    mongo: AsyncIOMotorClient = Depends(get_mongo),
) -> PlanResponse:

    tenant_id = current_user.tenant_id
    user_id = current_user.user_id

    # ── Rate limiting (pre-flight, read-only) ──────────────────────────────
    await _check_rate_limit_readonly(redis, user_id)

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

        # Map objects → uploaded_files (state expects list with object_id, filename, mime_type)
        uploaded_files = [
            {
                "object_id": obj.get("object_id", ""),
                "filename": obj.get("filename", ""),
                "mime_type": obj.get("mime_type", ""),
            }
            for obj in body.objects
            if obj.get("object_id")
        ]

        # Map tools → source_scope; always include "web" as minimum
        _valid_tools = {"web", "arxiv", "content_lake", "files"}
        source_scope = [t for t in body.tools if t in _valid_tools]
        if "web" not in source_scope:
            source_scope.insert(0, "web")

        initial_state: PlanningState = {
            "topic_id": job_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "chat_bot_id": "",              # deprecated field; kept for state compat
            "chat_history": [m.model_dump() for m in body.chat_history],
            "uploaded_files": uploaded_files,
            "file_contents": {},
            "context_brief": "",            # context_builder will populate
            "original_topic": body.topic,   # guaranteed by model validator
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
            "source_scope": source_scope,
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
            if existing_doc.get("status") != "needs_clarification":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Job {job_id} is in status '{existing_doc.get('status')}' "
                        "and cannot accept clarification answers. "
                        "Use plan_feedback to revise an existing plan."
                    ),
                )
            initial_state = {**saved_state}  # type: ignore[assignment]
            initial_state["clarification_answers"] = list(body.clarification_answers)
            initial_state["clarification_questions"] = list(
                body.clarification_questions or saved_state.get("clarification_questions", [])
            )
            initial_state["plan"] = None

        else:
            # mode3 — plan revision
            revision_count = saved_state.get("plan_revision_count", 0)
            if revision_count >= settings.loops.plan_revision_max:
                # Max revisions reached — return current plan, client shows approve/cancel only
                return PlanResponse(
                    job_id=job_id,
                    status="awaiting_approval",
                    needs_clarification=False,
                    refined_topic=saved_state.get("refined_topic"),
                    analysis={
                        "depth_of_research": saved_state.get("depth_of_research"),
                        "audience": saved_state.get("audience"),
                        "objective": saved_state.get("objective"),
                        "domain": saved_state.get("domain"),
                        "recency_scope": saved_state.get("recency_scope"),
                        "source_scope": saved_state.get("source_scope"),
                        "assumptions": saved_state.get("assumptions"),
                    },
                    plan=saved_state.get("plan"),
                    checklist=saved_state.get("checklist") or [],
                    max_revisions_reached=True,
                )

            initial_state = {**saved_state}  # type: ignore[assignment]
            initial_state["plan_feedback"] = body.plan_feedback
            initial_state["plan_revision_count"] = revision_count + 1

    # ── Consume rate-limit slot (before graph — Mode 1 only) ───────────────
    # Modes 2 & 3 resume an existing job_id so they don't create new entries.
    if mode == "mode1":
        await _consume_rate_limit(redis, user_id, job_id)

    # ── Run planning graph ─────────────────────────────────────────────────
    # Capture carrier before the graph runs so Langfuse trace links to the request span
    _pre_graph_carrier = inject_trace_carrier()
    lf_handler = get_callback_handler(
        trace_name=f"plan:{job_id}",
        user_id=user_id,
        session_id=job_id,
        metadata={
            "tenant_id": tenant_id,
            "topic": (body.topic or "")[:200],
            "otel_trace_id": _pre_graph_carrier.get("traceparent", ""),
        },
        tags=["planning", tenant_id],
    )

    graph_config: dict = {
        "configurable": {
            "thread_id": job_id,
            "redis_client": redis,
        },
    }
    if lf_handler:
        graph_config["callbacks"] = [lf_handler]

    try:
        result = await asyncio.wait_for(
            planning_graph.ainvoke(initial_state, config=graph_config),
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

    # ── Map final state → error check ──────────────────────────────────────
    if result.get("status") == "failed":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.get("error") or "Planning graph failed with unknown error",
        )

    # ── Capture OTEL carrier (must be called while the request span is active) ─
    otel_carrier = inject_trace_carrier()

    # ── Persist state to MongoDB (EVERY invocation) ────────────────────────
    now_iso = datetime.now(timezone.utc).isoformat()
    llm_model = (
        settings.llm_provider
        + "/"
        + settings.models.model_for_provider("plan_creation", settings.llm_provider)
    )
    result_for_save = {
        **result,
        "uploaded_files": initial_state.get("uploaded_files", []),
        "chat_history": initial_state.get("chat_history", []),
        "topic_id": job_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "chat_bot_id": "",
        "original_topic": initial_state.get("original_topic", body.topic or ""),
    }

    try:
        await _save_job_doc(
            mongo=mongo,
            job_id=job_id,
            tenant_id=tenant_id,
            user_id=user_id,
            topic=initial_state.get("original_topic") or body.topic or "",
            result=result_for_save,
            llm_model=llm_model,
            now_iso=now_iso,
            existing_doc=existing_doc,
            otel_carrier=otel_carrier,
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
