"""
worker/dispatcher_main.py — Entry point for the Dispatcher process.

Run as:
    python -m worker.dispatcher_main

K8s Deployment command:
    command: ["python", "-m", "worker.dispatcher_main"]

Environment variables consumed at startup:
    DISPATCHER__WORKER_IMAGE   — required unless DISPATCHER__DRY_RUN=true
    DISPATCHER__DRY_RUN        — set to "true" for local Docker Compose dev
    POD_NAME                   — injected by K8s Downward API for unique consumer name

Graceful shutdown:
    SIGTERM / SIGINT sets dispatcher._running = False, causing the read loop
    to exit cleanly after the current message (if any) is processed.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from app.config import settings
from worker.dispatcher import Dispatcher

# Configure basic logging so the process is observable without a full OTEL setup.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def _main() -> None:
    cfg = settings.dispatcher

    # Fail fast: worker_image is required in non-dry-run mode
    if not cfg.worker_image and not cfg.dry_run:
        logger.error(
            "DISPATCHER__WORKER_IMAGE is not set and DISPATCHER__DRY_RUN is false. "
            "Set the image or enable dry_run mode for local development. Exiting."
        )
        sys.exit(1)

    if cfg.dry_run:
        logger.warning(
            "dispatcher: DRY RUN mode enabled — K8s Jobs will NOT be spawned"
        )

    logger.info(
        "dispatcher: starting (stream=%s group=%s max_concurrent=%d dry_run=%s)",
        cfg.stream_name, cfg.consumer_group, cfg.max_concurrent_jobs, cfg.dry_run,
    )

    dispatcher = Dispatcher()

    # Graceful shutdown: SIGTERM (K8s pod termination) / SIGINT (Ctrl-C)
    loop = asyncio.get_running_loop()

    def _stop() -> None:
        logger.info("dispatcher: shutdown signal received — stopping after current message")
        dispatcher._running = False

    loop.add_signal_handler(signal.SIGTERM, _stop)
    loop.add_signal_handler(signal.SIGINT, _stop)

    await dispatcher.run()
    logger.info("dispatcher: exited cleanly")


if __name__ == "__main__":
    asyncio.run(_main())
