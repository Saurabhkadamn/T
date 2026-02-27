"""
source_verifier — Execution Graph Node (Section Sub-graph, Node 3)

Responsibility
--------------
Verify authenticity of web URLs from raw_search_results and compute
url_hash + domain for each one.  Non-web results (arxiv, files,
content_lake) are inherently trusted and are NOT processed here.

For each web URL:
  1. Issue an HTTP HEAD request (timeout=10s).
  2. Determine is_authentic based on HTTP status code:
       200–299  → authentic
       400–499 (excl. 429) → not authentic (404, 403, 410, etc.)
       429, 500–599 → treat as authentic (rate-limited or server error;
                      benefit of the doubt — don't penalise good sources)
       connection error / timeout → treat as authentic (network unreliable)
  3. Compute SHA-256 url_hash of the normalised URL.
  4. Extract registered domain via urllib.parse (strips "www." prefix).

Output is a list[VerifiedUrl] stored in state["verified_urls"].
Non-web results get an implicit authenticity_score=1.0 from the scorer.

Node contract
-------------
Input  fields consumed: raw_search_results
Output fields written:  verified_urls
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from langchain_core.runnables import RunnableConfig

from graphs.execution.state import RawSearchResult, SectionResearchState, VerifiedUrl

logger = logging.getLogger(__name__)

_HEAD_TIMEOUT = 10.0
_AUTHENTIC_STATUS = range(200, 300)         # 2xx
_INAUTHENTIC_STATUS = {400, 401, 403, 404, 410, 451}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_url(url: str) -> str:
    """Lowercase and strip trailing slash; keep the rest unchanged."""
    return url.lower().rstrip("/")


def _url_hash(url: str) -> str:
    """SHA-256 of the normalised URL, hex-encoded (64 chars)."""
    return hashlib.sha256(_normalise_url(url).encode()).hexdigest()


def _extract_domain(url: str) -> str:
    """Extract registered domain from a URL, stripping 'www.' prefix.

    Uses stdlib urllib.parse — no tldextract dependency.
    Examples:
        "https://www.example.com/page" → "example.com"
        "https://arxiv.org/abs/1234"   → "arxiv.org"
    """
    try:
        netloc = urlparse(url).netloc.lower()
        # Strip port if present.
        domain = netloc.split(":")[0]
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


async def _check_url(url: str) -> VerifiedUrl:
    """Issue a HEAD request and build a VerifiedUrl record."""
    h = _url_hash(url)
    domain = _extract_domain(url)

    try:
        async with httpx.AsyncClient(
            timeout=_HEAD_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "KadalResearchBot/1.0"},
        ) as client:
            resp = await client.head(url)
            status = resp.status_code
    except Exception as exc:
        # Network error or timeout — treat as authentic (benefit of doubt).
        logger.debug(
            "source_verifier: HEAD failed for %r — %s (treating as authentic)",
            url,
            exc,
        )
        return VerifiedUrl(url=url, is_authentic=True, url_hash=h, domain=domain)

    if status in _INAUTHENTIC_STATUS:
        is_authentic = False
    else:
        is_authentic = True

    logger.debug(
        "source_verifier: %r → HTTP %d, authentic=%s", url, status, is_authentic
    )
    return VerifiedUrl(url=url, is_authentic=is_authentic, url_hash=h, domain=domain)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def source_verifier(
    state: SectionResearchState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """LangGraph node: verify web URLs and compute url_hash + domain.

    All HEAD requests run concurrently with asyncio.gather.
    Non-web results are skipped (they don't have verifiable URLs).
    """
    import asyncio

    section_id: str = state.get("section_id", "")
    raw_results: list[RawSearchResult] = state.get("raw_search_results") or []

    # Collect unique web URLs only.
    web_results = [r for r in raw_results if r.get("source_type") == "web" and r.get("url")]
    unique_urls: list[str] = list({r["url"] for r in web_results})

    logger.info(
        "source_verifier: checking %d unique web URL(s) (section_id=%s)",
        len(unique_urls),
        section_id,
    )

    if not unique_urls:
        return {"verified_urls": []}

    tasks = [asyncio.create_task(_check_url(url)) for url in unique_urls]
    verified: list[VerifiedUrl] = await asyncio.gather(*tasks)

    authentic_count = sum(1 for v in verified if v["is_authentic"])
    logger.info(
        "source_verifier: %d/%d URL(s) authentic (section_id=%s)",
        authentic_count,
        len(verified),
        section_id,
    )

    return {"verified_urls": list(verified)}
