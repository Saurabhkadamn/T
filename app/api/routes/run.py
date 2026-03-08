"""
app/api/routes/run.py — POST /api/deepresearch/run

Loads the approved plan from deep_research_jobs, creates the report record,
queues a background_jobs entry, and returns job_id + report_id to the client.
The actual execution graph runs in the EKS Worker (worker/executor.py).

Flow
----
1. Validate tenant_id.
2. Rate-limit check — Redis counter: rate:run:{tenant_id}:{user_id}
3. Load deep_research_jobs by {job_id, tenant_id} — 404 if missing.
4. Validate status == "plans_ready" — 400 otherwise.
5. Generate report_id = uuid4().
6. UPDATE deep_research_jobs {status: research_queued, report_id, updated_at}.
7. INSERT deep_research_reports document (status=research_queued).
8. INSERT background_jobs document (type=DEEP_RESEARCH, ref_id=job_id) — non-fatal.
9. Set Redis hash job:{job_id}:status.
10. Return RunResponse(job_id, report_id, status="research_queued").
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorClient
from redis.asyncio import Redis

from app.api.models import RunRequest, RunResponse
from app.config import settings
from app.dependencies import TokenUser, get_current_user, get_mongo, get_redis
from app.tracing import inject_trace_carrier

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


async def _check_rate_limit(
    redis: Redis | None,
    tenant_id: str,
    user_id: str,
) -> None:
    limit = settings.rate_limit_run_per_hour
    if limit is None or redis is None:
        return

    key = f"rate:run:{tenant_id}:{user_id}"
    ttl = settings.redis_ttls.rate_limit_run_ttl_seconds
    try:
        allowed = await redis.eval(_RATE_LIMIT_SCRIPT, 1, key, limit, ttl)
    except Exception as exc:
        logger.warning("run: rate-limit Redis error (failing open) — %s", exc)
        return  # fail open
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Run rate limit exceeded. Try again later.",
        )


@router.post(
    "/run",
    response_model=RunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Approve plan and queue a deep research job for async execution",
)
async def run(
    body: RunRequest,
    current_user: TokenUser = Depends(get_current_user),
    redis: Redis | None = Depends(get_redis),
    mongo: AsyncIOMotorClient = Depends(get_mongo),
) -> RunResponse:
    tenant_id = current_user.tenant_id
    user_id = current_user.user_id

    # ── Rate limiting ──────────────────────────────────────────────────────
    await _check_rate_limit(redis, tenant_id, user_id)

    db = mongo[settings.mongo_db_name]

    # ── Load plan from deep_research_jobs ─────────────────────────────────
    try:
        job_doc = await db["Deep_Research_Jobs"].find_one(
            {"job_id": body.job_id, "tenant_id": tenant_id}
        )
    except Exception as exc:
        logger.exception("run: MongoDB find_one failed for job_id=%s", body.job_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error: {exc}",
        ) from exc

    if job_doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {body.job_id!r} not found",
        )

    # ── Handle cancel action ───────────────────────────────────────────────
    if body.action == "cancel":
        try:
            await db["Deep_Research_Jobs"].update_one(
                {"job_id": body.job_id, "tenant_id": tenant_id},
                {"$set": {"status": "cancelled", "updated_at": datetime.now(timezone.utc).isoformat()}},
            )
        except Exception as exc:
            logger.exception("run: failed to cancel job_id=%s", body.job_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to cancel job: {exc}",
            ) from exc
        logger.info("run: cancelled job_id=%s tenant_id=%s", body.job_id, tenant_id)
        return RunResponse(job_id=body.job_id, status="cancelled")

    if job_doc.get("status") != "plans_ready":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Job {body.job_id!r} is not in plans_ready state "
                f"(current: {job_doc.get('status')!r})"
            ),
        )

    # ── Generate report_id ─────────────────────────────────────────────────
    report_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()

    # ── Inject OTEL carrier so Worker can continue the same trace ─────────
    otel_carrier = inject_trace_carrier()

    # ── Update deep_research_jobs ──────────────────────────────────────────
    try:
        await db["Deep_Research_Jobs"].update_one(
            {"job_id": body.job_id, "tenant_id": tenant_id},
            {"$set": {
                "status": "research_queued",
                "report_id": report_id,
                "updated_at": now_iso,
                "metadata.otel_carrier": otel_carrier,
                "metadata.otel_trace_id": otel_carrier.get("traceparent", ""),
            }},
        )
    except Exception as exc:
        logger.exception("run: failed to update deep_research_jobs")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update job: {exc}",
        ) from exc

    # ── Insert deep_research_reports ──────────────────────────────────────
    plan: dict = job_doc.get("plan") or {}
    report_doc = {
        "report_id": report_id,
        "job_id": body.job_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "topic": job_doc.get("topic", ""),
        "title": plan.get("title", job_doc.get("topic", "")),
        "chat_bot_id": job_doc.get("chat_bot_id", ""),
        "llm_model": job_doc.get("llm_model", ""),
        "depth_of_research": job_doc.get("analysis", {}).get(
            "depth_of_research", "intermediate"
        ),
        "section_count": len(plan.get("sections", [])),
        "citation_count": 0,
        "summary": None,
        "citations": [],
        "status": "research_queued",
        "s3_key": None,
        "executed_at": None,
        "created_at": now_iso,
    }
    try:
        await db["Deep_Research_Reports"].insert_one(report_doc)
    except Exception as exc:
        logger.exception("run: failed to insert deep_research_reports")
        # Best-effort rollback of job status update
        await db["Deep_Research_Jobs"].update_one(
            {"job_id": body.job_id, "tenant_id": tenant_id},
            {"$set": {"status": "plans_ready", "report_id": None, "updated_at": now_iso}},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create report record: {exc}",
        ) from exc

    # ── Insert Background_Jobs ─────────────────────────────────────────────
    bg_doc = {
        "job_id": body.job_id,
        "job_type": "DEEP_RESEARCH",
        "tenant_id": tenant_id,
        "source_path": "",
        "call_back_url": None,
        "job_submitted_date": now_iso,
        "job_status": "QUEUED",
        "error": None,
        "job_completed_date": None,
    }
    try:
        await db["Background_Jobs"].insert_one(bg_doc)
    except Exception as exc:
        # Non-fatal — job/report documents exist; the platform scheduler can
        # still pick up the job from deep_research_jobs directly.
        logger.error("run: failed to insert background_jobs — %s", exc)

    # ── Set Redis status hash ──────────────────────────────────────────────
    redis_key = f"job:{body.job_id}:status"
    ttl = settings.redis_ttls.job_status_ttl_seconds
    try:
        await redis.hset(
            redis_key,
            mapping={
                "status": "research_queued",
                "progress": "0",
                "tenant_id": tenant_id,
                "updated_at": now_iso,
            },
        )
        await redis.expire(redis_key, ttl)
    except Exception as exc:
        logger.error("run: failed to set Redis status hash — %s", exc)

    logger.info(
        "run: queued job_id=%s report_id=%s tenant_id=%s",
        body.job_id, report_id, tenant_id,
    )
    return RunResponse(job_id=body.job_id, report_id=report_id, status="research_queued")
