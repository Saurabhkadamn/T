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

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)

router = APIRouter()

_TERMINAL_EVENTS = {"completed", "error"}
_DB_NAME = "kadal_platform"


@router.websocket("/ws/deep-research/{job_id}")
async def deep_research_ws(
    websocket: WebSocket,
    job_id: str,
    tenant_id: str = Query(..., description="Tenant ID for authorization"),
) -> None:
    """Stream deep research job events to a WebSocket client."""
    await websocket.accept()
    logger.info("ws: client connected job_id=%s tenant_id=%s", job_id, tenant_id)

    # Use the shared Redis client from app state — no new connection per request.
    redis_client = websocket.app.state.redis
    pubsub = redis_client.pubsub()
    channel = f"job:{job_id}:events"

    # ── Tenant ID authorization ────────────────────────────────────────────
    stored_tenant: str | None = await redis_client.hget(
        f"job:{job_id}:status", "tenant_id"
    )
    if stored_tenant is None:
        # Redis cache miss — fall back to MongoDB
        mongo: AsyncIOMotorClient = websocket.app.state.mongo
        doc = await mongo[_DB_NAME]["deep_research_jobs"].find_one(
            {"job_id": job_id}, {"tenant_id": 1}
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
        await pubsub.subscribe(channel)
        logger.debug("ws: subscribed to %s", channel)

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
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
        except Exception:
            pass
        logger.info("ws: connection cleaned up job_id=%s", job_id)
