"""
app/main.py — FastAPI application entry point.

Lifespan
--------
startup:
  1. Run db.indexes.setup_all_indexes() — idempotent, safe on every deploy.
  2. Create shared Redis connection pool → app.state.redis
  3. Create shared MongoDB client → app.state.mongo

shutdown:
  1. Close Redis connection.
  2. Close MongoDB client.

Routers registered
------------------
  /api/deepresearch/plan      ← routes/plan.py
  /api/deepresearch/run       ← routes/run.py
  /api/deepresearch/status    ← routes/status.py
  /api/deepresearch/reports   ← routes/reports.py (list + detail)
  /ws/deep-research/{job_id}  ← websocket.py

Usage::
    uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.routes import plan, reports, run, status
from app.api.websocket import router as ws_router
from app.config import settings
from db.indexes import setup_all_indexes

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan context manager
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Set up shared resources on startup; tear them down on shutdown."""

    # ── Startup ───────────────────────────────────────────────────────────
    logger.info("startup: applying MongoDB indexes …")
    try:
        await setup_all_indexes()
        logger.info("startup: MongoDB indexes OK")
    except Exception as exc:
        logger.error("startup: setup_all_indexes failed — %s", exc)
        # Non-fatal at startup — indexes may already exist from a previous run.

    logger.info("startup: initialising Redis connection pool …")
    try:
        _redis_pool = aioredis.ConnectionPool(
            host=settings.redis_host,
            password=settings.redis_password or None,
            max_connections=settings.redis_max_connections,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
        app.state.redis = aioredis.Redis(connection_pool=_redis_pool)
        logger.info("startup: Redis client ready (max_connections=%d)", settings.redis_max_connections)
    except Exception as exc:
        logger.warning(
            "startup: Redis init failed (%s) — API will start without Redis. "
            "Rate-limiting, caching, and streaming will be unavailable until Redis recovers.",
            exc,
        )
        app.state.redis = None

    # Non-fatal connectivity check — log result, never block startup
    if app.state.redis is not None:
        try:
            await app.state.redis.ping()
            logger.info("startup: Redis ping OK")
        except Exception as exc:
            logger.warning(
                "startup: Redis unreachable (%s) — API will start in degraded mode. "
                "All Redis operations will fail open until Redis recovers.",
                exc,
            )

    logger.info("startup: initialising MongoDB client …")
    app.state.mongo = AsyncIOMotorClient(
        settings.mongo_uri,
        maxPoolSize=settings.mongo_max_pool_size,
        minPoolSize=5,
        serverSelectionTimeoutMS=5_000,
        connectTimeoutMS=10_000,
    )
    logger.info("startup: MongoDB client ready (maxPoolSize=%d)", settings.mongo_max_pool_size)

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────
    logger.info("shutdown: closing Redis …")
    try:
        await app.state.redis.aclose()
    except Exception as exc:
        logger.error("shutdown: Redis close error — %s", exc)

    logger.info("shutdown: closing MongoDB …")
    try:
        app.state.mongo.close()
    except Exception as exc:
        logger.error("shutdown: MongoDB close error — %s", exc)

    logger.info("shutdown: complete")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Kadal AI — Deep Research API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["health"])
async def health() -> dict:
    return {"status": "ok"}


# ── REST routers ─────────────────────────────────────────────────────────
app.include_router(plan.router, prefix="/api/deepresearch", tags=["deep-research"])
app.include_router(run.router, prefix="/api/deepresearch", tags=["deep-research"])
app.include_router(status.router, prefix="/api/deepresearch", tags=["deep-research"])
app.include_router(reports.router, prefix="/api/deepresearch", tags=["deep-research"])

# ── WebSocket router ──────────────────────────────────────────────────────
app.include_router(ws_router, tags=["websocket"])
