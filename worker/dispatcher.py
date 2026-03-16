"""
worker/dispatcher.py — Redis Streams consumer that spawns Kubernetes Jobs.

Architecture
------------
The Dispatcher is a long-running asyncio process (K8s Deployment) that:

1. Reads from the ``dr:jobs:stream`` consumer group "dispatchers" via XREADGROUP.
2. Guards idempotency: checks MongoDB status == QUEUED before spawning.
3. Enforces back-pressure: waits until in-flight RUNNING jobs < max_concurrent_jobs.
4. Spawns a K8s Job (BatchV1Api) per research job.  Does NOT XACK — the worker
   pod calls XACK via worker/executor._ack_stream_message() on completion.
5. Runs a PEL sweep every pel_check_interval_seconds to recover crashed pods:
   - Terminal status in Mongo → XACK (worker crashed before acking).
   - RUNNING > 2× stuck_job_timeout → mark FAILED + XACK.
   - QUEUED, < 3 deliveries → re-dispatch.
   - QUEUED, >= 3 deliveries → mark FAILED + XACK (poison pill).
   - Malformed job_id → XACK bad message to clear PEL.

Consumer name: ``dispatcher-{POD_NAME}`` where POD_NAME is injected via the
K8s Downward API (see Deployment spec).  If POD_NAME is not set, falls back
to the hostname.

K8s Job naming: ``dr-worker-{job_id[:12]}`` — deterministic so a second
dispatcher replica hitting the same message gets a 409 AlreadyExists, which
is treated as success (safe dedup).

dry_run mode (DISPATCHER__DRY_RUN=true): skips K8s spawn and logs instead.
Used for local Docker Compose development.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis
from bson import ObjectId, errors as bson_errors
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.enums import ExecutionJobStatus

logger = logging.getLogger(__name__)

# Maximum number of re-delivery attempts before poisoning a QUEUED entry
_MAX_DELIVERIES = 3

# A running job that has been in-flight longer than 2× stuck_job_timeout is declared dead
_DEAD_MULTIPLIER = 2


class Dispatcher:
    """Long-running dispatcher: reads Redis Streams and spawns K8s Jobs."""

    def __init__(self) -> None:
        self._running: bool = True
        self._redis: aioredis.Redis | None = None
        self._mongo: AsyncIOMotorClient | None = None
        self._last_pel_check: float = 0.0

        # Consumer name — unique per pod for HA replicas
        pod_name = os.environ.get("POD_NAME") or socket.gethostname()
        self._consumer_name = f"dispatcher-{pod_name}"

        cfg = settings.dispatcher
        self._stream = cfg.stream_name
        self._group = cfg.consumer_group
        self._msgid_prefix = cfg.msgid_key_prefix
        self._block_ms = cfg.block_ms
        self._pel_interval = cfg.pel_check_interval_seconds
        self._stuck_timeout = cfg.stuck_job_timeout_seconds
        self._max_concurrent = cfg.max_concurrent_jobs
        self._dry_run = cfg.dry_run

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        self._redis = aioredis.Redis(
            host=settings.redis_host,
            password=settings.redis_password or None,
            decode_responses=True,
        )
        self._mongo = AsyncIOMotorClient(
            settings.mongo_uri,
            maxPoolSize=settings.mongo_max_pool_size,
        )
        logger.info("dispatcher: connected (consumer=%s)", self._consumer_name)

    async def aclose(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
        if self._mongo is not None:
            self._mongo.close()
        logger.info("dispatcher: closed")

    # ------------------------------------------------------------------
    # Stream / group helpers
    # ------------------------------------------------------------------

    async def _ensure_consumer_group(self) -> None:
        """Create consumer group if it doesn't exist.  MKSTREAM creates stream too."""
        try:
            await self._redis.xgroup_create(
                self._stream, self._group, id="0", mkstream=True
            )
            logger.info(
                "dispatcher: created consumer group %r on stream %r",
                self._group, self._stream,
            )
        except Exception as exc:
            # BUSYGROUP means group already exists — that's fine
            if "BUSYGROUP" in str(exc):
                logger.debug("dispatcher: consumer group already exists")
            else:
                raise

    # ------------------------------------------------------------------
    # MongoDB helpers
    # ------------------------------------------------------------------

    def _db(self):
        return self._mongo[settings.mongo_db_name]

    async def _is_job_still_queued(self, job_id: str) -> bool:
        """Idempotency guard: return True only if the job is still QUEUED."""
        try:
            doc = await self._db()["Deep_Research_Jobs"].find_one(
                {"_id": ObjectId(job_id)},
                {"status": 1},
            )
            return doc is not None and doc.get("status") == ExecutionJobStatus.QUEUED
        except Exception as exc:
            logger.error(
                "dispatcher: MongoDB status check failed for job_id=%s — %s", job_id, exc
            )
            return False  # fail safe: don't spawn if we can't verify

    async def _count_in_flight(self) -> int:
        """Count RUNNING jobs across all tenants."""
        try:
            return await self._db()["Deep_Research_Jobs"].count_documents(
                {"status": ExecutionJobStatus.RUNNING}
            )
        except Exception as exc:
            logger.error("dispatcher: in-flight count failed — %s", exc)
            return 0

    async def _mark_job_failed(self, job_id: str, reason: str) -> None:
        try:
            await self._db()["Deep_Research_Jobs"].update_one(
                {"_id": ObjectId(job_id)},
                {
                    "$set": {
                        "status": ExecutionJobStatus.FAILED,
                        "error": reason,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                },
            )
            logger.info(
                "dispatcher: marked job_id=%s FAILED — %s", job_id, reason
            )
        except Exception as exc:
            logger.error(
                "dispatcher: could not mark job_id=%s FAILED — %s", job_id, exc
            )

    # ------------------------------------------------------------------
    # Kubernetes Job spawning
    # ------------------------------------------------------------------

    def _build_k8s_job_manifest(self, job_id: str) -> dict[str, Any]:
        cfg = settings.dispatcher
        job_name = f"dr-worker-{job_id[:12]}"
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": cfg.k8s_namespace,
                "annotations": {"kadal/job_id": job_id},
            },
            "spec": {
                "ttlSecondsAfterFinished": cfg.worker_job_ttl_seconds,
                "backoffLimit": cfg.worker_backoff_limit,
                "template": {
                    "metadata": {
                        "annotations": {"kadal/job_id": job_id},
                    },
                    "spec": {
                        "serviceAccountName": cfg.worker_service_account,
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "worker",
                                "image": cfg.worker_image,
                                "command": ["python", "-m", "worker.main"],
                                "envFrom": [
                                    {"secretRef": {"name": cfg.worker_env_secret_name}}
                                ],
                                "env": [
                                    {"name": "EKS_JOB_ID", "value": job_id},
                                    {
                                        "name": "OTEL_SERVICE_NAME",
                                        "value": "kadal-deepresearch-worker",
                                    },
                                ],
                                "resources": {
                                    "requests": {
                                        "cpu": cfg.worker_cpu_request,
                                        "memory": cfg.worker_memory_request,
                                    },
                                    "limits": {
                                        "cpu": cfg.worker_cpu_limit,
                                        "memory": cfg.worker_memory_limit,
                                    },
                                },
                            }
                        ],
                    },
                },
            },
        }

    async def _spawn_k8s_job(self, job_id: str) -> bool:
        """Spawn a K8s Job for the given job_id.  Returns True on success."""
        if self._dry_run:
            logger.info(
                "dispatcher: DRY RUN — would spawn K8s Job for job_id=%s", job_id
            )
            return True

        try:
            from kubernetes import client as k8s_client, config as k8s_config  # type: ignore[import]
        except ImportError:
            logger.error(
                "dispatcher: 'kubernetes' package not installed — cannot spawn K8s Job"
            )
            return False

        try:
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                k8s_config.load_kube_config()  # local kubeconfig fallback

            batch_v1 = k8s_client.BatchV1Api()
            manifest = self._build_k8s_job_manifest(job_id)
            cfg = settings.dispatcher

            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: batch_v1.create_namespaced_job(cfg.k8s_namespace, manifest),
            )
            logger.info(
                "dispatcher: spawned K8s Job dr-worker-%s for job_id=%s",
                job_id[:12], job_id,
            )
            return True

        except Exception as exc:
            exc_str = str(exc)
            if "AlreadyExists" in exc_str or "409" in exc_str:
                logger.info(
                    "dispatcher: K8s Job dr-worker-%s already exists (409) — treating as success",
                    job_id[:12],
                )
                return True
            logger.error(
                "dispatcher: failed to spawn K8s Job for job_id=%s — %s", job_id, exc
            )
            return False

    # ------------------------------------------------------------------
    # XACK helper
    # ------------------------------------------------------------------

    async def _xack(self, msg_id: str) -> None:
        try:
            await self._redis.xack(self._stream, self._group, msg_id)
        except Exception as exc:
            logger.error(
                "dispatcher: XACK failed for msg_id=%s — %s", msg_id, exc
            )

    # ------------------------------------------------------------------
    # Message processing
    # ------------------------------------------------------------------

    async def _process_message(self, msg_id: str, fields: dict[str, str]) -> None:
        """Handle a single stream message."""
        job_id = fields.get("job_id", "")

        # Guard: validate job_id is a valid ObjectId to avoid stuck PEL entry
        try:
            ObjectId(job_id)
        except (bson_errors.InvalidId, TypeError):
            logger.error(
                "dispatcher: invalid job_id %r in stream msg_id=%s — XACKing bad message",
                job_id, msg_id,
            )
            await self._xack(msg_id)
            return

        # Guard: idempotency — only proceed if still QUEUED
        if not await self._is_job_still_queued(job_id):
            logger.info(
                "dispatcher: job_id=%s is no longer QUEUED — XACKing (already handled)",
                job_id,
            )
            await self._xack(msg_id)
            return

        # Back-pressure: wait until in-flight < max_concurrent_jobs
        while True:
            in_flight = await self._count_in_flight()
            if in_flight < self._max_concurrent:
                break
            logger.info(
                "dispatcher: back-pressure — %d jobs running (max=%d), sleeping 30s",
                in_flight, self._max_concurrent,
            )
            await asyncio.sleep(30)
            if not self._running:
                return

        # Spawn K8s Job — leave in PEL on failure (dispatcher or PEL sweep will retry)
        success = await self._spawn_k8s_job(job_id)
        if not success:
            logger.warning(
                "dispatcher: spawn failed for job_id=%s — leaving in PEL for retry",
                job_id,
            )
        # NO XACK here — worker calls XACK via executor._ack_stream_message()

    # ------------------------------------------------------------------
    # PEL sweep
    # ------------------------------------------------------------------

    async def _run_pel_sweep(self) -> None:
        """Recover stuck messages in the Pending Entry List."""
        logger.info("dispatcher: starting PEL sweep")
        stuck_ms = self._stuck_timeout * 1000

        try:
            # XPENDING (summary) first to check if there's anything
            pending = await self._redis.xpending(self._stream, self._group)
            if pending.get("pending", 0) == 0:
                logger.debug("dispatcher: PEL is empty — sweep done")
                return

            # Detailed range: XCLAIM entries idle > stuck_job_timeout_ms
            entries = await self._redis.xpending_range(
                self._stream,
                self._group,
                min="-",
                max="+",
                count=100,
                idle=stuck_ms,
            )
        except Exception as exc:
            logger.error("dispatcher: PEL sweep XPENDING failed — %s", exc)
            return

        for entry in entries:
            msg_id: str = entry["message_id"]
            delivery_count: int = entry.get("times_delivered", 0)

            # XCLAIM the entry so we own it for recovery
            try:
                claimed = await self._redis.xclaim(
                    self._stream,
                    self._group,
                    self._consumer_name,
                    min_idle_time=stuck_ms,
                    message_ids=[msg_id],
                )
            except Exception as exc:
                logger.error(
                    "dispatcher: XCLAIM failed for msg_id=%s — %s", msg_id, exc
                )
                continue

            if not claimed:
                continue  # already claimed by another replica

            claimed_fields: dict[str, str] = claimed[0][1] if claimed else {}
            job_id = claimed_fields.get("job_id", "")

            if not job_id:
                logger.warning(
                    "dispatcher: PEL entry msg_id=%s has no job_id — XACKing", msg_id
                )
                await self._xack(msg_id)
                continue

            # Fetch current MongoDB status
            try:
                doc = await self._db()["Deep_Research_Jobs"].find_one(
                    {"_id": ObjectId(job_id)},
                    {"status": 1, "started_at": 1},
                )
            except Exception as exc:
                logger.error(
                    "dispatcher: PEL sweep DB lookup failed for job_id=%s — %s", job_id, exc
                )
                continue

            if doc is None:
                logger.warning(
                    "dispatcher: PEL job_id=%s not found in DB — XACKing", job_id
                )
                await self._xack(msg_id)
                continue

            mongo_status = doc.get("status", "")
            terminal = {
                ExecutionJobStatus.COMPLETED,
                ExecutionJobStatus.PARTIAL_SUCCESS,
                ExecutionJobStatus.FAILED,
            }

            if mongo_status in terminal:
                # Worker completed but crashed before calling XACK
                logger.info(
                    "dispatcher: PEL job_id=%s is terminal (%s) — XACKing",
                    job_id, mongo_status,
                )
                await self._xack(msg_id)

            elif mongo_status == ExecutionJobStatus.RUNNING:
                # Check if it's been running too long (> 2× stuck_job_timeout)
                started_at_raw = doc.get("started_at")
                if started_at_raw:
                    try:
                        started_at = datetime.fromisoformat(started_at_raw)
                        elapsed = (
                            datetime.now(timezone.utc) - started_at
                        ).total_seconds()
                        if elapsed > self._stuck_timeout * _DEAD_MULTIPLIER:
                            logger.warning(
                                "dispatcher: PEL job_id=%s RUNNING for %.0fs (> %ds) — marking FAILED",
                                job_id, elapsed, self._stuck_timeout * _DEAD_MULTIPLIER,
                            )
                            await self._mark_job_failed(
                                job_id, "Stuck RUNNING — dispatcher PEL sweep"
                            )
                            await self._xack(msg_id)
                        else:
                            logger.debug(
                                "dispatcher: PEL job_id=%s RUNNING %.0fs — not yet stuck",
                                job_id, elapsed,
                            )
                    except Exception as exc:
                        logger.error(
                            "dispatcher: PEL started_at parse error for job_id=%s — %s",
                            job_id, exc,
                        )
                else:
                    logger.debug(
                        "dispatcher: PEL job_id=%s RUNNING but no started_at — skipping",
                        job_id,
                    )

            elif mongo_status == ExecutionJobStatus.QUEUED:
                if delivery_count < _MAX_DELIVERIES:
                    logger.info(
                        "dispatcher: PEL job_id=%s still QUEUED (deliveries=%d) — re-dispatching",
                        job_id, delivery_count,
                    )
                    spawned = await self._spawn_k8s_job(job_id)
                    if not spawned:
                        logger.warning(
                            "dispatcher: PEL re-dispatch failed for job_id=%s", job_id
                        )
                else:
                    logger.error(
                        "dispatcher: PEL job_id=%s QUEUED after %d deliveries — marking FAILED (poison pill)",
                        job_id, delivery_count,
                    )
                    await self._mark_job_failed(
                        job_id,
                        f"Dispatcher gave up after {delivery_count} delivery attempts",
                    )
                    await self._xack(msg_id)
            else:
                logger.warning(
                    "dispatcher: PEL job_id=%s has unexpected status %r — skipping",
                    job_id, mongo_status,
                )

        logger.info("dispatcher: PEL sweep complete (%d entries checked)", len(entries))

    # ------------------------------------------------------------------
    # Main read loop
    # ------------------------------------------------------------------

    async def _run_read_loop(self) -> None:
        while self._running:
            # PEL sweep on schedule
            now = time.monotonic()
            if now - self._last_pel_check >= self._pel_interval:
                await self._run_pel_sweep()
                self._last_pel_check = time.monotonic()

            # Blocking read from consumer group
            try:
                results = await self._redis.xreadgroup(
                    groupname=self._group,
                    consumername=self._consumer_name,
                    streams={self._stream: ">"},
                    count=1,
                    block=self._block_ms,
                )
            except aioredis.ConnectionError as exc:
                logger.error(
                    "dispatcher: Redis connection error — %s. Retrying in 5s.", exc
                )
                await asyncio.sleep(5)
                continue
            except Exception as exc:
                logger.error(
                    "dispatcher: XREADGROUP error — %s. Retrying in 5s.", exc
                )
                await asyncio.sleep(5)
                continue

            if not results:
                continue  # BLOCK timeout — loop again

            for _stream_name, messages in results:
                for msg_id, fields in messages:
                    await self._process_message(msg_id, fields)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect, bootstrap consumer group, then enter read loop."""
        await self.connect()
        try:
            await self._ensure_consumer_group()
            await self._run_read_loop()
        finally:
            await self.aclose()
