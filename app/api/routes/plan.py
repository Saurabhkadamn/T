"""
app/api/routes/plan.py — POST /api/deepresearch/plan

Runs the planning graph synchronously (ainvoke) and returns either
clarification questions or a ready-to-approve research plan.

Flow
----
1. Validate tenant_id (from X-Tenant-ID header via dependency).
2. Rate-limit check — Redis counter: rate:plan:{tenant_id}:{user_id}
3. Load file contents from Redis via tools/file_content.py.
4. Build PlanningState TypedDict from request body.
5. Invoke planning_graph.ainvoke(state, config={"configurable":{"thread_id": job_id}}).
6. On status=="awaiting_approval" → INSERT deep_research_jobs document.
7. Return PlanResponse based on final state:
   - needs_clarification=True  → return questions (graph stopped at clarifier)
   - plan is set               → return plan + checklist
   - status=failed             → raise HTTP 500
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorClient
from redis.asyncio import Redis

from app.api.models import PlanRequest, PlanResponse
from app.config import settings
from app.dependencies import get_mongo, get_redis, get_tenant_id
from graphs.planning.graph import planning_graph
from graphs.planning.state import PlanningState
from tools.file_content import get_multiple_file_contents

logger = logging.getLogger(__name__)

router = APIRouter()

_DB_NAME = "kadal_platform"

_RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local current = tonumber(redis.call('GET', key) or '0')
if current >= limit then
    return 0
end
redis.call('INCR', key)
redis.call('EXPIRE', key, ttl)
return 1
"""


async def _check_rate_limit(
    redis: Redis,
    tenant_id: str,
    user_id: str,
) -> None:
    """Raise HTTP 429 if the tenant/user has exceeded the plan rate limit."""
    limit = settings.rate_limit_plan_per_hour
    if limit is None:
        return  # unlimited

    key = f"rate:plan:{tenant_id}:{user_id}"
    ttl = settings.redis_ttls.rate_limit_plan_ttl_seconds
    allowed = await redis.eval(_RATE_LIMIT_SCRIPT, 1, key, limit, ttl)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Plan rate limit exceeded. Try again later.",
        )


@router.post(
    "/plan",
    response_model=PlanResponse,
    summary="Run planning graph and return plan or clarification questions",
)
async def plan(
    body: PlanRequest,
    tenant_id: str = Depends(get_tenant_id),
    redis: Redis = Depends(get_redis),
    mongo: AsyncIOMotorClient = Depends(get_mongo),
) -> PlanResponse:
    # ── Tenant consistency check ───────────────────────────────────────────
    if body.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tenant_id in body must match X-Tenant-ID header",
        )

    # ── Rate limiting ──────────────────────────────────────────────────────
    await _check_rate_limit(redis, tenant_id, body.user_id)

    # ── Determine job_id ──────────────────────────────────────────────────
    # New session → generate; continuation (answering clarifications) → reuse
    job_id = body.job_id or str(uuid.uuid4())

    # ── Load file contents from Redis ──────────────────────────────────────
    object_ids = [f.object_id for f in body.uploaded_files]
    file_contents = await get_multiple_file_contents(object_ids) if object_ids else {}

    # ── Build initial PlanningState ────────────────────────────────────────
    # The graph field is still named topic_id; job_id is passed as that value.
    initial_state: PlanningState = {
        "topic_id": job_id,
        "tenant_id": tenant_id,
        "user_id": body.user_id,
        "chat_bot_id": body.chat_bot_id,
        "chat_history": [
            {"role": m.role, "content": m.content} for m in body.chat_history
        ],
        "uploaded_files": [
            {"object_id": f.object_id, "filename": f.filename, "mime_type": f.mime_type}
            for f in body.uploaded_files
        ],
        "file_contents": file_contents,
        "original_topic": body.topic,
        "refined_topic": "",
        "needs_clarification": False,
        "clarification_questions": [],
        "clarification_answers": list(body.clarification_answers),
        "clarification_count": 0,
        "depth_of_research": "intermediate",
        "audience": "",
        "objective": "",
        "domain": "",
        "recency_scope": "",
        "source_scope": [],
        "assumptions": [],
        "plan": None,
        "plan_approved": False,
        "plan_revision_count": 0,
        "checklist": [],
        "self_review_count": 0,
        "status": "pending",
        "error": None,
    }

    # ── Run planning graph ─────────────────────────────────────────────────
    try:
        result = await planning_graph.ainvoke(
            initial_state,
            config={"configurable": {"thread_id": job_id}},
        )
    except Exception as exc:
        logger.exception("plan: planning graph raised an exception")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Planning graph error: {exc}",
        ) from exc

    # ── Map final state → response ─────────────────────────────────────────
    if result.get("status") == "failed":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.get("error") or "Planning graph failed with unknown error",
        )

    graph_status: str = result.get("status", "unknown")
    needs_clarification: bool = result.get("needs_clarification", False)

    # ── Save to DB on plan ready ───────────────────────────────────────────
    # Only insert when the plan is fully reviewed and ready for approval.
    # On clarification loops (needs_clarification=True) the job is not saved yet.
    if graph_status == "awaiting_approval" and result.get("plan"):
        now_iso = datetime.now(timezone.utc).isoformat()
        llm_model = (
            settings.llm_provider
            + "/"
            + settings.models.model_for("plan_creation")
        )
        job_doc = {
            "job_id": job_id,
            "report_id": None,
            "tenant_id": tenant_id,
            "user_id": body.user_id,
            "chat_bot_id": body.chat_bot_id,
            "topic": body.topic,
            "refined_topic": result.get("refined_topic", ""),
            "llm_model": llm_model,
            "status": "plans_ready",
            "progress": 0,
            "plan": dict(result["plan"]) if result.get("plan") else None,
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
            "uploaded_files": [f.model_dump() for f in body.uploaded_files],
            "tools_enabled": {},
            "metadata": {
                "otel_trace_id": "",
                "total_tokens_used": 0,
                "total_cost_usd": 0.0,
                "pro_calls": 0,
                "flash_calls": 0,
            },
            "started_at": None,
            "completed_at": None,
            "error": None,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        try:
            db = mongo[_DB_NAME]
            await db["deep_research_jobs"].replace_one(
                {"job_id": job_id, "tenant_id": tenant_id},
                job_doc,
                upsert=True,
            )
        except Exception as exc:
            logger.error("plan: failed to save deep_research_jobs — %s", exc)
            # Non-fatal: still return the plan to the client

    # ── Build analysis object for response ─────────────────────────────────
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
        clarification_questions=result.get("clarification_questions", []),
        refined_topic=result.get("refined_topic") or None,
        analysis=analysis_dict,
        plan=dict(result["plan"]) if result.get("plan") else None,
        checklist=result.get("checklist", []),
        error=result.get("error"),
    )
