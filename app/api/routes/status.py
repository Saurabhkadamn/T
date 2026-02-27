"""
app/api/routes/status.py — GET /api/deepresearch/status/{job_id}

Returns the current status and progress of a deep research job.

Lookup order
------------
1. Redis hash job:{job_id}:status  (fast path — worker keeps this up to date)
2. MongoDB deep_research_jobs WHERE {job_id, tenant_id}  (fallback + tenant check)

Both lookups always include tenant_id to satisfy the security constraint.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorClient
from redis.asyncio import Redis

from app.api.models import StatusResponse
from app.dependencies import get_mongo, get_redis, get_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter()

_DB_NAME = "kadal_platform"


@router.get(
    "/status/{job_id}",
    response_model=StatusResponse,
    summary="Get job status and progress",
)
async def get_status(
    job_id: str,
    tenant_id: str = Depends(get_tenant_id),
    redis: Redis = Depends(get_redis),
    mongo: AsyncIOMotorClient = Depends(get_mongo),
) -> StatusResponse:
    # ── Redis fast path ────────────────────────────────────────────────────
    redis_key = f"job:{job_id}:status"
    try:
        redis_data = await redis.hgetall(redis_key)
    except Exception as exc:
        logger.warning("status: Redis hgetall failed — %s", exc)
        redis_data = {}

    if redis_data:
        # Redis values are bytes when decode_responses=False, str otherwise.
        # app.state.redis is created with decode_responses=True (set in main.py).
        return StatusResponse(
            job_id=job_id,
            status=redis_data.get("status", "unknown"),
            progress=int(redis_data.get("progress", 0)),
            section=redis_data.get("section") or None,
            started_at=redis_data.get("started_at") or None,
            updated_at=redis_data.get("updated_at") or None,
        )

    # ── MongoDB fallback ───────────────────────────────────────────────────
    db = mongo[_DB_NAME]
    try:
        doc = await db["deep_research_jobs"].find_one(
            {"job_id": job_id, "tenant_id": tenant_id},
            projection={"status": 1, "progress": 1, "started_at": 1, "updated_at": 1, "_id": 0},
        )
    except Exception as exc:
        logger.exception("status: MongoDB query failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error: {exc}",
        ) from exc

    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id!r} not found",
        )

    return StatusResponse(
        job_id=job_id,
        status=doc.get("status", "unknown"),
        progress=int(doc.get("progress", 0)),
        started_at=doc.get("started_at"),
        updated_at=doc.get("updated_at"),
    )
