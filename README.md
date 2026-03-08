# Kadal AI — Deep Research Microservice

A FastAPI + LangGraph backend that accepts a research topic, runs multi-source AI research across web, academic, and internal sources, and returns a fully-formed streamed HTML report.

Two LangGraph graphs power the system: a **Planning Graph** (synchronous, runs inside FastAPI) and an **Execution Graph** (asynchronous, runs inside an EKS Worker). They are separated by a human-in-the-loop plan-approval boundary.

---

## Prerequisites

- Docker + Docker Compose
- A `.env` file with valid credentials (copy from `.env.example`)
- A Keycloak instance for Bearer token auth
- MongoDB Atlas cluster (connection string goes in `.env`)

---

## Quick Start

```bash
# 1. Set up environment
cp .env.example .env
# Fill in: MONGO_URI, GEMINI_API_KEY, KEYCLOACK_INTROSPECT_URL, etc.

# 2. Start API + Redis
docker-compose up --build

# 3. Open Swagger UI
open http://localhost:8000/docs
```

The API container starts on port `8000`. Redis starts on `6379`. MongoDB is remote (Atlas) — no local container needed.

---

## API Endpoints

### POST /api/deepresearch/plan
Runs the planning graph synchronously. Returns a structured research plan.

```bash
curl -X POST http://localhost:8000/api/deepresearch/plan \
  -H "Authorization: Bearer <keycloak-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "Impact of AI on healthcare in India",
    "chat_history": [],
    "objects": [],
    "tools": ["web", "arxiv"]
  }'
```

Response includes `job_id`, `status`, `plan` (sections + checklist), and `analysis` (depth, audience, domain).

### POST /api/deepresearch/run
Queues the approved plan for async execution by the worker.

```bash
curl -X POST http://localhost:8000/api/deepresearch/run \
  -H "Authorization: Bearer <keycloak-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "job_id": "<job_id from /plan response>"
  }'
```

### GET /api/deepresearch/status/{job_id}
Polls job status. Fast path: Redis. Fallback: MongoDB.

```bash
curl http://localhost:8000/api/deepresearch/status/<job_id> \
  -H "Authorization: Bearer <keycloak-token>"
```

Status lifecycle: `plans_ready` → `research_queued` → `research` → `completed` | `partial_success` | `failed`

### GET /api/deepresearch/reports
List all completed reports for the authenticated user.

```bash
curl http://localhost:8000/api/deepresearch/reports \
  -H "Authorization: Bearer <keycloak-token>"
```

### GET /api/deepresearch/reports/{report_id}
Get a single report with S3 presigned URL (1-hour expiry).

```bash
curl http://localhost:8000/api/deepresearch/reports/<report_id> \
  -H "Authorization: Bearer <keycloak-token>"
```

### GET /health
Health check — returns `{"status": "ok"}`.

---

## WebSocket (Live Streaming)

Connect to receive real-time section updates while the worker executes:

```
ws://localhost:8000/ws/deep-research/<job_id>?tenant_id=<tenant_id>
```

Event types: `section_start`, `content_delta`, `progress`, `completed`, `error`

Example (JavaScript):
```js
const ws = new WebSocket(`ws://localhost:8000/ws/deep-research/${jobId}?tenant_id=${tenantId}`);
ws.onmessage = (e) => {
  const event = JSON.parse(e.data);
  if (event.type === "content_delta") {
    document.getElementById("report").innerHTML += event.data.chunk;
  }
};
```

---

## Architecture Overview

```
POST /plan  ──►  Planning Graph (sync, FastAPI process)
                 context_builder → query_analyzer → clarifier?
                 → plan_creator → plan_reviewer
                 Returns: ResearchPlan

POST /run   ──►  Background_Jobs (QUEUED) ──► EKS Worker picks up
                 Execution Graph (async, worker process)
                 supervisor → [section subgraphs in parallel via Send()]
                   └─ search_query_gen → searcher → source_verifier
                      → compressor → scorer
                 → knowledge_fusion → report_writer → report_reviewer
                 → exporter (S3 + MongoDB + Redis pub/sub)

WS          ──►  Redis pub/sub → WebSocket → Browser
```

**Multi-model assignment:**
- Gemini 2.5 Pro — reasoning (query analysis, plan creation, knowledge fusion, report writing)
- Gemini 2.5 Flash — mechanical (search query gen, compression, scoring)

**Knowledge fusion trust order:** uploaded files > content lake > arxiv > web

---

## Commands Reference

```bash
# Start services
docker-compose up --build          # first run
docker-compose up                  # subsequent runs
docker-compose down                # stop

# Run API locally (without Docker)
uvicorn app.main:app --reload

# Run worker locally (without Docker)
EKS_JOB_ID=<job_id> python -m worker.main

# Type check
python -m mypy app/ worker/ graphs/

# Lint
python -m ruff check .

# Format
python -m black .

# MongoDB indexes (idempotent, safe to re-run)
python -m db.indexes
```

---

## Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Description |
|---|---|
| `MONGO_URI` | MongoDB Atlas SRV connection string |
| `REDIS_HOST` | Redis host (default: `localhost`) |
| `GEMINI_API_KEY` | Google AI Studio key |
| `KEYCLOACK_INTROSPECT_URL` | Keycloak token introspection endpoint |
| `S3_BUCKET` | S3 bucket for HTML report storage |
| `LLM_PROVIDER` | `google` / `openai` / `anthropic` (default: `google`) |
| `TAVILY_API_KEY` | Tavily search API key |
| `SERPER_API_KEY` | Serper (Google) search API key |

---

## Swagger UI

Full interactive API docs available at: **http://localhost:8000/docs**
