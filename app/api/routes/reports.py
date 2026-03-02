"""
app/api/routes/reports.py

GET /api/deepresearch/reports           — paginated list for a tenant/user
GET /api/deepresearch/reports/{report_id} — detail with fresh S3 presigned URL

Security rules
--------------
- Every query includes tenant_id (mandatory).
- Presigned URLs are generated on each GET with 1 hr TTL — never stored in DB.
- deep_research_reports is NOT touched by chat history clearing.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.models import ReportDetailResponse, ReportListItem, ReportListResponse
from app.config import settings
from app.dependencies import get_mongo, get_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter()

_UUID4_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_S3_KEY_RE = re.compile(
    r"^deep-research/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+/[0-9a-f-]{36}\.html$"
)


def _generate_presigned_url(s3_key: str) -> str | None:
    """Generate a fresh S3 presigned GET URL with TTL from settings.

    Returns None on any AWS error so the caller can still serve metadata.
    """
    # Validate s3_key format before passing to AWS
    if not _S3_KEY_RE.match(s3_key):
        logger.error("reports: suspicious s3_key rejected — %r", s3_key)
        return None
    try:
        s3 = boto3.client("s3", region_name=settings.s3_region)
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": s3_key},
            ExpiresIn=settings.s3_presigned_url_ttl_seconds,
        )
        return url
    except (BotoCoreError, ClientError) as exc:
        logger.error("reports: presigned URL generation failed — %s", exc)
        return None


@router.get(
    "/reports",
    response_model=ReportListResponse,
    summary="List deep research reports for the authenticated tenant/user",
)
async def list_reports(
    user_id: str = Query(..., description="Filter by user_id"),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    tenant_id: str = Depends(get_tenant_id),
    mongo: AsyncIOMotorClient = Depends(get_mongo),
) -> ReportListResponse:
    db = mongo[settings.mongo_db_name]
    query_filter = {"tenant_id": tenant_id, "user_id": user_id}

    try:
        total = await db["Deep_Research_Reports"].count_documents(query_filter)
        cursor = (
            db["Deep_Research_Reports"]
            .find(
                query_filter,
                projection={
                    "_id": 0, "s3_key": 0, "citations": 0,
                },
            )
            .sort("executed_at", -1)
            .skip(skip)
            .limit(limit)
        )
        docs = await cursor.to_list(length=limit)
    except Exception as exc:
        logger.exception("list_reports: MongoDB query failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error: {exc}",
        ) from exc

    items = [
        ReportListItem(
            report_id=doc["report_id"],
            job_id=doc.get("job_id", ""),
            tenant_id=doc["tenant_id"],
            user_id=doc["user_id"],
            topic=doc.get("topic", ""),
            title=doc.get("title", ""),
            status=doc.get("status", "unknown"),
            depth_of_research=doc.get("depth_of_research", ""),
            section_count=doc.get("section_count", 0),
            citation_count=doc.get("citation_count", 0),
            summary=doc.get("summary"),
            llm_model=doc.get("llm_model"),
            executed_at=doc.get("executed_at"),
            created_at=doc.get("created_at", ""),
        )
        for doc in docs
    ]

    return ReportListResponse(items=items, total=total, skip=skip, limit=limit)


@router.get(
    "/reports/{report_id}",
    response_model=ReportDetailResponse,
    summary="Get a single report with a fresh S3 presigned URL",
)
async def get_report(
    report_id: str = Path(..., min_length=36, max_length=36, pattern=_UUID4_PATTERN),
    tenant_id: str = Depends(get_tenant_id),
    mongo: AsyncIOMotorClient = Depends(get_mongo),
) -> ReportDetailResponse:
    db = mongo[settings.mongo_db_name]

    try:
        doc = await db["Deep_Research_Reports"].find_one(
            {"report_id": report_id, "tenant_id": tenant_id},
            projection={"_id": 0},
        )
    except Exception as exc:
        logger.exception("get_report: MongoDB query failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error: {exc}",
        ) from exc

    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Report {report_id!r} not found",
        )

    # Generate fresh presigned URL if report has been uploaded to S3
    html_url: str | None = None
    html_url_expires_at: str | None = None
    s3_key: str | None = doc.get("s3_key")
    if s3_key:
        html_url = _generate_presigned_url(s3_key)
        if html_url:
            expires_dt = datetime.now(timezone.utc) + timedelta(
                seconds=settings.s3_presigned_url_ttl_seconds
            )
            html_url_expires_at = expires_dt.isoformat()

    return ReportDetailResponse(
        report_id=doc["report_id"],
        job_id=doc.get("job_id", ""),
        tenant_id=doc["tenant_id"],
        user_id=doc["user_id"],
        topic=doc.get("topic", ""),
        title=doc.get("title", ""),
        status=doc.get("status", "unknown"),
        depth_of_research=doc.get("depth_of_research", ""),
        section_count=doc.get("section_count", 0),
        citation_count=doc.get("citation_count", 0),
        summary=doc.get("summary"),
        llm_model=doc.get("llm_model"),
        executed_at=doc.get("executed_at"),
        created_at=doc.get("created_at", ""),
        html_url=html_url,
        html_url_expires_at=html_url_expires_at,
        citations=doc.get("citations") or [],
    )
