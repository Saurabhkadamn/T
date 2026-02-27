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
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

logger = logging.getLogger(__name__)


async def _main() -> None:
    job_id = os.environ.get("EKS_JOB_ID", "").strip()
    if not job_id:
        logger.error("EKS_JOB_ID environment variable is not set or empty")
        sys.exit(1)

    logger.info("worker: starting job_id=%s", job_id)

    from worker.executor import run

    await run(job_id)

    logger.info("worker: finished job_id=%s", job_id)


if __name__ == "__main__":
    asyncio.run(_main())
