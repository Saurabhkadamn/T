"""
db/indexes.py — MongoDB Collection Setup (Layer 2)

Creates all required collections with their correct indexes.
Must be run ONCE before any API or worker code touches the database.

Usage:
    python -m db.indexes          # apply indexes to the configured MONGO_URI
    await setup_all_indexes()     # call from worker startup / migration script

Collections
-----------
background_jobs (existing — add DEEP_RESEARCH type support):
  - {type: 1, status: 1}
  - {ref_id: 1}

deep_research_jobs (new):
  - {job_id: 1}           UNIQUE
  - {tenant_id: 1, user_id: 1}
  - {status: 1}

deep_research_reports (new):
  - {report_id: 1}                            UNIQUE
  - {tenant_id: 1, user_id: 1, executed_at: -1}

source_scores (new):
  - {url_hash: 1, tenant_id: 1}              UNIQUE (compound)
  - {domain: 1}

langgraph_checkpoints (new):
  - Managed by AsyncMongoDBSaver — do not add custom indexes here.
    The saver handles its own index creation on first use.

Critical constraint: every query to every collection MUST include tenant_id.
This is enforced at the application layer, not the DB layer (MongoDB does not
have row-level security).  These indexes are designed to make tenant-scoped
queries efficient.
"""

from __future__ import annotations

import asyncio
import logging

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, IndexModel

logger = logging.getLogger(__name__)

_DB_NAME = "kadal_platform"


# ---------------------------------------------------------------------------
# Index specifications
# ---------------------------------------------------------------------------

_BACKGROUND_JOBS_INDEXES = [
    IndexModel([("type", ASCENDING), ("status", ASCENDING)], name="type_status"),
    IndexModel([("ref_id", ASCENDING)], name="ref_id"),
]

_DEEP_RESEARCH_JOBS_INDEXES = [
    IndexModel([("job_id", ASCENDING)], unique=True, name="job_id_unique"),
    IndexModel(
        [("tenant_id", ASCENDING), ("user_id", ASCENDING)],
        name="tenant_user",
    ),
    IndexModel([("status", ASCENDING)], name="status"),
]

_DEEP_RESEARCH_REPORTS_INDEXES = [
    IndexModel([("report_id", ASCENDING)], unique=True, name="report_id_unique"),
    IndexModel(
        [
            ("tenant_id", ASCENDING),
            ("user_id", ASCENDING),
            ("executed_at", DESCENDING),
        ],
        name="tenant_user_executed_at",
    ),
]

_SOURCE_SCORES_INDEXES = [
    IndexModel(
        [("url_hash", ASCENDING), ("tenant_id", ASCENDING)],
        unique=True,
        name="url_hash_tenant_unique",
    ),
    IndexModel([("domain", ASCENDING)], name="domain"),
]


# ---------------------------------------------------------------------------
# Setup function
# ---------------------------------------------------------------------------

async def setup_all_indexes(mongo_uri: str | None = None) -> None:
    """Create all collections and apply indexes idempotently.

    Safe to call on every deployment — MongoDB's create_indexes() is
    idempotent for existing identical indexes.

    Args:
        mongo_uri: Override MONGO_URI from settings (useful in tests).
    """
    from app.config import settings

    uri = mongo_uri or settings.mongo_uri
    client = AsyncIOMotorClient(uri)
    db = client[_DB_NAME]

    tasks = [
        _apply_indexes(db, "background_jobs", _BACKGROUND_JOBS_INDEXES),
        _apply_indexes(db, "deep_research_jobs", _DEEP_RESEARCH_JOBS_INDEXES),
        _apply_indexes(db, "deep_research_reports", _DEEP_RESEARCH_REPORTS_INDEXES),
        _apply_indexes(db, "source_scores", _SOURCE_SCORES_INDEXES),
        # langgraph_checkpoints: AsyncMongoDBSaver manages its own indexes.
        _ensure_collection_exists(db, "langgraph_checkpoints"),
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for name, result in zip(
        [
            "background_jobs",
            "deep_research_jobs",
            "deep_research_reports",
            "source_scores",
            "langgraph_checkpoints",
        ],
        results,
    ):
        if isinstance(result, Exception):
            logger.error("setup_all_indexes: failed for %r — %s", name, result)
        else:
            logger.info("setup_all_indexes: %r OK", name)

    client.close()


async def _apply_indexes(
    db,
    collection_name: str,
    indexes: list[IndexModel],
) -> None:
    coll = db[collection_name]
    created = await coll.create_indexes(indexes)
    logger.debug(
        "_apply_indexes: %r — created/confirmed %d index(es): %s",
        collection_name,
        len(created),
        created,
    )


async def _ensure_collection_exists(db, collection_name: str) -> None:
    existing = await db.list_collection_names()
    if collection_name not in existing:
        await db.create_collection(collection_name)
        logger.info(
            "_ensure_collection_exists: created collection %r", collection_name
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    async def _main() -> None:
        logger.info("Applying MongoDB indexes …")
        await setup_all_indexes()
        logger.info("Done.")

    asyncio.run(_main())
