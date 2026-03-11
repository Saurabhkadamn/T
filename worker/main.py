"""
worker/main.py — EKS Job entry point.

Reads JOB_ID from the EKS_JOB_ID environment variable and runs the full
execution pipeline for that job via worker/executor.py.

Usage (local dev / EKS)::
    EKS_JOB_ID=<uuid> python -m worker.main

Exit codes
----------
0 — job completed successfully (or with partial_success)
1 — job failed / unhandled exception
"""

from __future__ import annotations

import asyncio
import os
import sys

from app.logging_utils import setup_json_logging

# JSON logging must be configured before any other imports that use logging
setup_json_logging("kadal-deepresearch-worker")

import logging  # noqa: E402 — after setup_json_logging

logger = logging.getLogger(__name__)


async def _main() -> None:
    job_id = os.environ.get("EKS_JOB_ID", "").strip()
    if not job_id:
        logger.error("EKS_JOB_ID environment variable is not set or empty")
        sys.exit(1)

    # Initialise OTEL before any graph work starts so the tracer provider is
    # set before get_tracer() / @node_span calls inside executor/nodes.
    # Set OTEL_SERVICE_NAME=kadal-deepresearch-worker in the EKS Job env to
    # distinguish worker spans from API spans in the collector.
    from app.tracing import get_langfuse, init_langfuse, init_otel, shutdown_otel

    init_otel()       # Must be before any graph/node code
    init_langfuse()   # Bug 1 fix: worker needs its own Langfuse init

    logger.info("worker: starting job_id=%s", job_id)

    from worker.executor import run

    try:
        await run(job_id)
        logger.info("worker: finished job_id=%s", job_id)
    finally:
        # Bug 3 fix: flush Langfuse buffer before EKS Job process exits
        try:
            lf = get_langfuse()
            if lf is not None:
                lf.flush()
        except Exception as exc:
            logger.warning("worker: Langfuse flush error — %s", exc)

        # Gap 5 fix: flush OTEL BatchSpanProcessor queue before exit
        shutdown_otel()


if __name__ == "__main__":
    asyncio.run(_main())
