"""
exporter — Execution Graph Node (Main pipeline, final node)

Responsibility
--------------
Write all research outputs to durable storage.  No LLM calls.

Operations (in order):
  1. Upload report_html to S3 under the namespaced key:
       deep-research/{tenant_id}/{user_id}/{report_id}.html
     Generates a 1-hour presigned URL for client access.

  2. Upsert the deep_research_reports document in MongoDB.
     Saves citations (trimmed), summary (extracted from HTML), citation_count.
     Every query MUST include tenant_id — no exceptions.

  3. Update the deep_research_jobs document status to "completed"
     (sets completed_at).

  4. Publish final status to Redis (job:{job_id}:status hash).

On any S3 or MongoDB failure:
  - Log the error.
  - Set status="partial_success" with an error message.
  - Do NOT raise — always let the graph terminate gracefully.

S3 key format: deep-research/{tenant_id}/{user_id}/{report_id}.html
S3 presigned URL TTL: 3600 seconds (1 hour)

Node contract
-------------
Input  fields consumed: report_html, s3_key (if already set), tenant_id,
                        user_id, job_id, report_id, citations,
                        plan, topic, refined_topic
Output fields written:  s3_key, status, error (on failure)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import boto3
import redis.asyncio as aioredis
from botocore.config import Config as BotocoreConfig
from motor.motor_asyncio import AsyncIOMotorClient

from langchain_core.runnables import RunnableConfig

from app.config import settings
from app.tracing import node_span
from graphs.execution.state import Citation, ExecutionState

logger = logging.getLogger(__name__)

_S3_KEY_TEMPLATE = "deep-research/{tenant_id}/{user_id}/{report_id}.html"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _s3_key(tenant_id: str, user_id: str, report_id: str) -> str:
    return _S3_KEY_TEMPLATE.format(
        tenant_id=tenant_id, user_id=user_id, report_id=report_id
    )


def _extract_summary(html: str, max_chars: int = 500) -> str:
    """Strip HTML tags and return first max_chars of text content."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


_s3_client = None
_mongo_client: AsyncIOMotorClient | None = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client(
            "s3",
            region_name=settings.s3_region,
            config=BotocoreConfig(
                connect_timeout=10,
                read_timeout=30,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
    return _s3_client


def _get_mongo_client() -> AsyncIOMotorClient:
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = AsyncIOMotorClient(
            settings.mongo_uri, serverSelectionTimeoutMS=5_000
        )
    return _mongo_client


async def close_exporter_clients() -> None:
    """Close module-level singleton clients.

    Call this from worker/executor.py's finally block to ensure Motor's
    background threads don't hang the process on EKS Job exit.
    """
    global _mongo_client
    if _mongo_client is not None:
        _mongo_client.close()
        _mongo_client = None


async def _upload_to_s3(
    html: str, key: str
) -> tuple[str, str | None]:
    """Upload HTML to S3; return (key, presigned_url | None on error)."""
    import asyncio

    def _sync_upload() -> str:
        client = _get_s3_client()
        client.put_object(
            Bucket=settings.s3_bucket,
            Key=key,
            Body=html.encode("utf-8"),
            ContentType="text/html; charset=utf-8",
        )
        presigned = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": key},
            ExpiresIn=settings.s3_presigned_url_ttl_seconds,
        )
        return presigned

    loop = asyncio.get_event_loop()
    try:
        presigned_url = await loop.run_in_executor(None, _sync_upload)
        return key, presigned_url
    except Exception as exc:
        logger.error("exporter: S3 upload failed — %s (key=%s)", exc, key)
        return key, None


async def _upsert_report(
    mongo_client: AsyncIOMotorClient,
    state: ExecutionState,
    s3_key: str,
    now_iso: str,
) -> None:
    """Upsert deep_research_reports document.  Always includes tenant_id."""
    db = mongo_client[settings.mongo_db_name]
    coll = db["Deep_Research_Reports"]

    # Fetch llm_model from deep_research_jobs (not stored in ExecutionState)
    llm_model: str = ""
    try:
        job_doc = await db["Deep_Research_Jobs"].find_one(
            {"job_id": state["job_id"], "tenant_id": state["tenant_id"]},
            projection={"llm_model": 1, "_id": 0},
        )
        if job_doc:
            llm_model = job_doc.get("llm_model", "")
    except Exception as exc:
        logger.warning("exporter: could not fetch llm_model — %s", exc)

    # Trim citations to arch.md schema: [{id, title, url, source_type}]
    raw_citations: list[Citation] = state.get("citations") or []
    trimmed_citations = [
        {
            "id": c.get("citation_id", ""),
            "title": c.get("title", ""),
            "url": c.get("url", ""),
            "source_type": c.get("source_type", "web"),
        }
        for c in raw_citations
    ]

    # Extract summary from HTML
    report_html: str = state.get("report_html") or ""
    summary = _extract_summary(report_html) if report_html else None

    doc = {
        "report_id": state["report_id"],
        "job_id": state["job_id"],
        "tenant_id": state["tenant_id"],          # MUST be present
        "user_id": state["user_id"],
        "topic": state.get("topic", ""),
        "refined_topic": state.get("refined_topic", ""),
        "llm_model": llm_model,
        "s3_key": s3_key,
        "status": "completed",
        "executed_at": now_iso,
        "section_count": len(state.get("sections") or []),
        "citation_count": len(trimmed_citations),
        "summary": summary,
        "citations": trimmed_citations,
        "otel_trace_id": state.get("otel_trace_id", ""),
    }

    await coll.update_one(
        {"report_id": state["report_id"], "tenant_id": state["tenant_id"]},
        {"$set": doc},
        upsert=True,
    )


async def _update_job_status(
    mongo_client: AsyncIOMotorClient,
    job_id: str,
    tenant_id: str,
    status: str,
    now_iso: str,
) -> None:
    """Update deep_research_jobs.  Always includes tenant_id in filter."""
    db = mongo_client[settings.mongo_db_name]
    coll = db["Deep_Research_Jobs"]
    await coll.update_one(
        {"job_id": job_id, "tenant_id": tenant_id},
        {
            "$set": {
                "status": status,
                "completed_at": now_iso,
                "updated_at": now_iso,
            }
        },
    )


async def _publish_redis_status(
    job_id: str, status: str, progress: int, now_iso: str
) -> None:
    """Publish final status update to Redis job hash and pub/sub channel."""
    try:
        client = aioredis.Redis(
            host=settings.redis_host,
            password=settings.redis_password or None,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
        async with client:
            status_key = f"job:{job_id}:status"
            await client.hset(
                status_key,
                mapping={
                    "status": status,
                    "progress": progress,
                    "updated_at": now_iso,
                },
            )
            await client.expire(status_key, settings.redis_ttls.job_status_ttl_seconds)

            # Publish to pub/sub so WebSocket subscribers get notified.
            event = json.dumps({
                "type": "completed" if status == "completed" else "error",
                "status": status,
                "progress": progress,
                "updated_at": now_iso,
            })
            await client.publish(f"job:{job_id}:events", event)
    except Exception as exc:
        logger.error("exporter: Redis publish failed — %s (job_id=%s)", exc, job_id)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

@node_span("exporter")
async def exporter(
    state: ExecutionState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """LangGraph node: export report to S3 and write all MongoDB records.

    Always terminates cleanly — errors set status to partial_success, not fail.
    """
    job_id: str = state.get("job_id", "")
    report_id: str = state.get("report_id", "")
    tenant_id: str = state.get("tenant_id", "")
    user_id: str = state.get("user_id", "")
    report_html: str | None = state.get("report_html")

    now_iso = datetime.now(tz=timezone.utc).isoformat()
    errors: list[str] = []

    logger.info("exporter: starting export (job_id=%s)", job_id)

    # ------------------------------------------------------------------
    # 1. Upload to S3
    # ------------------------------------------------------------------
    key = _s3_key(tenant_id, user_id, report_id)
    final_s3_key = key

    if report_html:
        final_s3_key, presigned_url = await _upload_to_s3(report_html, key)
        if presigned_url:
            logger.info("exporter: S3 upload OK — key=%s", final_s3_key)
        else:
            errors.append("S3 upload failed")
    else:
        errors.append("report_html is None — nothing to upload")

    # ------------------------------------------------------------------
    # 2 & 3. MongoDB writes
    # ------------------------------------------------------------------
    mongo_client = _get_mongo_client()
    try:
        await _upsert_report(mongo_client, state, final_s3_key, now_iso)
        logger.info("exporter: deep_research_reports upserted (job_id=%s)", job_id)

        final_status = "completed" if not errors else "partial_success"
        await _update_job_status(
            mongo_client, job_id, tenant_id, final_status, now_iso
        )
        logger.info(
            "exporter: deep_research_jobs status=%r (job_id=%s)",
            final_status,
            job_id,
        )
    except Exception as exc:
        logger.error(
            "exporter: MongoDB write failed — %s (job_id=%s)", exc, job_id
        )
        errors.append(f"MongoDB write error: {exc}")
        final_status = "partial_success"

    # ------------------------------------------------------------------
    # 5. Redis status update + pub/sub
    # ------------------------------------------------------------------
    progress = 100 if not errors else 90
    await _publish_redis_status(job_id, final_status, progress, now_iso)

    error_msg = "; ".join(errors) if errors else None
    logger.info(
        "exporter: done — status=%r, errors=%d (job_id=%s)",
        final_status,
        len(errors),
        job_id,
    )

    return {
        "s3_key": final_s3_key,
        "status": final_status,
        "error": error_msg,
        "progress": progress,
    }
