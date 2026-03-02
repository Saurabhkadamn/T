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

import re

from fastapi import Depends, Header, HTTPException, Request, status
from motor.motor_asyncio import AsyncIOMotorClient
from redis.asyncio import Redis

_TENANT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


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

    Raises HTTP 400 if the header is missing, empty, or contains invalid characters.
    Accepts only alphanumeric characters, underscores, and hyphens (max 128 chars).
    """
    tenant_id = x_tenant_id.strip()
    if not _TENANT_ID_RE.match(tenant_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-Tenant-ID format (alphanumeric, _ and - only, max 128 chars)",
        )
    return tenant_id
