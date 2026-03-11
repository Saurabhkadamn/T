"""
app/api/websocket.py — WS /ws/deep-research/{job_id}

Subscribes to the Redis pub/sub channel job:{job_id}:events and forwards
each JSON event to the connected WebSocket client.

Lifecycle
---------
1. Client connects with ?tenant_id=... query param (or X-Tenant-ID header
   is not available on WebSocket upgrade — use query param instead).
2. Handler subscribes to Redis pub/sub: SUBSCRIBE job:{job_id}:events
3. Forwards each message as JSON text to the client.
4. Closes the connection when:
   - A "completed" or "error" event type is received.
   - The client disconnects.
   - Any unhandled exception occurs.

Fallback (Redis unavailable)
-----------------------------
If Redis pub/sub subscription fails, the handler switches to a MongoDB
polling loop.  It polls Deep_Research_Jobs every 3 seconds, sends
synthetic progress/completed/error events, and closes on terminal state.
The "completed" event's presigned_url will be empty — the client should
call GET /api/deepresearch/reports/{report_id} to obtain a fresh URL.

Event schema (JSON)
-------------------
All events published by worker/streaming.py follow this shape:
  {"event": "<type>", "data": {...}}

Types: section_start | content_delta | progress | completed | error
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from bson import ObjectId

from app.config import settings
from app.enums import ExecutionJobStatus

logger = logging.getLogger(__name__)

router = APIRouter()

_TERMINAL_EVENTS = {"completed", "error"}
_POLL_INTERVAL_SECONDS = 3
_POLL_TIMEOUT_SECONDS = 3600  # 1 hour max polling


# ---------------------------------------------------------------------------
# MongoDB polling fallback
# ---------------------------------------------------------------------------

async def _poll_mongo_fallback(
    websocket: WebSocket,
    mongo: Any,
    job_id: str,
    tenant_id: str,
) -> None:
    """Poll MongoDB for job status when Redis pub/sub is unavailable.

    Sends synthetic WebSocket events so the client is not left hanging.
    Closes the connection on terminal state or after _POLL_TIMEOUT_SECONDS.
    """
    logger.info("ws: entering MongoDB poll fallback for job_id=%s", job_id)
    db = mongo[settings.mongo_db_name]
    last_status: str | None = None
    elapsed = 0

    while elapsed < _POLL_TIMEOUT_SECONDS:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        elapsed += _POLL_INTERVAL_SECONDS

        try:
            doc = await db["Deep_Research_Jobs"].find_one(
                {"_id": ObjectId(job_id), "tenant_id": tenant_id},
                {"status": 1},
            )
        except Exception as exc:
            logger.warning("ws poll: MongoDB query failed — %s", exc)
            continue

        if doc is None:
            continue

        current_status: str = doc.get("status", "")
        if current_status == last_status:
            continue
        last_status = current_status

        if current_status == ExecutionJobStatus.COMPLETED:
            try:
                report_doc = await db["Deep_Research_Reports"].find_one(
                    {"job_id": ObjectId(job_id), "tenant_id": tenant_id},
                    {"report_id": 1},
                )
                report_id = report_doc["report_id"] if report_doc else ""
            except Exception:
                report_id = ""
            # presigned_url is empty — client must call GET /reports/{report_id}
            try:
                await websocket.send_text(json.dumps({
                    "event": "completed",
                    "data": {"report_id": report_id, "presigned_url": ""},
                }))
                await websocket.close()
            except Exception:
                pass
            return

        if current_status == ExecutionJobStatus.FAILED:
            try:
                await websocket.send_text(json.dumps({
                    "event": "error",
                    "data": {
                        "message": f"Job failed (status: {current_status})",
                        "job_id": job_id,
                    },
                }))
                await websocket.close()
            except Exception:
                pass
            return

        # Non-terminal — send a heartbeat so the client knows we're alive
        try:
            await websocket.send_text(json.dumps({
                "event": "progress",
                "data": {"progress": 0, "section": current_status},
            }))
        except WebSocketDisconnect:
            logger.info("ws poll: client disconnected job_id=%s", job_id)
            return
        except Exception:
            return

    # Timed out — close gracefully
    logger.warning("ws poll: timeout reached for job_id=%s", job_id)
    try:
        await websocket.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws/deep-research/{job_id}")
async def deep_research_ws(
    websocket: WebSocket,
    job_id: str,
    tenant_id: str = Query(..., description="Tenant ID for authorization"),
) -> None:
    """Stream deep research job events to a WebSocket client."""
    # ── Origin validation (before accept) ─────────────────────────────────
    allowed = settings.cors_origins
    if "*" not in allowed:
        origin = websocket.headers.get("origin", "")
        if origin not in allowed:
            logger.warning("ws: rejected connection from disallowed origin %r", origin)
            await websocket.close(code=4403)
            return

    await websocket.accept()
    logger.info("ws: client connected job_id=%s tenant_id=%s", job_id, tenant_id)

    # Use the shared Redis client from app state — may be None if Redis is down.
    redis_client = getattr(websocket.app.state, "redis", None)
    pubsub = redis_client.pubsub() if redis_client is not None else None
    channel = f"job:{job_id}:events"

    # ── Tenant ID authorization ────────────────────────────────────────────
    stored_tenant: str | None = None
    if redis_client is not None:
        try:
            stored_tenant = await redis_client.hget(
                f"job:{job_id}:status", "tenant_id"
            )
        except Exception as exc:
            logger.warning("ws: Redis hget failed, falling back to MongoDB — %s", exc)
    if stored_tenant is None:
        # Redis cache miss — fall back to MongoDB
        mongo = websocket.app.state.mongo
        doc = await mongo[settings.mongo_db_name]["Deep_Research_Jobs"].find_one(
            {"_id": ObjectId(job_id)}, {"tenant_id": 1}
        )
        stored_tenant = doc["tenant_id"] if doc else None

    if stored_tenant != tenant_id:
        logger.warning(
            "ws: tenant_id mismatch — closing job_id=%s "
            "(got=%r expected=%r)",
            job_id, tenant_id, stored_tenant,
        )
        await websocket.close(code=4403)
        return

    try:
        # ── Try Redis pub/sub; fall back to MongoDB polling if unavailable ─
        pubsub_available = False
        if pubsub is not None:
            try:
                await pubsub.subscribe(channel)
                logger.debug("ws: subscribed to %s", channel)
                pubsub_available = True
            except Exception as exc:
                logger.warning(
                    "ws: Redis pub/sub unavailable, switching to MongoDB poll fallback — %s", exc
                )
        else:
            logger.warning("ws: Redis client unavailable, switching to MongoDB poll fallback")

        if pubsub_available:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue

                raw: str = message["data"]
                try:
                    await websocket.send_text(raw)
                except WebSocketDisconnect:
                    logger.info("ws: client disconnected job_id=%s", job_id)
                    break

                # Check if this is a terminal event — close cleanly
                try:
                    parsed = json.loads(raw)
                    if parsed.get("event") in _TERMINAL_EVENTS:
                        logger.info(
                            "ws: terminal event %r received — closing job_id=%s",
                            parsed["event"], job_id,
                        )
                        await websocket.close()
                        break
                except (json.JSONDecodeError, KeyError):
                    pass
        else:
            await _poll_mongo_fallback(
                websocket, websocket.app.state.mongo, job_id, tenant_id
            )

    except WebSocketDisconnect:
        logger.info("ws: client disconnected during setup job_id=%s", job_id)
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.exception("ws: unhandled error job_id=%s", job_id)
        try:
            await websocket.send_text(
                json.dumps({"event": "error", "data": {"message": str(exc)}})
            )
            await websocket.close()
        except Exception:
            pass
    finally:
        if pubsub is not None:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception:
                pass
        logger.info("ws: connection cleaned up job_id=%s", job_id)
