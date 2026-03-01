"""
worker/executor.py — Execution graph runner.

Called by worker/main.py with a job_id.  Loads the job from MongoDB,
builds the initial ExecutionState, runs the execution graph, and streams
progress events to Redis pub/sub via worker/streaming.py.

Steps
-----
1. Load deep_research_jobs document from MongoDB (tenant_id always included).
2. Load file contents from Redis via tools/file_content.get_multiple_file_contents.
3. Build initial ExecutionState from the job document.
4. Call build_execution_graph() → (compiled_graph, checkpointer).
5. Stream: async for event in graph.astream(state, config=config).
6. Parse stream events to publish section_start / progress / content_delta.
7. On completion: streamer.publish_completed().
8. On exception: streamer.publish_error(), update job status in MongoDB.
9. Always: close checkpointer + streamer.

Checkpointing
-------------
thread_id = job_id, so if the EKS Job is restarted, the graph resumes from
the last completed node — completed sections are never re-executed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from graphs.execution.graph import build_execution_graph
from graphs.execution.state import ExecutionState, ToolsEnabled
from tools.file_content import get_multiple_file_contents
from worker.streaming import JobStreamer

logger = logging.getLogger(__name__)

_DB_NAME = "kadal_platform"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run(job_id: str) -> None:
    """Entry point called by worker/main.py.

    Runs the full execution pipeline for the given job_id.
    """
    streamer = JobStreamer(job_id)
    mongo_client: AsyncIOMotorClient | None = None
    redis_client: aioredis.Redis | None = None
    checkpointer = None
    tenant_id: str = ""  # captured early so error handler can use it

    try:
        await streamer.connect()
        await streamer.publish_progress(0, job_status="initializing")

        # ── 1. Load job document ───────────────────────────────────────────
        mongo_client = AsyncIOMotorClient(settings.mongo_uri)
        redis_client = aioredis.Redis(
            host=settings.redis_host,
            password=settings.redis_password or None,
            decode_responses=True,
        )
        db = mongo_client[_DB_NAME]

        job_doc = await db["deep_research_jobs"].find_one({"job_id": job_id})
        if job_doc is None:
            raise ValueError(f"Job {job_id!r} not found in deep_research_jobs")

        tenant_id = job_doc["tenant_id"]

        # ── 2. Load file contents from Redis ───────────────────────────────
        uploaded_files: list[dict] = job_doc.get("uploaded_files", [])
        object_ids = [f["object_id"] for f in uploaded_files]
        file_contents = await get_multiple_file_contents(object_ids) if object_ids else {}

        # ── 3. Build initial ExecutionState ────────────────────────────────
        # depth_of_research is nested under analysis in the new schema
        analysis: dict = job_doc.get("analysis") or {}
        depth = analysis.get("depth_of_research") or job_doc.get("depth_of_research", "intermediate")
        depth_cfg = settings.get_depth_config(depth)

        tools_raw: dict = job_doc.get("tools_enabled", {})
        tools_enabled: ToolsEnabled = {
            "web": bool(tools_raw.get("web", True)),
            "arxiv": bool(tools_raw.get("arxiv", True)),
            "content_lake": bool(tools_raw.get("content_lake", False)),
            "files": bool(tools_raw.get("files", False)),
        }

        plan = job_doc.get("plan", {})
        sections = plan.get("sections", [])

        initial_state: ExecutionState = {
            "job_id": job_id,
            "report_id": job_doc["report_id"],
            "tenant_id": tenant_id,
            "user_id": job_doc["user_id"],
            "topic": job_doc.get("topic", ""),
            "refined_topic": job_doc.get("refined_topic", ""),
            "plan": plan,
            "checklist": job_doc.get("checklist", []),
            "sections": sections,
            "attachments": uploaded_files,
            "file_contents": file_contents,
            "tools_enabled": tools_enabled,
            "max_search_iterations": depth_cfg.max_search_iterations,
            "max_sources_per_section": depth_cfg.max_sources_per_section,
            "section_results": [],
            "compressed_findings": [],
            "citations": [],
            "source_scores": [],
            "reflection_count": 0,
            "knowledge_gaps": [],
            "fused_knowledge": None,
            "report_html": None,
            "review_feedback": None,
            "revision_count": 0,
            "s3_key": None,
            "progress": 0,
            "status": "initializing",
            "error": None,
            "otel_trace_id": "",
        }

        # Mark job as running in MongoDB
        started_at = _now_iso()
        await db["deep_research_jobs"].update_one(
            {"job_id": job_id, "tenant_id": tenant_id},
            {"$set": {
                "status": "research",
                "started_at": started_at,
                "updated_at": started_at,
            }},
        )

        # Persist started_at to Redis status hash for the fast-path status endpoint
        try:
            await redis_client.hset(
                f"job:{job_id}:status",
                mapping={"started_at": started_at},
            )
        except Exception as exc:
            logger.warning("executor: could not write started_at to Redis — %s", exc)

        # ── 4. Build execution graph ───────────────────────────────────────
        graph, checkpointer = await build_execution_graph()
        config = {"configurable": {"thread_id": job_id}}

        await streamer.publish_progress(5, job_status="research")

        # ── 5. Stream execution graph events ──────────────────────────────
        total_sections = max(len(sections), 1)
        sections_done = 0

        async for event in graph.astream(initial_state, config=config):
            node_name = next(iter(event), None)
            node_output = event.get(node_name, {}) if node_name else {}

            if node_name == "supervisor":
                node_status = node_output.get("status", "")
                if node_status == "researching":
                    await streamer.publish_progress(10, job_status="researching")

            elif node_name == "section_subgraph":
                sections_done += 1
                pct = 10 + int(70 * sections_done / total_sections)
                section_title = ""
                results = node_output.get("section_results", [])
                if results:
                    section_title = results[-1].get("section_title", "")
                await streamer.publish_progress(
                    pct, section=section_title, job_status="researching"
                )
                if section_title:
                    section_id = results[-1].get("section_id", "")
                    await streamer.publish_section_start(section_id, section_title)

            elif node_name == "knowledge_fusion":
                await streamer.publish_progress(82, job_status="fusing")

            elif node_name == "report_writer":
                await streamer.publish_progress(90, job_status="writing")

            elif node_name == "report_reviewer":
                await streamer.publish_progress(95, job_status="reviewing")

            elif node_name == "exporter":
                await streamer.publish_progress(99, job_status="exporting")

        # ── 6. Fetch final state for completed event ───────────────────────
        final_checkpoint = await checkpointer.aget(config)
        final_state: dict = (
            final_checkpoint.get("channel_values", {}) if final_checkpoint else {}
        )
        report_id = final_state.get("report_id", job_doc["report_id"])
        s3_key = final_state.get("s3_key", "")

        # Generate presigned URL for the completed event
        presigned_url = ""
        if s3_key:
            import boto3
            s3 = boto3.client("s3")
            try:
                presigned_url = s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": settings.s3_bucket, "Key": s3_key},
                    ExpiresIn=3600,
                )
            except Exception as exc:
                logger.error("executor: presigned URL failed — %s", exc)

        await streamer.publish_completed(report_id, presigned_url)

        # Update MongoDB job status to completed
        await db["deep_research_jobs"].update_one(
            {"job_id": job_id, "tenant_id": tenant_id},
            {"$set": {"status": "completed", "progress": 100, "updated_at": _now_iso()}},
        )

    except Exception as exc:
        logger.exception("executor: job_id=%s failed — %s", job_id, exc)
        await streamer.publish_error(str(exc))

        # Best-effort: mark job as failed in MongoDB
        if mongo_client is not None:
            try:
                db = mongo_client[_DB_NAME]
                update_filter: dict = {"job_id": job_id}
                if tenant_id:
                    update_filter["tenant_id"] = tenant_id
                await db["deep_research_jobs"].update_one(
                    update_filter,
                    {
                        "$set": {
                            "status": "failed",
                            "error": str(exc),
                            "updated_at": _now_iso(),
                        }
                    },
                )
            except Exception as inner_exc:
                logger.error("executor: failed to update job status — %s", inner_exc)

    finally:
        if checkpointer is not None:
            try:
                await checkpointer.aclose()
            except Exception:
                pass
        if redis_client is not None:
            try:
                await redis_client.aclose()
            except Exception:
                pass
        if mongo_client is not None:
            mongo_client.close()
        await streamer.aclose()
        logger.info("executor: finished job_id=%s", job_id)
