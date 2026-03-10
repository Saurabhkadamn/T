# Observability Guide — Kadal AI Deep Research

Two observability systems run in parallel:

| System | What It Traces | Where to See It |
|--------|---------------|-----------------|
| **Langfuse** | LLM calls — prompts, completions, token counts, cost | Langfuse UI (self-hosted or Cloud) |
| **OpenTelemetry** | Infrastructure — HTTP requests, MongoDB, Redis, node execution, cross-service spans | Jaeger / Grafana Tempo / Datadog |

They are complementary. Langfuse gives LLM-level detail; OTEL gives infrastructure-level detail. Both are linked by the same `trace_id`.

---

## 1. Environment Variables

Add these to your `.env`:

```env
# ── Langfuse ──────────────────────────────────────────────────────────────
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=http://localhost:3000      # your self-hosted Langfuse URL
LANGFUSE_ENABLED=true                        # set false to disable

# ── OpenTelemetry ─────────────────────────────────────────────────────────
OTEL_ENABLED=true
OTEL_COLLECTOR_ENDPOINT=localhost:4317       # OTLP gRPC endpoint (no http://)
OTEL_SERVICE_NAME=kadal-deepresearch-api     # override per service
OTEL_SERVICE_VERSION=1.0.0
OTEL_SAMPLE_RATE=1.0                         # 1.0 = 100%  lower in prod (e.g. 0.1)
```

**Worker** must set a different service name so spans are grouped separately in the UI:

```env
# In EKS pod spec / docker-compose worker override:
OTEL_SERVICE_NAME=kadal-deepresearch-worker
```

To **disable** either system without restarting:

```env
LANGFUSE_ENABLED=false
OTEL_ENABLED=false
```

---

## 2. Quick Start — Local Testing

### Step 1: Start Jaeger (all-in-one, no config needed)

```bash
docker run --rm -p 4317:4317 -p 16686:16686 jaegertracing/all-in-one:latest
```

- Receives OTEL spans on port `4317` (gRPC)
- UI available at `http://localhost:16686`

### Step 2: Start Langfuse

**Option A — Langfuse Cloud (easiest):**
Sign up at https://cloud.langfuse.com, get your keys, set:
```env
LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

**Option B — Self-hosted via their official docker-compose:**
```bash
# From https://langfuse.com/docs/deployment/self-host
git clone https://github.com/langfuse/langfuse.git
cd langfuse
docker-compose up -d
# UI at http://localhost:3000
```

### Step 3: Configure `.env`

```env
OTEL_COLLECTOR_ENDPOINT=localhost:4317
OTEL_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=http://localhost:3000
```

### Step 4: Start Redis + API

```bash
docker-compose up
```

This starts:
- `kadal_redis` — Redis 7 on port 6379
- `kadal_api` — FastAPI on port 8000 (reads `.env` for MongoDB Atlas URI, OTEL, Langfuse)

**Startup sequence** (`app/main.py` lifespan):
1. `db.indexes.setup_all_indexes()` — creates MongoDB indexes idempotently
2. Redis connection pool → `app.state.redis`
3. MongoDB client → `app.state.mongo`
4. `init_otel()` — starts OTEL tracer provider + auto-instrumentors
5. `init_langfuse()` — validates Langfuse credentials

You should see in the logs:
```json
{"msg": "startup: MongoDB indexes OK", ...}
{"msg": "tracing: OTEL initialised (service=kadal-deepresearch-api endpoint=localhost:4317 sample_rate=1.00)", ...}
{"msg": "tracing: Langfuse connected: http://localhost:3000", ...}
```

### Step 5: Trigger a request and inspect

```bash
curl -X POST http://localhost:8000/api/deepresearch/plan \
  -H "Authorization: Bearer <keycloak_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "Impact of LLMs on software engineering",
    "tools": ["web", "arxiv"],
    "chat_history": [],
    "objects": []
  }'
```

Open Jaeger at `http://localhost:16686` → select service `kadal-deepresearch-api` → find trace.

### Step 6: Run the worker and see the cross-service trace

**Via docker-compose (profile):**
```bash
EKS_JOB_ID=<job_id> docker-compose --profile worker up worker
```

**Or directly:**
```bash
EKS_JOB_ID=<job_id> OTEL_SERVICE_NAME=kadal-deepresearch-worker python -m worker.main
```

Back in Jaeger, the **same trace** now shows spans from both API and Worker under a single trace ID.

---

## 3. What Gets Traced Automatically

Once OTEL is initialised, these are traced **without any code changes**:

| Source | What is traced | Instrumented by |
|--------|---------------|-----------------|
| FastAPI | Every HTTP request: method, path, status code, duration | `FastAPIInstrumentor` in `main.py` |
| httpx | All outbound HTTP calls (Tavily, Serper, source_verifier, content_lake) | `HTTPXClientInstrumentor` |
| motor / pymongo | All MongoDB operations: collection, command, duration | `PymongoInstrumentor` |
| redis-py | All Redis commands: key pattern, command, duration | `RedisInstrumentor` |

> **Note:** The `/health` endpoint is excluded from FastAPI instrumentation (`excluded_urls="/health"`) to avoid noise in traces.

---

## 4. What Gets Traced Manually (LangGraph Nodes)

These execution graph nodes have explicit OTEL child spans via the `@node_span` decorator (`app/tracing.py`):

| Node | Span name | Extra attributes |
|------|-----------|-----------------|
| `supervisor` | `node.supervisor` | `job_id`, `tenant_id` |
| `knowledge_fusion` | `node.knowledge_fusion` | `job_id`, `tenant_id` |
| `report_writer` | `node.report_writer` | `job_id`, `tenant_id` |
| `report_reviewer` | `node.report_reviewer` | `job_id`, `tenant_id` |
| `exporter` | `node.exporter` | `job_id`, `tenant_id` |

Fast mechanical nodes (`search_query_gen`, `searcher`, `compressor`, `scorer`) are covered by the auto-instrumentors — `httpx` spans capture search tool calls, `redis` spans capture file content lookups.

Planning graph nodes (`context_builder`, `query_analyzer`, `clarifier`, `plan_creator`, `plan_reviewer`) are covered by the parent FastAPI request span via `FastAPIInstrumentor`. Their LLM calls are tracked in Langfuse.

---

## 5. Full Trace Flow (End-to-End)

```
Browser / Client
  │  POST /api/deepresearch/plan
  │  (traceparent injected by load balancer or client)
  ▼
FastAPI [kadal-deepresearch-api]
  └─ span: plan_request  (trace_id=ABC, span_id=001)
        ├─ pymongo: Deep_Research_Jobs.replace_one  (upsert planning state)
        └─ otel_carrier {"traceparent":"00-ABC-001-01"} saved to MongoDB

  │  POST /api/deepresearch/run  (user approves plan)
  ▼
FastAPI [kadal-deepresearch-api]
  └─ span: run_request  (trace_id=ABC, span_id=002)
        ├─ pymongo: Deep_Research_Jobs.update_one  (status → research_queued)
        ├─ pymongo: Deep_Research_Reports.insert_one
        ├─ pymongo: Background_Jobs.insert_one
        ├─ redis: HSET job:{job_id}:status
        └─ otel_carrier updated in MongoDB metadata

EKS Worker [kadal-deepresearch-worker]
  └─ span: execution  (trace_id=ABC, span_id=003)  ← SAME TRACE
        │   carrier read from MongoDB → context restored via extract_trace_context()
        │
        ├─ pymongo: Deep_Research_Jobs.update_one  (status → research)
        │
        ├─ span: node.supervisor
        │     └─ httpx: Tavily / Serper / Arxiv searches (per section, in parallel)
        │
        ├─ span: node.knowledge_fusion
        │     └─ (LLM call tracked in Langfuse as execution:{job_id})
        │
        ├─ span: node.report_writer
        │     └─ (LLM call tracked in Langfuse)
        │
        ├─ span: node.report_reviewer
        │     └─ (LLM call tracked in Langfuse)
        │
        └─ span: node.exporter
              ├─ pymongo: Deep_Research_Reports.update_one  (upsert final report)
              ├─ pymongo: Deep_Research_Jobs.update_one  (status → completed)
              └─ redis: PUBLISH job:{job_id}:events

Every log line from API + Worker:
  {"trace_id": "ABC...", "span_id": "...", "msg": "..."}
```

---

## 6. Log Correlation

All log output is JSON-formatted with `trace_id` and `span_id` injected automatically by the `LoggingInstrumentor`:

```json
{
  "ts": "2026-03-08 12:34:56,789",
  "level": "INFO",
  "logger": "graphs.execution.nodes.report_writer",
  "msg": "report_writer: writing initial report (job_id=abc-123)",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "span_id": "00f067aa0ba902b7",
  "service": "kadal-deepresearch-worker"
}
```

**To find all logs for a request:** filter by `trace_id` in your log aggregator.

```
# Grafana Loki:
{service="kadal-deepresearch-worker"} | json | trace_id="4bf92f3577b34da6a3ce929d0e0e4736"

# Datadog:
service:kadal-deepresearch-worker @trace_id:4bf92f3577b34da6a3ce929d0e0e4736

# CloudWatch Logs Insights:
filter trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
```

---

## 7. Langfuse ↔ OTEL Cross-Link

Every Langfuse trace has `otel_trace_id` in its metadata:

- **Plan graph traces:** `trace_name="plan:{job_id}"` → `metadata.otel_trace_id = <traceparent value>`
- **Execution graph traces:** `trace_name="execution:{job_id}"` → `metadata.otel_trace_id = <32-hex trace_id>`

In the Langfuse UI: open any trace → **Metadata** tab → copy `otel_trace_id` → paste into Jaeger search to jump to the infrastructure view.

The `otel_trace_id` is also stored in `Deep_Research_Jobs.metadata.otel_trace_id` and `Deep_Research_Reports.otel_trace_id` in MongoDB, so you can look up traces directly from a job document.

---

## 8. Production Configuration

### Sampling

For high-traffic production, lower the sample rate to reduce costs:

```env
OTEL_SAMPLE_RATE=0.1    # trace 10% of requests
```

Langfuse traces every LLM call regardless of OTEL sampling — they are independent.

### Pointing to a real collector

**Grafana Tempo (via OpenTelemetry Collector):**
```env
OTEL_COLLECTOR_ENDPOINT=otel-collector.monitoring.svc.cluster.local:4317
```

**Datadog Agent:**
```env
OTEL_COLLECTOR_ENDPOINT=datadog-agent.monitoring.svc.cluster.local:4317
```

**AWS X-Ray (via ADOT Collector):**
```env
OTEL_COLLECTOR_ENDPOINT=aws-otel-collector.monitoring.svc.cluster.local:4317
```

### Disabling per environment

```env
# Unit tests / CI — no external dependencies
OTEL_ENABLED=false
LANGFUSE_ENABLED=false
```

---

## 9. Adding a Span to a New Node

Use the `@node_span` decorator from `app.tracing`. Only apply to high-value / slow nodes — fast nodes are already covered by auto-instrumentors.

```python
from app.tracing import node_span

@node_span("my_new_node")
async def my_new_node(
    state: ExecutionState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    # your node logic here
    ...
```

The decorator automatically:
- Creates a child span named `node.my_new_node`
- Attaches `job_id`, `tenant_id`, `section_id` (when present in state) as span attributes
- Passes `config` and any other args through unchanged
- Is a no-op when OTEL is disabled (`_NoOpTracer` fallback)

---

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| No traces in Jaeger | `OTEL_ENABLED=false` or wrong endpoint | Check `.env` and that Jaeger is running on port 4317 |
| Traces appear but Worker spans are a separate trace | `otel_carrier` not saved to MongoDB | Check `run.py` → `metadata.otel_carrier` is being set |
| `"trace_id": ""` in every log line | `init_otel()` not called or OTEL disabled | Check startup logs for `"OTEL initialised"` message |
| Langfuse not recording | Wrong keys or `LANGFUSE_ENABLED=false` | Check `"Langfuse connected"` in startup logs |
| Langfuse `otel_trace_id` is empty string | No active OTEL span when `plan.py` runs | Ensure `OTEL_ENABLED=true` and a valid endpoint |
| OTEL init fails with import error | Missing packages | Run `pip install -r requirements.txt` (7 OTEL packages required) |
| Worker spans missing from trace | `EKS_JOB_ID` not set | Set `EKS_JOB_ID=<job_id>` before starting the worker |
