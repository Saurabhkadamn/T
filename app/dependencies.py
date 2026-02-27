"""
app/dependencies.py — Shared FastAPI dependencies.

All three dependencies read clients from app.state, which is populated by
the lifespan context manager in app/main.py.  This avoids creating a new
connection on every request.

Usage in a route::

    from app.dependencies import get_redis, get_mongo, get_tenant_id

    @router.get("/example")
    async def example(
        tenant_id: str = Depends(get_tenant_id),
        redis: Redis = Depends(get_redis),
        mongo: AsyncIOMotorClient = Depends(get_mongo),
    ):
        ...
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status
from motor.motor_asyncio import AsyncIOMotorClient
from redis.asyncio import Redis


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

async def get_redis(request: Request) -> Redis:
    """Return the shared async Redis client from app state."""
    redis: Redis = request.app.state.redis
    return redis


# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------

async def get_mongo(request: Request) -> AsyncIOMotorClient:
    """Return the shared AsyncIOMotorClient from app state."""
    mongo: AsyncIOMotorClient = request.app.state.mongo
    return mongo


# ---------------------------------------------------------------------------
# Tenant ID extraction
# ---------------------------------------------------------------------------

async def get_tenant_id(
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
) -> str:
    """Extract and validate the tenant_id from the X-Tenant-ID request header.

    Raises HTTP 400 if the header is missing or empty.
    FastAPI's Header(...) already enforces presence; this guard handles
    whitespace-only values.
    """
    tenant_id = x_tenant_id.strip()
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Tenant-ID header must not be empty",
        )
    return tenant_id
