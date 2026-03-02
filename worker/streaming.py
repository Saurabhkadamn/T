"""
worker/streaming.py — Redis pub/sub event publisher + job status updater.

All functions are called by worker/executor.py as the execution graph
streams events.  Publishes to the channel that app/api/websocket.py
subscribes to.

Redis key schema
----------------
job:{job_id}:events           → Pub/Sub channel (string messages)
job:{job_id}:status           → Hash {status, progress, section, updated_at}

Event JSON shape
----------------
Every message published to the channel follows this structure:
  {"event": "<type>", "data": {...}}

Event types
-----------
section_start   — {"section_id": "...", "section_title": "..."}
content_delta   — {"section_id": "...", "chunk": "..."}
progress        — {"progress": 42, "section": "..."}
completed       — {"report_id": "...", "presigned_url": "..."}
error           — {"message": "...", "job_id": "..."}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStreamer:
    """Stateful publisher bound to a single job_id.

    Create one instance per worker invocation and call publish_* methods as
    the execution graph progresses.

    Usage::
        streamer = JobStreamer(job_id)
        await streamer.connect()
        try:
            await streamer.publish_progress(10, section="Introduction")
            await streamer.publish_section_start("sec-1", "Introduction")
            ...
            await streamer.publish_completed(report_id, presigned_url)
        finally:
            await streamer.aclose()
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self._channel = f"job:{job_id}:events"
        self._status_key = f"job:{job_id}:status"
        self._ttl = settings.redis_ttls.job_status_ttl_seconds
        self._redis: aioredis.Redis | None = None

    async def connect(self) -> None:
        self._redis = aioredis.Redis(
            host=settings.redis_host,
            password=settings.redis_password or None,
            decode_responses=True,
        )

    async def aclose(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    # ── Internal helpers ───────────────────────────────────────────────────

    async def _publish(self, event_type: str, data: dict) -> None:
        if self._redis is None:
            return
        message = json.dumps({"event": event_type, "data": data})
        try:
            await self._redis.publish(self._channel, message)
        except Exception as exc:
            logger.error("streaming._publish: %s", exc)

    async def _update_status_hash(
        self,
        job_status: str,
        progress: int,
        section: str | None = None,
    ) -> None:
        if self._redis is None:
            return
        mapping: dict[str, str] = {
            "status": job_status,
            "progress": str(progress),
            "updated_at": _now_iso(),
        }
        if section is not None:
            mapping["section"] = section
        try:
            await self._redis.hset(self._status_key, mapping=mapping)
            await self._redis.expire(self._status_key, self._ttl)
        except Exception as exc:
            logger.error("streaming._update_status_hash: %s", exc)

    # ── Public event methods ───────────────────────────────────────────────

    async def publish_section_start(
        self, section_id: str, section_title: str
    ) -> None:
        await self._publish(
            "section_start",
            {"section_id": section_id, "section_title": section_title},
        )
        logger.debug(
            "streaming: section_start job_id=%s section_id=%s", self.job_id, section_id
        )

    async def publish_content_delta(
        self, section_id: str, chunk: str
    ) -> None:
        await self._publish(
            "content_delta",
            {"section_id": section_id, "chunk": chunk},
        )

    async def publish_progress(
        self,
        progress: int,
        section: str | None = None,
        job_status: str = "researching",
    ) -> None:
        await self._publish(
            "progress",
            {"progress": progress, "section": section or ""},
        )
        await self._update_status_hash(job_status, progress, section)
        logger.debug(
            "streaming: progress=%d job_id=%s", progress, self.job_id
        )

    async def publish_completed(
        self,
        report_id: str,
        presigned_url: str,
    ) -> None:
        await self._update_status_hash("completed", 100)
        await self._publish(
            "completed",
            {"report_id": report_id, "presigned_url": presigned_url},
        )
        logger.info(
            "streaming: completed job_id=%s report_id=%s", self.job_id, report_id
        )

    async def publish_error(self, message: str) -> None:
        await self._update_status_hash("failed", 0)
        await self._publish(
            "error",
            {"message": message, "job_id": self.job_id},
        )
        logger.error(
            "streaming: error job_id=%s message=%s", self.job_id, message
        )
