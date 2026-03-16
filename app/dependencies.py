"""
app/dependencies.py — Shared FastAPI dependencies.

All clients (Redis, Mongo) read from app.state, which is populated by
the lifespan context manager in app/main.py.  This avoids creating a new
connection on every request.

Usage in a route::

    from app.dependencies import get_redis, get_mongo, get_current_user

    @router.get("/example")
    async def example(
        current_user: TokenUser = Depends(get_current_user),
        redis: Redis = Depends(get_redis),
        mongo: AsyncIOMotorClient = Depends(get_mongo),
    ):
        ...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from motor.motor_asyncio import AsyncIOMotorClient
from redis.asyncio import Redis

from app.config import settings

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")


# ---------------------------------------------------------------------------
# Token user dataclass
# ---------------------------------------------------------------------------

@dataclass
class TokenUser:
    user_id: str
    tenant_id: str
    client_id: str
    email: str
    keycloak_user_id: str = ""
    username: str = ""
    roles: list = None  # type: ignore[assignment]
    user_type: str = "internal"  # "internal" | "external"

    def __post_init__(self) -> None:
        if self.roles is None:
            self.roles = []


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

async def get_redis(request: Request) -> Redis | None:
    """Return the shared async Redis client from app state.

    Returns None if Redis was unavailable at startup.  All callers already
    wrap Redis operations in try/except so a None value simply fails open.
    """
    return getattr(request.app.state, "redis", None)


# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------

async def get_mongo(request: Request) -> AsyncIOMotorClient:
    """Return the shared AsyncIOMotorClient from app state."""
    mongo: AsyncIOMotorClient = request.app.state.mongo
    return mongo


# ---------------------------------------------------------------------------
# Keycloak token validation
# ---------------------------------------------------------------------------

async def get_current_user(token: str = Depends(oauth2_scheme)) -> TokenUser:
    """Validate Keycloak Bearer token and extract user_id + tenant_id.

    Token is sent by calling services as: Authorization: Bearer <token>

    Introspection response conventions:
    - tenant_id: last path segment of the ``iss`` URL
      (e.g. ".../realms/my-tenant" → "my-tenant")
    - user_id: ``user_id`` field for human logins; ``sub`` for service accounts
      (presence of ``username`` key distinguishes the two cases)
    """
    introspect_url = settings.keycloak_introspect_url
    if not introspect_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth service not configured",
        )

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                introspect_url,
                headers={"content-type": "application/x-www-form-urlencoded"},
                data={"token": token},
            )
        payload = resp.json()
    except Exception as exc:
        logger.error("get_current_user: Keycloak introspect failed — %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token validation failed",
        )

    if not payload.get("active"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inactive or invalid",
        )

    iss = payload.get("iss", "")
    iss_tenant_id = iss.rstrip("/").split("/")[-1] if iss else ""
    if iss and not iss_tenant_id:
        logger.warning("get_current_user: could not extract tenant from iss=%r", iss)
    client_id = payload.get("client_id", "")
    email = payload.get("email", "")

    # ── Human user login (has both username and client_id) ──────────────────
    if "username" in payload and "client_id" in payload and "iss" in payload:
        keycloak_user_id = payload.get("user_id", "")
        user_id = payload.get("user_id", "System")
        username = payload.get("username") if user_id != "System" else "System"
        user_roles = payload.get("user_roles")
        roles = (
            [user_roles[0].get("role_name", "offline_access").lower()]
            if user_roles and isinstance(user_roles[0], dict) and user_roles[0].get("role_name")
            else ["offline_access"]
        )
        tenant_id = iss_tenant_id

    # ── Service account / client login (has client_id + iss, no username) ───
    elif "client_id" in payload and "iss" in payload:
        keycloak_user_id = payload.get("sub", "")
        user_id = keycloak_user_id  # no human user_id for service accounts
        username = ""
        roles = ["client_login"]
        # Service accounts may carry an explicit TenantId claim
        tenant_id = payload.get("TenantId", iss_tenant_id)

    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing required claims (client_id / iss)",
        )

    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing required claim: iss / TenantId",
        )

    logger.info(
        "Token validated — client_id=%s tenant_id=%s user_id=%s keycloak_user_id=%s roles=%s",
        client_id,
        tenant_id,
        user_id,
        keycloak_user_id,
        roles,
    )

    return TokenUser(
        user_id=user_id,
        tenant_id=tenant_id,
        client_id=client_id,
        email=email,
        keycloak_user_id=keycloak_user_id,
        username=username,
        roles=roles,
        user_type="internal",
    )


# ---------------------------------------------------------------------------
# Convenience extractors
# ---------------------------------------------------------------------------

async def get_tenant_id(
    current_user: TokenUser = Depends(get_current_user),
) -> str:
    """Extract tenant_id from the validated token."""
    return current_user.tenant_id
