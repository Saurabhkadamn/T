"""
graphs/execution/_url_utils.py — Shared URL utility functions.

Centralises url_hash computation so source_verifier.py and scorer.py
always produce identical hashes for the same URL.
"""

from __future__ import annotations

import hashlib


def url_hash(url: str) -> str:
    """SHA-256 of the normalised URL (lowercased, trailing slash stripped).

    Returns an empty string for empty/None URLs — callers must decide
    how to handle the missing-URL case (e.g. skip the record).
    """
    if not url:
        return ""
    return hashlib.sha256(url.lower().rstrip("/").encode()).hexdigest()
