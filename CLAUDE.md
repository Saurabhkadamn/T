# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Project

**Kadal AI — Deep Research Microservice**
A FastAPI + LangGraph backend that lets users run multi-source AI research and receive a streamed HTML report. Full architecture is in `arch.md`.

---

## Build Strategy: Bottom-Up (Strict Order)

> **No boilerplate generation. Build each layer from first principles, then test it before moving to the next.**

### Layer 1 — Graph Logic (Start Here)
Build the LangGraph state types and graph wiring before any API or DB code exists.

1. `graphs/planning/state.py` → `PlanningState` TypedDict
2. `graphs/planning/nodes/` → one file per node, in pipeline order:
   - `context_builder.py` (pure Python, no LLM)
   - `query_analyzer.py` (Gemini 2.5 Pro)
   - `clarifier.py` (returns questions to caller)
   - `plan_creator.py` (Gemini 2.5 Pro)
   - `plan_reviewer.py` (Gemini 2.5 Pro, max 2 self-reviews)
3. `graphs/planning/graph.py` → wire nodes, compile graph
4. `graphs/execution/state.py` → `ExecutionState` + `SectionResearchState` TypedDicts
5. `graphs/execution/nodes/` → in execution order:
   - `supervisor.py` (spawns via `Send()` API)
   - `search_query_gen.py` (Gemini 2.5 Flash)
   - `searcher.py` (parallel async across sources)
   - `source_verifier.py` (web URLs only)
   - `compressor.py` (**critical** — 50K tokens → 3-5K, Flash)
   - `scorer.py` (Flash)
   - `knowledge_fusion.py` (Pro, conflict resolution)
   - `report_writer.py` (Pro, streams HTML chunks)
   - `report_reviewer.py` (Pro, max 1 revision loop)
   - `exporter.py` (no LLM — S3 + Mongo writes)
6. `graphs/execution/section_subgraph.py` → isolated sub-graph per section
7. `graphs/execution/graph.py` → wire with `AsyncMongoDBSaver` checkpointer

**Test each graph independently** with mocked LLM calls before any API layer exists.

### Layer 2 — MongoDB Collections
Create all collections with correct indexes before the service layer touches them.

| Collection | Type | Key Indexes |
|-----------|------|-------------|
| `background_jobs` | existing — add `DEEP_RESEARCH` type | `{type:1, status:1}`, `{ref_id:1}` |
| `deep_research_jobs` | new | `{job_id:1}` unique, `{tenant_id:1, user_id:1}`, `{status:1}` |
| `deep_research_reports` | new | `{report_id:1}` unique, `{tenant_id:1, user_id:1, executed_at:-1}` |
| `source_scores` | new | `{url_hash:1, tenant_id:1}`, `{domain:1}` |
| `langgraph_checkpoints` | new | managed by `AsyncMongoDBSaver` |

**Critical constraint:** Every query to every collection MUST include `tenant_id`.

### Layer 3 — Tools (Search Integrations)
`tools/` — one file per source, each independently testable:
- `tavily_search.py`, `serper_search.py`, `arxiv_search.py` → via MCP client
- `content_lake.py` → internal API (enabled only when `tools.content_lake=true`)
- `file_content.py` → Redis cache lookup by `object_id`
- `mcp_client.py` → shared MCP session management

### Layer 4 — FastAPI + Worker (Build + Test Together)
Only start this after Layers 1-3 pass their tests.

**API endpoints in build order:**
1. `POST /api/deepresearch/plan` → calls planning graph synchronously
2. `POST /api/deepresearch/run` → creates `background_jobs` + `deep_research_jobs` + `deep_research_reports`
3. `GET /api/deepresearch/status/{job_id}` → Redis hash first, fallback to MongoDB
4. `GET /api/deepresearch/reports` + `GET /api/deepresearch/reports/{report_id}`
5. `WS /ws/deep-research/{job_id}` → subscribe to Redis pub/sub channel `job:{job_id}:events`

**Worker** (`worker/`): `main.py` → `executor.py` → runs execution graph → `streaming.py` publishes to Redis pub/sub.

---

## Architecture: Key Patterns

### Two Separate Graphs (Critical Separation Point)
- **Planning Graph** — runs synchronously inside FastAPI during `/plan`
- **Execution Graph** — runs asynchronously inside the EKS Worker during `/run`
- Separated by the human-in-the-loop plan approval boundary

### Supervisor-Researcher Pattern
The execution graph supervisor spawns section researchers **in parallel** via LangGraph's `Send()` API. Each section researcher runs as an **isolated sub-graph with its own context window** — they do not share state.

### Context Compression (★ Critical Node)
The `compressor.py` node is non-negotiable. Raw search results are 50K+ tokens; without compression, downstream LLM calls exceed context windows and cost becomes uncontrollable. Compress per section before returning to supervisor.

### Multi-Model Assignment
- **Gemini 2.5 Pro** — reasoning tasks: query analysis, plan creation, plan review, supervisor reflection, knowledge fusion, report writing, report review
- **Gemini 2.5 Flash** — mechanical tasks: search query generation, context compression, source scoring

### Knowledge Fusion Priority (when sources conflict)
1. Uploaded files (user-specific, highest trust)
2. Content Lake (verified internal org docs)
3. Arxiv (peer-reviewed academic)
4. Web (Tavily/Serper — broadest but least verified)

### Loop Limits (Enforce in Graph Routing)
| Loop | Max |
|------|-----|
| clarification ↔ query_understanding | 3 |
| plan_refinement ↔ planning | 3 |
| plan self-review | 2 |
| supervisor reflection rounds | 2 |
| report revision (reviewer → writer) | 1 |

### Streaming Architecture
```
EKS Worker → PUBLISH → Redis Pub/Sub (channel: job:{job_id}:events) → FastAPI WS Handler → Browser
```
WebSocket events: `section_start`, `content_delta`, `progress`, `completed`, `error`

### Checkpointing (Crash Recovery)
```python
checkpointer = AsyncMongoDBSaver(mongo_client, db_name="kadal_platform", collection_name="langgraph_checkpoints")
config = {"configurable": {"thread_id": job_id}}  # thread_id = job_id
```
On EKS Job retry, the worker resumes from the last completed node — completed sections are never re-executed.

---

## State Types (Reference)

**`PlanningState`** fields: `topic_id`, `tenant_id`, `user_id`, `chat_bot_id`, `chat_history`, `uploaded_files`, `file_contents`, `original_topic`, `refined_topic`, `needs_clarification`, `clarification_questions`, `clarification_answers`, `clarification_count`, `depth_of_research`, `audience`, `objective`, `domain`, `recency_scope`, `source_scope`, `assumptions`, `plan`, `plan_approved`, `plan_revision_count`, `checklist`, `self_review_count`, `status`, `error`

**`ExecutionState`** fields: `job_id`, `report_id`, `tenant_id`, `user_id`, `topic`, `refined_topic`, `plan`, `checklist`, `sections`, `attachments`, `file_contents`, `tools_enabled`, `max_search_iterations`, `max_sources_per_section`, `section_results` (reducer: `operator.add`), `compressed_findings` (reducer: `operator.add`), `citations` (reducer: `operator.add`), `source_scores` (reducer: `operator.add`), `reflection_count`, `knowledge_gaps`, `fused_knowledge`, `report_html`, `review_feedback`, `revision_count`, `s3_key`, `progress`, `status`, `error`, `otel_trace_id`

**`SectionResearchState`** fields: `section_id`, `section_title`, `section_description`, `search_strategy`, `max_search_iterations`, `max_sources`, `file_contents`, `search_queries`, `raw_search_results`, `search_iteration`, `verified_urls`, `compressed_findings`, `source_scores`, `citations`, `section_findings`, `section_citations`, `section_source_scores`

---

## Redis Key Schema

```
job:{job_id}:status           → Hash {status, progress, section, updated_at}  TTL: 24hr
job:{job_id}:events           → Pub/Sub channel
file:{object_id}:content      → String (platform-wide file cache)
source:{url_hash}:{tenant_id} → Hash {score, recency, times_used}  TTL: 7 days
rate:plan:{tenant_id}:{uid}   → Counter with TTL
rate:run:{tenant_id}:{uid}    → Counter with TTL
```

---

## Environment Variables

```
MONGO_URI, REDIS_HOST, REDIS_PASSWORD, GEMINI_API_KEY,
S3_BUCKET, LANGFUSE_HOST, OTEL_COLLECTOR_ENDPOINT,
TAVILY_API_KEY, SERPER_API_KEY, CONTENT_LAKE_URL
```

---

## Development Commands

*(To be added once `requirements.txt` / `pyproject.toml` and `docker-compose.yml` are created)*

Expected commands will follow this pattern:
```bash
docker-compose up          # Start MongoDB + Redis locally
python -m pytest           # Run all tests
python -m pytest tests/graphs/   # Run graph-layer tests only
python -m black .          # Format
python -m mypy app/ worker/ graphs/   # Type check
uvicorn app.main:app --reload   # Run API locally
```

---

## Security Rules (Non-Negotiable)

- Every MongoDB query must include `tenant_id` — no exceptions
- S3 keys must be namespaced: `deep-research/{tenant_id}/{user_id}/{report_id}.html`
- `deep_research_reports` must NOT be touched by "Clear History" chat operations
- All secrets via K8s Secrets / `.env` — never hardcoded
- Report access via S3 presigned URLs only (1hr expiry)
