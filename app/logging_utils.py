"""
app/logging_utils.py — Shared JSON log formatter for API and Worker processes.

Injects OTEL trace_id/span_id into every log line (set automatically by
LoggingInstrumentor after init_otel() is called).

Usage::
    from app.logging_utils import setup_json_logging
    setup_json_logging("kadal-deepresearch-api")
"""

from __future__ import annotations

import json
import logging
from typing import Any


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": getattr(record, "otelTraceID", ""),
            "span_id": getattr(record, "otelSpanID", ""),
            "service": getattr(record, "_service", "kadal"),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out)


def setup_json_logging(service_name: str = "kadal") -> None:
    """Replace root logger handler with JSON-formatted output.

    Stamps service_name onto every LogRecord so it appears in every log line.
    Safe to call multiple times — clears existing root handlers first.
    """

    class _ServiceFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            record._service = service_name  # type: ignore[attr-defined]
            return True

    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_ServiceFilter())
    logging.root.handlers.clear()
    logging.root.addHandler(handler)
    logging.root.setLevel(logging.INFO)
