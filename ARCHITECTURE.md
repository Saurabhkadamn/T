# ARCHITECTURE.md — Kadal AI Deep Research Microservice

> Technical reference for the async multi-source research pipeline.
> Generated from source code: `app/`, `graphs/`, `tools/`, `worker/`, `db/`.

---

## 1. Overview

Kadal AI Deep Research is a FastAPI + LangGraph microservice that accepts a research topic from a
caller, produces a structured multi-section report sourced from the web, academic papers, tenant
content, and user-uploaded files, and streams progress back over WebSocket.

**Two-phase architecture:**

| Phase | Where it runs | Trigger | LangGraph graph |
|-------|--------------|---------|----------------|
| Planning | FastAPI process (in-process, sync invoke) | `POST /api/deepresearch/plan` | `graphs/planning/graph.py` |
| Execution | EKS Kubernetes Job (async stream) | `POST /api/deepresearch/run` → Redis Stream → Dispatcher → K8s Job | `graphs/execution/graph.py` |

**Key design decisions:**

- **Multi-tenant isolation** — `tenant_id` is mandatory on every MongoDB query; no RLS, enforced at application level.
- **Depth-dependent behaviour** — compression targets, source counts, search iterations, and revision caps all scale with `depth_of_research` (surface / intermediate / in-depth).
- **LangGraph checkpointing** — `AsyncMongoDBSaver` (collection `Langgraph_Checkpoints`) checkpoints every node completion so a restarted EKS pod resumes rather than re-runs completed sections.
- **Provider-agnostic LLM layer** — a single `LLM_PROVIDER` env var switches all nodes between Google Gemini, OpenAI, and Anthropic without touching node code.

---

## 2. Repository Layout

```
.
├── app/
│   ├── __init__.py               # empty package init
│   ├── config.py                 # pydantic-settings: single source of truth for all settings
│   ├── dependencies.py           # FastAPI dependency injection: Keycloak auth, Redis, Mongo
│   ├── enums.py                  # PlanningJobStatus + ExecutionJobStatus enums
│   ├── llm_factory.py            # get_llm(task, temp) → LangChain model (Google/OpenAI/Anthropic)
│   ├── logging_utils.py          # JSON log formatter with trace_id/span_id injection
│   ├── main.py                   # FastAPI app: lifespan, CORS, routers, /health
│   ├── tracing.py                # OpenTelemetry + Langfuse setup; @node_span decorator
│   └── api/
│       ├── models.py             # Pydantic request/response models for all endpoints
│       ├── websocket.py          # WS /ws/deep-research/{job_id}: Redis pub/sub relay
│       └── routes/
│           ├── plan.py           # POST /api/deepresearch/plan (planning graph)
│           ├── run.py            # POST /api/deepresearch/run (approve/cancel)
│           ├── status.py         # GET /api/deepresearch/status/{job_id}
│           └── reports.py        # GET /api/deepresearch/reports[/{report_id}]
│
├── graphs/
│   ├── planning/
│   │   ├── state.py              # PlanningState TypedDict + all planning sub-types
│   │   ├── graph.py              # Compiled planning StateGraph
│   │   └── nodes/
│   │       ├── context_builder.py   # Redis file load + Flash LLM → context_brief
│   │       ├── query_analyzer.py    # Pro LLM → depth, audience, source_scope, clarification
│   │       └── plan_creator.py      # Pro LLM → ResearchPlan with sections + checklist
│   │
│   └── execution/
│       ├── state.py              # ExecutionState + all execution sub-types
│       ├── graph.py              # Compiled async execution StateGraph with checkpointing
│       ├── section_subgraph.py   # Per-section research pipeline (search → compress → score)
│       ├── _url_utils.py         # Shared SHA-256 URL hashing utility
│       └── nodes/
│           ├── supervisor.py        # Fan-out via Send(); Phase 2 reflection (Pro LLM)
│           ├── search_query_gen.py  # Flash LLM → search queries per iteration
│           ├── searcher.py          # Parallel tool execution; dedup; cap
│           ├── source_verifier.py   # httpx HEAD check; SHA-256 url_hash; domain extraction
│           ├── compressor.py        # Flash LLM; depth-dependent token budget
│           ├── scorer.py            # Flash LLM; composite score; packages SectionResult
│           ├── knowledge_fusion.py  # Pro LLM; trust-tier conflict resolution
│           ├── report_writer.py     # Pro LLM; full HTML report + revision
│           ├── report_reviewer.py   # Pro LLM; approve / needs_revision routing
│           └── exporter.py          # S3 upload; MongoDB upserts; Redis status + pub/sub
│
├── tools/
│   ├── tavily_search.py          # POST api.tavily.com — full page text (primary web)
│   ├── serper_search.py          # POST google.serper.dev — snippets (web fallback)
│   ├── arxiv_search.py           # arxiv SDK + PDF; run_in_executor (sync SDK)
│   ├── content_lake.py           # Internal tenant content API (semantic search)
│   └── file_content.py           # Redis → LOR API → S3 fallback chain for uploaded files
│
├── worker/
│   ├── main.py                   # EKS Job entry: reads EKS_JOB_ID, calls executor.run()
│   ├── executor.py               # Loads job from MongoDB; runs execution graph; XACK
│   ├── streaming.py              # JobStreamer: publishes typed events to Redis pub/sub
│   ├── dispatcher.py             # Dispatcher class: Redis Streams → K8s Job spawner
│   └── dispatcher_main.py        # Entry point for Dispatcher K8s Deployment
│
├── db/
│   ├── __init__.py
│   └── indexes.py                # setup_all_indexes(): idempotent MongoDB index setup
│
├── app/api/models.py             # (listed above)
├── requirements.txt              # pinned Python dependencies
├── docker-compose.yml            # local dev: redis + api + worker + dispatcher services
└── .env.example                  # all required env vars documented (no secrets)
```

---

## 3. Tech Stack

| Category | Technology | Version |
|----------|-----------|---------|
| Runtime | Python | 3.11+ |
| Web framework | FastAPI | 0.115.6 |
| ASGI server | uvicorn[standard] | 0.34.0 |
| Validation | pydantic | 2.10.6 |
| Settings | pydantic-settings | 2.8.1 |
| Graph framework | langgraph | 1.0.10 |
| Graph checkpointing | langgraph-checkpoint-mongodb | 0.3.1 |
| LangChain core | langchain / langchain-core | 0.3.21 / 0.3.47 |
| LLM — Google | langchain-google-genai | 1.0.10 |
| LLM — OpenAI | langchain-openai | 0.2.14 |
| LLM — Anthropic | langchain-anthropic | 0.2.4 |
| Database driver | motor (async) | 3.7.1 |
| Database driver | pymongo | 4.15.0 |
| Cache / streams | redis (async) | 5.2.1 |
| HTTP client | httpx | 0.28.1 |
| Search — arXiv | arxiv SDK | 2.2.0 |
| PDF extraction | pymupdf | 1.25.3 |
| Cloud storage | boto3 (S3) | 1.37.26 |
| Observability | langfuse | 3.14.5 |
| Tracing | opentelemetry-sdk | 1.40.0 |
| OTLP exporter | opentelemetry-exporter-otlp-proto-grpc | 1.40.0 |
| OTEL instrumentors | opentelemetry-instrumentation-{fastapi,httpx,pymongo,redis,logging} | 0.61b0 |
| K8s client | kubernetes | 29.0.0 |
| Env file loading | python-dotenv | 1.1.0 |

---

## 4. Configuration System (`app/config.py`)

All configuration is centralised in a single `Settings` class (pydantic-settings). No node file
may hardcode a model name, token limit, or env var — all come from here.

### 4.1 Depth Configurations

`settings.get_depth_config(depth)` returns a `DepthConfig` instance. Falls back to
`intermediate` for unrecognised values.

| Parameter | `surface` | `intermediate` | `in-depth` |
|-----------|-----------|---------------|-----------|
| `max_search_iterations` | 1 | 2 | 3 |
| `max_sources_per_section` | 3 | 5 | 10 |
| `compression_target_tokens` | 2,500 | 5,000 | 8,000 |
| `max_chars_per_source` | 25,000 | 35,000 | 50,000 |
| `report_revision_max` | 1 | 2 | 3 |
| `supervisor_reflection_enabled` | `False` | `False` | `True` |

**Rationale for `max_chars_per_source`:** sized so N sources × chars stays within Flash's 1M-token
context window (surface ≈ 19K tokens, intermediate ≈ 44K tokens, in-depth ≈ 125K tokens).

### 4.2 Loop Limits (`LoopLimits`)

| Field | Default | Enforced by |
|-------|---------|-------------|
| `clarification_max` | 3 | `graph.py` `_route_after_query_analyzer` |
| `plan_refinement_max` | 3 | `plan.py` before graph invocation |
| `plan_revision_max` | 3 | `graph.py` `_route_entry` |
| `plan_self_review_max` | 2 | (reserved — not yet wired) |
| `supervisor_reflection_max` | 2 | `supervisor.py` `reflection_count` guard |

### 4.3 Model Configuration (`ModelConfig`)

| Provider | Pro model | Flash model |
|---------|----------|------------|
| Google | `gemini-2.5-pro` | `gemini-2.5-flash` |
| OpenAI | `gpt-4o` | `gpt-4o-mini` |
| Anthropic | `claude-opus-4-6` | `claude-haiku-4-5-20251001` |

**Task-to-tier assignments (default):**

| Task key | Tier | Override field |
|----------|------|---------------|
| `query_analysis` | Pro | `use_pro_for_query_analysis` |
| `plan_creation` | Pro | `use_pro_for_plan_creation` |
| `plan_review` | Pro | `use_pro_for_plan_review` |
| `supervisor_reflection` | Pro | `use_pro_for_supervisor_reflection` |
| `knowledge_fusion` | Pro | `use_pro_for_knowledge_fusion` |
| `report_writing` | Pro | `use_pro_for_report_writing` |
| `report_review` | Pro | `use_pro_for_report_review` |
| `context_building` | Flash | `use_pro_for_context_building` |
| `query_gen` | Flash | `use_pro_for_query_gen` |
| `compression` | Flash | `use_pro_for_compression` |
| `scoring` | Flash | `use_pro_for_scoring` |

### 4.4 Dispatcher Configuration (`DispatcherConfig`)

| Field | Default | Notes |
|-------|---------|-------|
| `stream_name` | `dr:jobs:stream` | Redis Stream name |
| `consumer_group` | `dispatchers` | XREADGROUP group |
| `msgid_key_prefix` | `dr:dispatch:msgid` | Redis key for msg_id storage |
| `msgid_ttl_seconds` | 86,400 | 24 hr |
| `stream_maxlen` | 10,000 | Approximate trim |
| `block_ms` | 5,000 | XREADGROUP BLOCK timeout |
| `pel_check_interval_seconds` | 300 | 5 min PEL sweep |
| `stuck_job_timeout_seconds` | 3,600 | 1 hr threshold for "stuck RUNNING" |
| `max_concurrent_jobs` | 10 | Back-pressure limit |
| `dry_run` | `False` | Skip K8s spawn (set True for local dev) |
| `k8s_namespace` | `default` | Kubernetes namespace |
| `worker_cpu_request` / `limit` | `500m` / `2000m` | |
| `worker_memory_request` / `limit` | `1Gi` / `4Gi` | |
| `worker_job_ttl_seconds` | 300 | `ttlSecondsAfterFinished` |
| `worker_backoff_limit` | 0 | Dispatcher handles retries via PEL |

### 4.5 Redis TTLs (`RedisTTLConfig`)

| Field | Default |
|-------|---------|
| `job_status_ttl_seconds` | 86,400 (24 hr) |
| `source_score_ttl_seconds` | 604,800 (7 days) |
| `rate_limit_run_ttl_seconds` | 3,600 (1 hr) |

### 4.6 Root Settings Fields (selected)

| Env var | Field | Default | Notes |
|---------|-------|---------|-------|
| `MONGO_URI` | `mongo_uri` | required | Atlas SRV URI |
| `MONGO_DB_NAME` | `mongo_db_name` | `Dev_Kadal` | |
| `REDIS_HOST` | `redis_host` | required | |
| `REDIS_PASSWORD` | `redis_password` | `""` | |
| `GEMINI_API_KEY` | `gemini_api_key` | required (Google) | |
| `OPENAI_API_KEY` | `openai_api_key` | `""` | Required when `LLM_PROVIDER=openai` |
| `ANTHROPIC_API_KEY` | `anthropic_api_key` | `""` | Required when `LLM_PROVIDER=anthropic` |
| `LLM_PROVIDER` | `llm_provider` | `google` | `google` \| `openai` \| `anthropic` |
| `S3_BUCKET` | `s3_bucket` | required | |
| `S3_REGION` | `s3_region` | `ap-south-1` | |
| `TAVILY_API_KEY` | `tavily_api_key` | required | |
| `SERPER_API_KEY` | `serper_api_key` | required | |
| `KEYCLOACK_INTROSPECT_URL` | `keycloak_introspect_url` | `""` | Typo preserved to match callers |
| `LLM_TIMEOUT_SECONDS` | `llm_timeout_seconds` | 120 | All LLM `ainvoke` calls |
| `PLANNING_TIMEOUT_SECONDS` | `planning_timeout_seconds` | 180 | `asyncio.wait_for` on graph `ainvoke` |
| `CORS_ORIGINS` | `cors_origins` | `["*"]` | JSON list string |
| `OTEL_ENABLED` | `otel_enabled` | `True` | |
| `OTEL_SAMPLE_RATE` | `otel_sample_rate` | `1.0` | `TraceIdRatioBased` |
| `LANGFUSE_ENABLED` | `langfuse_enabled` | `True` | Set `False` in unit tests |
| `PARTIAL_FAILURE_THRESHOLD` | `partial_failure_threshold` | `0.5` | Fraction of failed sections before `FAILED` vs `PARTIAL_SUCCESS` |

**Nested env var override syntax** (double-underscore delimiter):

```bash
DEPTH_IN_DEPTH__COMPRESSION_TARGET_TOKENS=10000
LOOPS__PLAN_SELF_REVIEW_MAX=1
MODELS__PRO_MODEL=gemini-2.5-pro-latest
DISPATCHER__MAX_CONCURRENT_JOBS=20
DISPATCHER__DRY_RUN=true
```

---

## 5. Authentication & Multi-Tenancy (`app/dependencies.py`)

### 5.1 Keycloak Bearer Token Introspection

All routes (except `/health`) are protected via `get_current_user(token: str = Depends(oauth2_scheme))`.

Flow:
1. `Authorization: Bearer <token>` header extracted by `OAuth2PasswordBearer`.
2. `POST KEYCLOACK_INTROSPECT_URL` with `content-type: application/x-www-form-urlencoded`, body `token=<token>`.
3. `payload.active == False` → HTTP 401.
4. `tenant_id` = last path segment of `payload["iss"]` (e.g. `.../realms/acme-corp` → `"acme-corp"`). `rstrip("/")` applied before `split("/")[-1]` to handle trailing slashes.
5. **Human user** (has `username` + `client_id` + `iss`): `user_id = payload["user_id"]`, roles from `user_roles[0].role_name`.
6. **Service account** (has `client_id` + `iss`, no `username`): `user_id = payload["sub"]`, explicit `TenantId` claim takes precedence over `iss`-derived tenant.

### 5.2 `TokenUser` Dataclass

```python
@dataclass
class TokenUser:
    user_id: str
    tenant_id: str
    client_id: str
    email: str
    keycloak_user_id: str = ""
    username: str = ""
    roles: list = None
    user_type: str = "internal"
```

### 5.3 Tenant Propagation

```
HTTP Request
  → get_current_user() → TokenUser.tenant_id
      → route handler (injected as current_user.tenant_id)
          → every MongoDB query: filter includes {"tenant_id": tenant_id}
          → initial_state["tenant_id"] in planning graph
          → Deep_Research_Jobs.tenant_id in MongoDB
          → worker executor reads tenant_id from job doc
          → ExecutionState["tenant_id"] propagated to all section Send() payloads
          → every content_lake tool call uses tenant_id
```

---

## 6. FastAPI Application Layer

### 6.1 Application Lifecycle (`app/main.py`)

**Startup** (lifespan):
1. `setup_all_indexes()` — creates/confirms MongoDB indexes (idempotent).
2. `redis.asyncio.Redis` connection pool — `max_connections=100`, stored in `app.state.redis`.
3. `AsyncIOMotorClient` — `maxPoolSize=50`, stored in `app.state.mongo`.
4. `init_otel()` — initialises OpenTelemetry SDK + `FastAPIInstrumentor`.
5. Langfuse callback handler setup (if `langfuse_enabled`).

**Shutdown** (lifespan finally):
- `redis.aclose()`, `mongo.close()`, OTEL `TracerProvider.shutdown()`.

### 6.2 Endpoints

| Method | Path | File | Status | Description |
|--------|------|------|--------|-------------|
| POST | `/api/deepresearch/plan` | `routes/plan.py` | 200 | Run planning graph (3 modes) |
| POST | `/api/deepresearch/run` | `routes/run.py` | 202 | Approve or cancel plan; queues execution |
| GET | `/api/deepresearch/status/{job_id}` | `routes/status.py` | 200 | Current job status + progress |
| GET | `/api/deepresearch/reports` | `routes/reports.py` | 200 | Paginated completed report list |
| GET | `/api/deepresearch/reports/{report_id}` | `routes/reports.py` | 200 | Report detail + fresh S3 presigned URL |
| WS | `/ws/deep-research/{job_id}` | `api/websocket.py` | — | Live execution events via Redis pub/sub |
| GET | `/health` | `main.py` | 200 | Liveness probe (`{"status":"ok"}`) |

### 6.3 Rate Limiting

**Plan endpoint (daily):**
- Redis Hash key: `rate:plan:day:{user_id}:{YYYY-MM-DD}` (UTC date).
- Value: `{job_id: "planning" | "completed" | "failed"}`.
- Count = entries where value ≠ `"failed"` (failed jobs do not count against the quota).
- TTL: 90,000 seconds (25 hours — covers full UTC day + buffer for timezone safety).
- `rate_limit_plan_per_day: int | None` — `None` = unlimited.
- Pattern: read-only pre-check before graph invocation; consume (add entry) post-graph on success.
- Redis errors → fail open (log + allow).
- Worker updates entry from `"planning"` → `"completed"` or `"failed"` after job finishes.

**Run endpoint (hourly):**
- Redis String key: `rate:run:{tenant_id}:{user_id}`.
- Lua script: `INCR` then `EXPIRE` only when `current == 1` (prevents sliding window drift).
- TTL: `redis_ttls.rate_limit_run_ttl_seconds` (3,600 seconds = 1 hour).
- `rate_limit_run_per_hour: int | None` — `None` = unlimited.
- Redis errors → fail open.

---

## 7. Planning Flow

### 7.1 Three Invocation Modes

The planning graph is stateless between calls. The API layer (`plan.py`) loads prior state from
MongoDB (`planning_state` field) and passes it as `initial_state` to `ainvoke`.

| Mode | Trigger | job_id behaviour | Entry node |
|------|---------|-----------------|-----------|
| 1 Fresh | New topic, no `job_id` | Generated (`uuid4()`) | `context_builder` (no `context_brief`) |
| 2 Clarify | `job_id` + `clarification_answers` | Existing (loaded from DB) | `query_analyzer` (`context_brief` set, no `plan`) |
| 3 Revise | `job_id` + `plan_feedback` | Existing (loaded from DB) | `plan_creator` (`context_brief` + `plan` both set) |

### 7.2 Planning Graph Topology (`graphs/planning/graph.py`)

```
START
  │
  ▼  _route_entry()
  ├─► [no context_brief]            → context_builder
  ├─► [context_brief, no plan]      → query_analyzer
  └─► [context_brief + plan]        → plan_creator   (Mode 3)
       │                    (also: plan_revision_max exceeded → END)

context_builder
  │
  ▼
query_analyzer
  │
  ├─► [status=failed]                              → END
  ├─► [needs_clarification AND count < cap]        → END  (surface questions to caller)
  └─► [otherwise OR clarification cap reached]    → plan_creator

plan_creator
  │
  ▼
END  (status=awaiting_approval — human approves via POST /run)
```

Graph is compiled once at module import (`planning_graph = _build_planning_graph()`).

### 7.3 Planning Nodes

| Node | File | LLM tier | Key inputs | Key outputs |
|------|------|----------|-----------|------------|
| `context_builder` | `nodes/context_builder.py` | Flash | `uploaded_files` (Redis lookup), `chat_history` | `context_brief`, `file_contents` |
| `query_analyzer` | `nodes/query_analyzer.py` | Pro | `original_topic`, `context_brief`, `clarification_answers` | `depth_of_research`, `audience`, `source_scope`, `clarification_questions`, `needs_clarification` |
| `plan_creator` | `nodes/plan_creator.py` | Pro | `refined_topic`, `analysis`, `plan_feedback` (Mode 3) | `plan` (`ResearchPlan`), `checklist` |

### 7.4 Planning State (`graphs/planning/state.py`)

**Auxiliary types:**

```python
class ChatMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str

class UploadedFile(TypedDict):
    object_id: str      # used as Redis key: file:{object_id}:content
    filename: str
    mime_type: str

class PlanSection(TypedDict):
    section_id: str           # stable UUID; used as LangGraph thread_id suffix
    title: str
    description: str
    search_strategy: str      # e.g. "arxiv,web" — searcher weights by this

class ResearchPlan(TypedDict):
    title: str
    summary: str
    sections: list[PlanSection]
    estimated_sources: int
```

**`PlanningState` field groups:**

| Group | Fields |
|-------|--------|
| Identity | `topic_id`, `tenant_id`, `user_id`, `chat_bot_id` |
| Input context | `chat_history`, `uploaded_files`, `file_contents` |
| Context brief | `context_brief` |
| Topic | `original_topic`, `refined_topic` |
| Clarification | `needs_clarification`, `clarification_questions`, `clarification_answers`, `clarification_count` |
| Parameters | `depth_of_research`, `audience`, `objective`, `domain`, `recency_scope`, `source_scope`, `assumptions` |
| Plan | `plan`, `plan_revision_count`, `plan_feedback`, `checklist` |
| Lifecycle | `status`, `error` |

### 7.5 MongoDB Persistence After Planning

After every graph invocation, `plan.py` calls `_save_job_doc()` which does
`replace_one({"_id": job_id, "tenant_id": tenant_id}, doc, upsert=True)` on `Deep_Research_Jobs`.

Fields written:
```
job_id, tenant_id, user_id, chat_bot_id, topic, refined_topic, analysis, plan, checklist,
uploaded_files, tools_enabled, status, llm_model, planning_state (full PlanningState dict),
created_date (YYYY-MM-DD UTC), created_at, updated_at
```

Graph status string → `PlanningJobStatus` enum:
- `"awaiting_approval"` → `PLAN_READY`
- `"needs_clarification"` → `NEED_CLARIFICATION`
- `"failed"` → `PLAN_INITIATED` (or previous status preserved)

---

## 8. Execution Trigger Flow (`POST /run`)

`routes/run.py` step-by-step:

1. `get_current_user()` → Keycloak introspection → `TokenUser` (with `tenant_id`).
2. Hourly rate-limit check (Lua INCR+EXPIRE on `rate:run:{tenant_id}:{user_id}`).
3. `Deep_Research_Jobs.find_one({"_id": ObjectId(job_id), "tenant_id": tenant_id})` → 404 if missing.
4. **Cancel path** (`body.action == "cancel"`):
   - Atomic `update_one` filtered to `status $in [PLAN_READY, NEED_CLARIFICATION, PLAN_EDIT_REQUESTED]`.
   - Returns 409 if `modified_count == 0` (already transitioned).
5. **Approve path**: generate `report_id = uuid4()`; call `inject_trace_carrier()` to capture OTEL context.
6. Atomic `update_one` filtered to `status == PLAN_READY` → sets `QUEUED`, `report_id`, `metadata.otel_carrier`.
   - Returns 409 if `modified_count == 0` (status changed between steps 3 and 6).
7. `Deep_Research_Reports.insert_one(report_doc)` — pre-populates with plan metadata.
8. Best-effort rollback: if report insert fails, attempt to reset job status to `PLAN_READY`.
9. `redis.hset("job:{job_id}:status", {status, progress, tenant_id, updated_at})` + `expire(24 hr)`.
10. `redis.xadd("dr:jobs:stream", {job_id, tenant_id, queued_at}, maxlen=10000)` → dispatcher picks up.
11. `redis.set("dr:dispatch:msgid:{job_id}", msg_id)` — stored for worker XACK.
12. Return `RunResponse(job_id, report_id, status="QUEUED")`.

### Job Status Lifecycle

```
PLAN_INITIATED
     │
     ▼ (query_analyzer returns questions)
NEED_CLARIFICATION ◄──────────────────────────────────────────────┐
     │ (user sends clarification_answers)                          │
     ▼                                                             │
PLAN_READY ──────────────────────────────────────────────────────►PLAN_EDIT_REQUESTED
     │ (POST /run, action=approve)          (POST /run, feedback)  │
     ▼                                                             │
  QUEUED                                                           │
     │ (dispatcher spawns K8s Job)           PLAN_CANCELED ◄───────┘
     ▼                                       (POST /run, action=cancel)
  RUNNING
     │
     ├──► COMPLETED       (full success)
     ├──► PARTIAL_SUCCESS (≥1 section failed but report generated)
     └──► FAILED          (unrecoverable or > partial_failure_threshold)
```

---

## 9. Execution Graph (Worker)

### 9.1 Worker Entry

**`worker/main.py`:**
```python
job_id = os.environ["EKS_JOB_ID"]
asyncio.run(executor.run(job_id))
```

**`worker/executor.py` `run()` steps:**
1. Create Redis + MongoDB clients.
2. `Deep_Research_Jobs.find_one({"_id": ObjectId(job_id)})` — loads job doc.
3. Extract `tenant_id`, `user_id`, plan, depth, file metadata.
4. Load file contents from Redis (for all `uploaded_files`).
5. Build `ExecutionState` initial dict from job doc fields.
6. Atomic `update_one` → status `RUNNING`, `started_at`.
7. Persist `started_at` to Redis hash `job:{job_id}:status`.
8. Call `build_execution_graph()` → `(graph, checkpointer)`.
9. Extract OTEL carrier from `job_doc["metadata"]["otel_carrier"]` → `extract_trace_context()`.
10. `async for event in graph.astream(initial_state, config={"thread_id": job_id})` — stream events.
11. Publish each event via `JobStreamer` to Redis pub/sub.
12. On graph completion: call `_ack_stream_message()` (Redis XACK via stored `msgid` key).
13. Graceful shutdown: close checkpointer mongo client, Redis, app Mongo.

### 9.2 Execution State Schema (`graphs/execution/state.py`)

**`ExecutionState` field groups:**

| Group | Key fields | Notes |
|-------|-----------|-------|
| Identity | `job_id`, `report_id`, `tenant_id`, `user_id` | `job_id` = LangGraph `thread_id` |
| Topic | `topic`, `refined_topic` | |
| Plan | `plan` (ResearchPlan), `checklist`, `sections` | `sections` typed `list[dict]` — checkpointer restores raw dicts |
| Attachments | `attachments`, `file_contents`, `tools_enabled` | |
| Config | `max_search_iterations`, `max_sources_per_section`, `max_chars_per_source`, `report_revision_max` | Set from `DepthConfig` |
| Reducers | `section_results`, `compressed_findings`, `citations`, `source_scores` | All `Annotated[list[T], operator.add]` |
| Supervisor | `reflection_count`, `knowledge_gaps` | |
| Knowledge | `fused_knowledge` | `Optional[str]` |
| Report | `report_html`, `review_feedback`, `revision_count`, `s3_key` | `Optional` fields |
| Lifecycle | `progress` (0–100), `status`, `error` | |
| Observability | `otel_trace_id` | `""` if tracing disabled |

**Reducer fields** (`operator.add` = list concatenation, enables parallel section writes):
- `section_results: Annotated[list[SectionResult], operator.add]`
- `compressed_findings: Annotated[list[CompressedFinding], operator.add]`
- `citations: Annotated[list[Citation], operator.add]`
- `source_scores: Annotated[list[SourceScore], operator.add]`

### 9.3 Main Execution Graph Topology (`graphs/execution/graph.py`)

```
START
  └── supervisor (Phase 1 — no LLM, reads sections, fan-out via Send())
        ├─► section_subgraph × N  (parallel, one per plan section)
        │       └──► supervisor  (Phase 2 — Pro LLM reflection, identify gaps)
        │               ├─► section_subgraph × gaps  (gap fill-in iterations)
        │               └─► knowledge_fusion         (when no more gaps or reflection_max reached)
        └─► knowledge_fusion  (surface/intermediate — reflection disabled, go direct)
                └── report_writer  (Pro LLM — full HTML generation)
                      └── report_reviewer  (Pro LLM — approve or request revision)
                            ├─► report_writer  [needs_revision ∧ revision_count < revision_max]
                            └─► exporter       [approved]
                                  └── END
```

Checkpointing: `AsyncMongoDBSaver(db_name="Dev_Kadal", collection_name="Langgraph_Checkpoints")`,
`thread_id = job_id`. On EKS pod restart, the graph resumes from the last completed node —
completed sections are never re-researched.

### 9.4 Section Sub-graph Topology (`graphs/execution/section_subgraph.py`)

```
START → search_query_gen → searcher
                               │
               ┌───────────────┴──────────────────────┐
               │ if search_iteration < max_iterations  │  (loop back)
               │                                       │
               └─────────────────────── source_verifier
                                              │
                                          compressor
                                              │
                                           scorer
                                              │
                                            END
```

Output schema (`SectionSubgraphOutput`) — merged into `ExecutionState` via reducer fields:
```python
section_results: list[SectionResult]
compressed_findings: list[CompressedFinding]
citations: list[Citation]
source_scores: list[SourceScore]
```

Each section runs as an independent parallel unit spawned by `supervisor` via LangGraph `Send()`.

### 9.5 Execution Nodes — Full Detail

| Node | File | LLM | Model tier | Temp | Key inputs | Key outputs |
|------|------|-----|-----------|------|-----------|------------|
| `supervisor` | `nodes/supervisor.py` | Phase 2 only | Pro | 0.3 | `section_results`, `sections`, `reflection_count` | `knowledge_gaps`, `Send()` list (fan-out) |
| `search_query_gen` | `nodes/search_query_gen.py` | Yes | Flash | 0.4 | `section_description`, `search_iteration`, `max_sources` | `search_queries` (`ceil(max_sources/max_iters)` clamped [2,5]) |
| `searcher` | `nodes/searcher.py` | No | — | — | `search_queries`, `tools_enabled`, `file_contents` | `raw_search_results` (appended), `search_iteration` (+1) |
| `source_verifier` | `nodes/source_verifier.py` | No | — | — | `raw_search_results` (web only) | `verified_urls` (SHA-256 hash, domain, `is_authentic`) |
| `compressor` | `nodes/compressor.py` | Yes | Flash | 0.1 | raw results, `verified_urls`, `compression_target_tokens`, `max_chars_per_source` | `compressed_text`, `citations` |
| `scorer` | `nodes/scorer.py` | Yes | Flash | 0.0 | results, citations, `section_id` | `source_scores`, `SectionResult` (packaged output) |
| `knowledge_fusion` | `nodes/knowledge_fusion.py` | Yes | Pro | 0.3 | `compressed_findings`, trust tiers | `fused_knowledge` |
| `report_writer` | `nodes/report_writer.py` | Yes | Pro | 0.4 | `fused_knowledge`, `plan`, `citations`, `review_feedback` | `report_html` (full HTML on first call; revision on subsequent) |
| `report_reviewer` | `nodes/report_reviewer.py` | Yes | Pro | 0.2 | `report_html`, `checklist` | `review_feedback`, `should_revise` routing |
| `exporter` | `nodes/exporter.py` | No | — | — | `report_html`, `job_id`, `tenant_id`, `report_id` | `s3_key`, final status in MongoDB + Redis |

### 9.6 Depth-Dependent Behaviour

| Parameter | `surface` | `intermediate` | `in-depth` |
|-----------|-----------|---------------|-----------|
| `max_search_iterations` | 1 | 2 | 3 |
| `max_sources_per_section` | 3 | 5 | 10 |
| `compression_target_tokens` | 2,500 | 5,000 | 8,000 |
| `max_chars_per_source` | 25,000 | 35,000 | 50,000 |
| `report_revision_max` | 1 | 2 | 3 |
| `supervisor_reflection` | disabled | disabled | enabled |

### 9.7 Trust Tiers

Used by `compressor` (sort order for citation priority), `knowledge_fusion` (conflict resolution), and `scorer` (authenticity weight).

| Tier | Source type | Rationale |
|------|------------|-----------|
| 1 | `files` (uploaded) | User-provided; highest trust |
| 2 | `content_lake` | Internal tenant knowledge base |
| 3 | `arxiv` | Peer-reviewed academic literature |
| 4 | `web` (Tavily/Serper) | Open internet; lowest trust |

### 9.8 Composite Score Formula (`scorer.py`)

```
composite = 0.5 × relevance_score + 0.3 × recency_score + 0.2 × authenticity_score
```

All component scores clamped to `[0.0, 1.0]`. `scoring` task uses temperature 0.0 (deterministic).

### 9.9 Exporter Operations (`nodes/exporter.py`)

Module-level lazy-init singletons (`_s3_client`, `_mongo_client`) — created once on first call,
never closed inside the node body.

1. S3 `put_object(Bucket, Key="{tenant_id}/{report_id}.html", Body=html_bytes, ContentType="text/html")`.
2. Generate presigned URL (TTL = `s3_presigned_url_ttl_seconds` = 3,600 s).
3. MongoDB `update_one` on `Deep_Research_Reports` — writes `s3_key`, summary (HTML stripped),
   `citations` (trimmed to `{id, title, url, source_type}`), `citation_count`, `llm_model`, `executed_at`.
4. MongoDB bulk upsert on `Source_Scores` — `$inc times_used`, `$push scores_history` (capped at 20 entries).
5. MongoDB `update_one` on `Deep_Research_Jobs` — final status (`COMPLETED` / `PARTIAL_SUCCESS`), `completed_at`, `updated_at`.
6. `redis.hset("job:{job_id}:status", {status, progress=100})`.
7. Redis pub/sub `publish("job:{job_id}:events", json_event)` — notifies WebSocket clients.

Final status logic:
- All sections `completed` → `COMPLETED`.
- Some sections failed; failed fraction ≤ `partial_failure_threshold` (0.5) → `PARTIAL_SUCCESS`.
- Failed fraction > threshold → `FAILED`.

### 9.10 Progress Milestones

| Progress | Phase |
|----------|-------|
| 0% | Initialising |
| 5% | Research started |
| 10%–80% | Sections (proportional to completed / total) |
| 82% | Knowledge fusion |
| 90% | Report writing |
| 95% | Report review |
| 99% | Exporting |
| 100% | Completed |

### 9.11 Progress Streaming (`worker/streaming.py`, `app/api/websocket.py`)

- `JobStreamer` publishes JSON events to Redis pub/sub channel `job:{job_id}:events`.
- Event types: `section_start`, `progress`, `completed`, `error`.
- WebSocket at `/ws/deep-research/{job_id}`:
  - `tenant_id` query param required; verified against Redis hash → MongoDB fallback.
  - Mismatch → `websocket.close(4403)`.
  - Subscribes to `job:{job_id}:events` pub/sub; relays JSON to WebSocket client.
  - Fallback when Redis unavailable: MongoDB polling every 3 seconds, synthetic events.

---

## 10. Dispatcher & Kubernetes (`worker/dispatcher.py`)

### 10.1 Purpose

Long-running K8s **Deployment** (not a Job) that bridges the Redis Stream to one-off K8s worker
Jobs. One dispatcher replica is sufficient; multiple replicas safe via consumer group semantics and
409-AlreadyExists dedup.

### 10.2 Consumer Group Model

- Stream: `dr:jobs:stream`
- Group: `"dispatchers"`
- Consumer: `"dispatcher-{POD_NAME}"` (POD_NAME injected via K8s Downward API; falls back to `socket.gethostname()`).
- `XREADGROUP BLOCK 5000ms COUNT 1` — blocking read, one message at a time.
- **The worker calls XACK**, not the dispatcher — this is the delivery guarantee.
- Dispatcher does NOT XACK after spawning; message stays in PEL until worker ACKs.

### 10.3 Message Processing Steps

1. Validate `job_id` is a valid BSON `ObjectId` — malformed → immediate XACK, no retry.
2. `Deep_Research_Jobs.find_one({"_id": ObjectId(job_id)})` — idempotency check: only proceed if `status == QUEUED`. Already RUNNING/COMPLETED → XACK.
3. Back-pressure loop: `count_documents({"status": "RUNNING"}) >= max_concurrent_jobs` → `asyncio.sleep(30)` until slot available.
4. Spawn K8s `batch/v1 Job` via `BatchV1Api.create_namespaced_job()` (runs in executor — sync SDK).
   - 409 AlreadyExists → treated as success (safe dedup for HA replicas).
   - Spawn failure → leave in PEL for PEL sweep retry.
5. No XACK — message remains in PEL.

### 10.4 K8s Job Spec

- Name: `dr-worker-{job_id[:12]}` (deterministic, enables 409 dedup).
- Annotation: `kadal/job_id: {job_id}`.
- Image: `settings.dispatcher.worker_image` (required at runtime).
- Command: `["python", "-m", "worker.main"]`.
- Env (from secret `kadal-worker-env` + injected):
  - `EKS_JOB_ID = {job_id}`
  - `OTEL_SERVICE_NAME = kadal-deepresearch-worker`
- Resources: CPU 500m–2000m, Memory 1Gi–4Gi (configurable).
- `ttlSecondsAfterFinished: 300` — auto-cleanup.
- `backoffLimit: 0` — dispatcher handles retries via PEL.
- `restartPolicy: Never`.
- Service account: `kadal-worker`.

### 10.5 PEL Sweep (Stuck Job Recovery)

Runs every `pel_check_interval_seconds` (300 s = 5 min). Claims entries idle > `stuck_job_timeout_seconds` (3,600 s = 1 hr).

| MongoDB status | Condition | Action |
|---------------|-----------|--------|
| `COMPLETED` / `PARTIAL_SUCCESS` / `FAILED` | — | XACK (worker crashed before ACKing) |
| `RUNNING` | elapsed > 2 × stuck_timeout (2 hr) | Mark `FAILED` + XACK |
| `RUNNING` | elapsed ≤ 2 × stuck_timeout | Skip (still healthy) |
| `QUEUED` | `times_delivered` < 3 | Re-spawn K8s Job |
| `QUEUED` | `times_delivered` ≥ 3 | Mark `FAILED` + XACK (poison pill) |
| Missing `job_id` | — | XACK bad message |

---

## 11. Tools Layer

### 11.1 Tool Summary

| Tool | File | Source type | External API | Auth | Timeout | Content |
|------|------|------------|-------------|------|---------|---------|
| Tavily | `tools/tavily_search.py` | `web` | `POST api.tavily.com/search` | `api_key` in JSON body | 30 s | Full raw page text (`include_raw_content=True`) |
| Serper | `tools/serper_search.py` | `web` | `POST google.serper.dev/search` | `X-API-KEY` header | 30 s | Snippets only |
| arXiv | `tools/arxiv_search.py` | `arxiv` | arxiv SDK + PDF HTTP | None | 60 s | Full PDF text (PyMuPDF) or abstract fallback |
| Content Lake | `tools/content_lake.py` | `content_lake` | `POST {cl_search_endpoint}` | Bearer token | `content_lake_timeout` (30 s default) | Semantic text chunks |
| Files | `tools/file_content.py` | `files` | Redis → LOR API → S3 | Bearer / IAM | — | Pre-extracted file text |

### 11.2 Searcher Execution (`nodes/searcher.py`)

All enabled tools run in parallel via `asyncio.gather`. After gathering:
- Tavily is primary; Serper triggered only if Tavily returns 0 results.
- Files included on iteration 0 only (already known — no re-fetch on later iterations).
- Dedup by URL.
- Sort by score descending.
- Cap at `max_sources_per_section`.
- Increment `search_iteration`.

### 11.3 File Content Fallback Chain (`tools/file_content.py`)

```
Redis GET "extracted_text_{object_id}"
  │ hit → return cached text
  └ miss → Redis GET "extracted_url_{object_id}"
              │ hit → fetch from S3 URL directly
              └ miss → GET {lor_endpoint}/objects/{object_id}
                            → parse extracted_text_file_url
                            → S3 GetObject → decode UTF-8
                            → cache to Redis
```

### 11.4 arXiv Dual Mode (`tools/arxiv_search.py`)

- **Primary**: `ArxivLoader` (LangChain Community) → PDF download + PyMuPDF text extraction.
  - Timeout: 60 s, 2 retries.
- **Fallback**: raw `arxiv.Client()` → title + abstract only.
- All calls via `asyncio.get_running_loop().run_in_executor()` (arxiv SDK is synchronous).
- Category filters applied when `search_strategy` specifies `cat:cs.CL`, `cat:cs.AI`, etc.

---

## 12. Database Layer

### 12.1 MongoDB Collections

Database name: `Dev_Kadal` (configurable via `MONGO_DB_NAME`).

| Collection | Primary key | Indexes | Description |
|------------|------------|---------|-------------|
| `Deep_Research_Jobs` | `_id` (ObjectId) | `{tenant_id, user_id}` (name: `tenant_user`), `{status}` (name: `status`) | Planning + execution job lifecycle |
| `Deep_Research_Reports` | `_id` (ObjectId) | `{report_id}` UNIQUE (name: `report_id_unique`), `{tenant_id, user_id, executed_at DESC}` (name: `tenant_user_executed_at`) | Completed report metadata + citations |
| `Source_Scores` | `{url_hash, tenant_id}` | — | Aggregated source quality scores per tenant |
| `Langgraph_Checkpoints` | (managed by `AsyncMongoDBSaver`) | (managed by saver — do not add custom indexes) | LangGraph execution graph state checkpoints |
| `Background_Jobs` | `_id` (ObjectId) | `{job_type, job_status}`, `{job_id}` | Dispatcher-level job tracking (used by `run.py`) |

Indexes created by `db/indexes.py:setup_all_indexes()` — idempotent, called on every startup.

### 12.2 Key Document Schemas

**`Deep_Research_Jobs`:**
```
_id (ObjectId), job_id (str UUID), tenant_id, user_id, chat_bot_id
status (PlanningJobStatus | ExecutionJobStatus)
topic, refined_topic
analysis: { depth_of_research, audience, objective, domain, recency_scope, source_scope }
plan (ResearchPlan dict), checklist (list[str]), uploaded_files
tools_enabled (dict), llm_model (str)
report_id (str UUID, set at QUEUED transition)
created_date (YYYY-MM-DD UTC), created_at, updated_at, started_at, completed_at
metadata: { otel_carrier (dict), otel_trace_id (str) }
planning_state (full PlanningState dict — used for graph re-entry in Modes 2/3)
error (str | null)
```

**`Deep_Research_Reports`:**
```
_id (ObjectId), report_id (str UUID), job_id (ObjectId), tenant_id, user_id, chat_bot_id
topic (str), title (str), status (ExecutionJobStatus)
depth_of_research (str), section_count (int), citation_count (int)
summary (str | null), citations (list[{id, title, url, source_type}])
llm_model (str), s3_key (str | null)
executed_at (ISO-8601 | null), created_at (ISO-8601)
```

**`Source_Scores` (upserted per source per tenant):**
```
url_hash (SHA-256), url, domain, tenant_id, section_id, source_type
authenticity_score, relevance_score, recency_score, composite_score
times_used ($inc), scores_history (capped list[float], max 20), scored_at
```

**`Background_Jobs`:**
```
job_id (str), job_type (str), job_status ("QUEUED" | ...)
job_submitted_date, source_path, call_back_url, job_completed_date
```

### 12.3 Tenant Isolation Rule

**Every single MongoDB query in the codebase includes `tenant_id` in the filter.**

This is enforced at application level (MongoDB has no row-level security). The `tenant_id` value
flows from the Keycloak token → `TokenUser.tenant_id` → every route handler → every Mongo call.

---

## 13. Redis Keys Reference

| Key pattern | Type | Written by | Read by | TTL |
|-------------|------|-----------|---------|-----|
| `job:{job_id}:status` | Hash | `run.py`, `executor.py`, `exporter.py` | `status` route, `websocket.py` | 24 hr (`job_status_ttl_seconds`) |
| `job:{job_id}:events` | Pub/Sub channel | `JobStreamer` (executor) | `websocket.py` | N/A (ephemeral) |
| `rate:plan:day:{user_id}:{YYYY-MM-DD}` | Hash | `plan.py`, `executor.py` | `plan.py` | 25 hr (`rate_limit_plan_daily_ttl_seconds`) |
| `rate:run:{tenant_id}:{user_id}` | String | `run.py` (Lua INCR) | `run.py` | 1 hr (`rate_limit_run_ttl_seconds`) |
| `dr:jobs:stream` | Stream | `run.py` (`XADD`) | `dispatcher.py` (`XREADGROUP`) | `maxlen ~10,000` |
| `dr:dispatch:msgid:{job_id}` | String | `run.py` | `executor.py` (XACK) | 24 hr (`msgid_ttl_seconds`) |
| `extracted_text_{object_id}` | String | `file_content.py` (cached after fetch) | `file_content.py` | No TTL |
| `extracted_url_{object_id}` | String | (external ingest) | `file_content.py` | No TTL |
| `file:{object_id}:content` | String | (external ingest) | `context_builder.py` | No TTL |

---

## 14. Observability

### 14.1 OpenTelemetry (`app/tracing.py`)

- SDK: `opentelemetry-sdk==1.40.0` + OTLP gRPC exporter.
- Sampler: `TraceIdRatioBased(otel_sample_rate)` (default 1.0 = 100%; lower in production).
- Auto-instrumentors active: FastAPI, httpx, pymongo, redis, logging.
- Insecure gRPC for in-cluster sidecars (`otel_insecure=True`); set `False` for TLS external collectors.

**Cross-process trace propagation (API → Worker):**
```
plan.py / run.py: inject_trace_carrier() → metadata.otel_carrier (MongoDB)
                                                         │
                                               executor.py reads it
                                                         │
                                         extract_trace_context() → child span
                                                         │
                                              execution graph spans
```

- `@node_span("name")` decorator wraps async node functions in child spans.
- `_NoOpTracer` fallback when `otel_enabled=False`.

### 14.2 Langfuse LLM Tracing (`app/tracing.py`)

- Self-hosted Langfuse (default `http://localhost:3000`).
- Trace name conventions: `"plan:{job_id}"` (planning), `"execution:{job_id}"` (worker).
- Metadata attached: `tenant_id`, `report_id`, `topic` (truncated to 200 chars), `otel_trace_id`.
- All LLM `ainvoke()` calls routed through the Langfuse LangChain callback handler.
- Disabled via `LANGFUSE_ENABLED=false` (e.g. unit tests).

### 14.3 Structured Logging (`app/logging_utils.py`)

`_JsonFormatter` outputs one JSON object per line:
```json
{"ts": "...", "level": "INFO", "logger": "...", "msg": "...", "trace_id": "...", "span_id": "...", "service": "kadal-deepresearch-api"}
```

- `trace_id` and `span_id` injected by `OpenTelemetryLoggingInstrumentor`.
- `service` stamped by a custom log `Filter`.
- Used by both the API process and the worker process (service name differs via `OTEL_SERVICE_NAME`).

---

## 15. LLM Layer (`app/llm_factory.py`, `app/config.py`)

### 15.1 Provider Selection

```python
# All nodes use this pattern:
from app.llm_factory import get_llm

llm = get_llm("report_writing", temperature=0.4)
# get_llm resolves: provider → model name → LangChain model class
```

`get_llm(task, temperature)` dispatches:
- `"google"` → `ChatGoogleGenerativeAI(model=..., temperature=..., google_api_key=...)`
- `"openai"` → `ChatOpenAI(model=..., temperature=..., openai_api_key=..., base_url=...)`
- `"anthropic"` → `ChatAnthropic(model=..., temperature=..., anthropic_api_key=...)`

### 15.2 Timeout & Resilience

All LLM calls are wrapped:
```python
response = await asyncio.wait_for(
    llm.ainvoke(messages),
    timeout=settings.llm_timeout_seconds  # default 120s
)
```

Failure behaviour:
- Planning nodes: auto-approve on timeout/failure (graph continues with best-effort state).
- Execution nodes: section marked `"partial"` or `"failed"`; graph continues to next node/section.

---

## 16. Local Development

### 16.1 Docker Compose Services (`docker-compose.yml`)

| Service | Image | Port | Profile | Description |
|---------|-------|------|---------|-------------|
| `redis` | `redis:7-alpine` | 6379 | (default) | Cache + Redis Streams |
| `api` | (local build) | 8000 | (default) | FastAPI + uvicorn |
| `worker` | (local build) | — | `worker` | EKS Job runner (requires `EKS_JOB_ID`) |
| `dispatcher` | (local build) | — | `dispatcher` | Redis Streams → K8s (dry_run=true by default) |

MongoDB uses Atlas (SRV URI from `.env`); no local MongoDB container.

### 16.2 Setup Commands

```bash
# Install Python dependencies
pip install -r requirements.txt

# Start Redis only
docker-compose up redis

# Apply MongoDB indexes (run once per environment or on schema changes)
python -m db.indexes

# Run API server
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Run a specific worker job directly (bypass dispatcher)
EKS_JOB_ID=<job_id> python -m worker.main

# Run dispatcher in dry-run mode (no actual K8s jobs)
DISPATCHER__DRY_RUN=true python -m worker.dispatcher_main

# Start API + Redis together
docker-compose up --build

# Start dispatcher profile
docker-compose --profile dispatcher up --build
```

### 16.3 Required Environment Variables

```bash
# MongoDB
MONGO_URI=mongodb+srv://...@.../Dev_Kadal   # Atlas SRV URI

# Redis
REDIS_HOST=localhost
REDIS_PASSWORD=                              # empty for local

# LLM (at least one provider)
LLM_PROVIDER=google                          # google | openai | anthropic
GEMINI_API_KEY=...                           # required when LLM_PROVIDER=google
OPENAI_API_KEY=...                           # required when LLM_PROVIDER=openai
ANTHROPIC_API_KEY=...                        # required when LLM_PROVIDER=anthropic

# Storage
S3_BUCKET=kadal-reports
S3_REGION=ap-south-1                         # default

# Search tools
TAVILY_API_KEY=...
SERPER_API_KEY=...

# Auth (Keycloak — note: typo in var name is intentional)
KEYCLOACK_INTROSPECT_URL=https://.../.../protocol/openid-connect/token/introspect

# Optional
CONTENT_LAKE_URL=...                         # only when tools.content_lake=true
LOR_ENDPOINT=...                             # for uploaded file object lookup
LANGFUSE_PUBLIC_KEY=...
LANGFUSE_SECRET_KEY=...
LANGFUSE_BASE_URL=http://localhost:3000
OTEL_COLLECTOR_ENDPOINT=...                  # leave empty to disable OTLP export
MONGO_DB_NAME=Dev_Kadal                      # override for production: Prod_Kadal
DISPATCHER__WORKER_IMAGE=...                 # required for dispatcher (non-dry-run)
```

### 16.4 Layer Build Order

```
Layer 1  Graph logic        graphs/planning/  +  graphs/execution/
Layer 2  MongoDB indexes    db/indexes.py
Layer 3  Tools              tools/
Layer 4  FastAPI + Worker   app/  +  worker/
```

Do not run Layer 4 before Layers 1–3 are tested.
