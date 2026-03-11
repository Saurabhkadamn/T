"""
app/tracing.py — Observability singletons: Langfuse (LLM tracing) + OTEL (distributed tracing).

Langfuse
--------
Tracks LLM prompt/completion pairs, token costs, and latency.
All other modules import via ``get_callback_handler()`` — no direct langfuse imports elsewhere.

OpenTelemetry
-------------
Provides cross-service distributed tracing so a single trace_id spans the
FastAPI request → MongoDB job → EKS Worker → LangGraph nodes path.

Usage::
    # At startup (lifespan):
    from app.tracing import init_otel, init_langfuse
    init_otel()
    init_langfuse()

    # Per graph run:
    from app.tracing import get_callback_handler
    lf_handler, lf_meta = get_callback_handler(
        trace_name="plan:abc123",
        user_id="user1",
        session_id="abc123",
        metadata={"tenant_id": "t1"},
        tags=["planning"],
    )
    config = {"configurable": {...}, "run_name": "plan:abc123", "metadata": lf_meta}
    if lf_handler:
        config["callbacks"] = [lf_handler]

    # Cross-service propagation:
    from app.tracing import inject_trace_carrier, extract_trace_context, get_tracer
    carrier = inject_trace_carrier()          # API side — store in MongoDB
    ctx = extract_trace_context(carrier)      # Worker side — reconstruct
"""

from __future__ import annotations

import functools
import logging
import os
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Langfuse singleton
# ---------------------------------------------------------------------------

_langfuse: Any = None          # Langfuse client singleton
_tracer_provider: Any = None   # OTEL TracerProvider singleton


def init_langfuse() -> None:
    """Call once at startup (from lifespan). Validates credentials.

    Sets the three standard Langfuse env vars before initialising the client
    so that the Langfuse SDK picks them up correctly regardless of import order.

    No-op when ``langfuse_enabled=False`` or keys are empty.
    """
    global _langfuse

    if not settings.langfuse_enabled:
        logger.info("tracing: Langfuse disabled (LANGFUSE_ENABLED=false)")
        return

    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        logger.warning(
            "tracing: LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY is empty — "
            "tracing will be skipped. Set both keys to enable Langfuse."
        )
        return

    # Expose env vars for Langfuse SDK auto-configuration
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
    os.environ.setdefault("LANGFUSE_BASE_URL", settings.langfuse_base_url)

    try:
        from langfuse import get_client  # type: ignore[import]

        _langfuse = get_client()
        _langfuse.auth_check()
        logger.info("tracing: Langfuse connected: %s", settings.langfuse_base_url)
    except Exception as exc:
        logger.warning(
            "tracing: Langfuse init/auth_check failed — tracing disabled. Reason: %s", exc
        )
        _langfuse = None


def get_langfuse() -> Any:
    """Return the Langfuse client singleton, or None if unavailable."""
    return _langfuse


def get_callback_handler(
    *,
    trace_name: str,
    user_id: str | None = None,
    session_id: str | None = None,
    metadata: dict | None = None,
    tags: list[str] | None = None,
) -> tuple[Any, dict]:
    """Return ``(CallbackHandler | None, langfuse_metadata_dict)`` for a single graph run.

    In Langfuse v3, ``CallbackHandler()`` takes no constructor args.  Trace
    attributes (user_id, session_id, tags) are passed via the LangChain
    ``config["metadata"]`` dict using the ``langfuse_*`` key convention.

    Usage::
        lf_handler, lf_meta = get_callback_handler(trace_name="plan:abc", ...)
        config = {
            "configurable": {...},
            "run_name": "plan:abc",   # becomes the Langfuse trace name
            "metadata": lf_meta,
        }
        if lf_handler:
            config["callbacks"] = [lf_handler]

    Args:
        trace_name: Human-readable trace name shown in the Langfuse UI.
                    Convention: ``"plan:{job_id}"`` or ``"execution:{job_id}"``.
        user_id:    Langfuse user identifier (for per-user analytics).
        session_id: Langfuse session identifier — use ``job_id`` so all spans
                    for a job group under a single session.
        metadata:   Free-form key/value pairs merged into the returned dict.
        tags:       List of tag strings (e.g. ``["planning", "tenant_abc"]``).

    Returns:
        Tuple of (``CallbackHandler`` or ``None``, metadata dict to pass into
        ``config["metadata"]``).
    """
    lf_meta: dict = {}
    if user_id:
        lf_meta["langfuse_user_id"] = user_id
    if session_id:
        lf_meta["langfuse_session_id"] = session_id
    if tags:
        lf_meta["langfuse_tags"] = tags
    if metadata:
        lf_meta.update(metadata)

    if _langfuse is None:
        return None, lf_meta

    try:
        from langfuse.langchain import CallbackHandler  # type: ignore[import]

        return CallbackHandler(), lf_meta
    except Exception as exc:
        logger.warning("tracing: could not create CallbackHandler — %s", exc)
        return None, lf_meta


# ---------------------------------------------------------------------------
# OpenTelemetry
# ---------------------------------------------------------------------------

def init_otel() -> None:
    """Initialize OTEL SDK: tracer provider + log bridge + auto-instrumentors.

    Call once at startup (lifespan) BEFORE init_langfuse().
    No-op when ``otel_enabled=False`` or ``otel_collector_endpoint`` is empty.
    """
    if not settings.otel_enabled or not settings.otel_collector_endpoint:
        logger.warning(
            "tracing: OTEL disabled or endpoint not set — skipping "
            "(OTEL_ENABLED=%s, OTEL_COLLECTOR_ENDPOINT=%r)",
            settings.otel_enabled,
            settings.otel_collector_endpoint,
        )
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.logging import LoggingInstrumentor
        from opentelemetry.instrumentation.pymongo import PymongoInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

        resource = Resource({
            SERVICE_NAME: settings.otel_service_name,
            SERVICE_VERSION: settings.otel_service_version,
        })

        exporter = OTLPSpanExporter(
            endpoint=settings.otel_collector_endpoint,
            insecure=settings.otel_insecure,
        )
        provider = TracerProvider(
            resource=resource,
            sampler=TraceIdRatioBased(settings.otel_sample_rate),
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        global _tracer_provider
        _tracer_provider = provider

        # Log bridge — injects otelTraceID + otelSpanID into every Python log record
        LoggingInstrumentor().instrument(set_logging_format=True)

        # Auto-instrumentors — cover outbound HTTP, MongoDB, and Redis without per-call changes
        HTTPXClientInstrumentor().instrument()   # Tavily, Serper, source_verifier, content_lake
        PymongoInstrumentor().instrument()       # all motor/pymongo calls
        RedisInstrumentor().instrument()         # all redis-py calls

        logger.info(
            "tracing: OTEL initialised (service=%s endpoint=%s sample_rate=%.2f)",
            settings.otel_service_name,
            settings.otel_collector_endpoint,
            settings.otel_sample_rate,
        )
    except Exception as exc:
        logger.warning("tracing: OTEL init failed — distributed tracing disabled. Reason: %s", exc)


def shutdown_otel() -> None:
    """Flush and shut down the OTEL TracerProvider. Call before process exit.

    Forces the BatchSpanProcessor to export any queued spans (up to 5 s),
    then shuts down the provider cleanly.  No-op when OTEL was not initialised.
    """
    if _tracer_provider is None:
        return
    try:
        _tracer_provider.force_flush(timeout_millis=5_000)
        _tracer_provider.shutdown()
        logger.info("tracing: OTEL TracerProvider shut down cleanly")
    except Exception as exc:
        logger.warning("tracing: OTEL shutdown error — %s", exc)


def get_tracer(name: str = "kadal") -> Any:
    """Return a tracer. Works even when OTEL is disabled (returns a no-op tracer)."""
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
    except Exception:
        return _NoOpTracer()


def inject_trace_carrier() -> dict:
    """Serialize current span context to a W3C dict for cross-service propagation.

    Returns:
        dict like ``{"traceparent": "00-<trace_id>-<span_id>-01"}``.
        Empty dict when OTEL is disabled or no active span.
    """
    try:
        from opentelemetry.propagate import inject as otel_inject
        carrier: dict = {}
        otel_inject(carrier)
        return carrier
    except Exception:
        return {}


def extract_trace_context(carrier: dict) -> Any:
    """Reconstruct trace context from a W3C carrier dict.

    Used in the Worker to become a child of the API span.

    Args:
        carrier: Dict from MongoDB metadata (e.g. ``{"traceparent": "00-…"}``).

    Returns:
        OTEL ``Context`` object.  Pass as ``context=`` to ``start_as_current_span``.
        Returns an empty context (no-op) when OTEL is disabled.
    """
    try:
        from opentelemetry.propagate import extract as otel_extract
        return otel_extract(carrier)
    except Exception:
        return None


def node_span(name: str, **extra_attrs: Any):
    """Decorator for LangGraph async node functions — adds an OTEL child span.

    Only apply to high-value / slow nodes (supervisor, knowledge_fusion,
    report_writer, report_reviewer, exporter).  Fast mechanical nodes are
    already covered by the httpx/pymongo/redis auto-instrumentors.

    Usage::
        @node_span("supervisor")
        async def supervisor(state: ExecutionState, config=None) -> dict:
            ...
    """
    def decorator(fn):  # type: ignore[return]
        @functools.wraps(fn)
        async def wrapper(state, *args, **kwargs):  # type: ignore[return]
            tracer = get_tracer("kadal.graph")
            attrs: dict = {"node": name, **extra_attrs}
            if "section_id" in state:
                attrs["section_id"] = state["section_id"]
            if "tenant_id" in state:
                attrs["tenant_id"] = state["tenant_id"]
            if "job_id" in state:
                attrs["job_id"] = state["job_id"]
            with tracer.start_as_current_span(f"node.{name}", attributes=attrs):
                return await fn(state, *args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Internal no-op tracer (used when opentelemetry-sdk is not installed)
# ---------------------------------------------------------------------------

class _NoOpSpan:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get_span_context(self):
        return _NoOpSpanContext()

    def set_attribute(self, *args):
        pass


class _NoOpSpanContext:
    trace_id = 0
    is_valid = False


class _NoOpTracer:
    def start_as_current_span(self, name: str, **kwargs):
        return _NoOpSpan()
