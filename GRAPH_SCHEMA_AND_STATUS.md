# Kadal AI — Graph State Schemas, MongoDB Writes & Status Reference

> Complete reference for both LangGraph graphs: state schemas, all MongoDB operations,
> status values, and lifecycle transitions. Keep this in sync with any schema changes.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Canonical Status Enumerations](#2-canonical-status-enumerations)
3. [Planning Graph — State Schema](#3-planning-graph--state-schema)
4. [Execution Graph — State Schemas](#4-execution-graph--state-schemas)
5. [MongoDB Collections — Schema Reference](#5-mongodb-collections--schema-reference)
6. [Planning Graph — Status Lifecycle](#6-planning-graph--status-lifecycle)
7. [Execution Graph — Status Lifecycle](#7-execution-graph--status-lifecycle)
8. [All MongoDB Writes — What, When, Where](#8-all-mongodb-writes--what-when-where)
9. [Redis Keys Reference](#9-redis-keys-reference)
10. [Complete Status Transition Diagram](#10-complete-status-transition-diagram)

---

## 1. Architecture Overview

```
Client
  │
  ▼
POST /api/deepresearch/plan         ← Planning Graph (runs SYNCHRONOUSLY inside FastAPI)
  │   returns: plan or clarification questions
  │   MongoDB: Deep_Research_Jobs pre-inserted (PLAN_INITIATED) then upserted after every call
  │
  ▼
POST /api/deepresearch/run          ← User approves plan
  │   MongoDB: Deep_Research_Jobs updated (→ QUEUED) + Deep_Research_Reports inserted (QUEUED)
  │   Redis:   job:{job_id}:status hash created
  │   Note: NO Background_Jobs insert — removed from this service
  │
  ▼
EKS Worker picks up job
  │
  ▼
Execution Graph (runs ASYNCHRONOUSLY in EKS Worker)
  │   MongoDB: Deep_Research_Jobs updated at start (→ RUNNING) and end (→ COMPLETED/FAILED)
  │            Deep_Research_Reports updated on completion (→ COMPLETED/FAILED)
  │   Redis:   job:{job_id}:status updated throughout
  │   S3:      HTML report uploaded
  │
  ▼
GET /api/deepresearch/status/{job_id}   ← Redis (fast) → MongoDB (fallback)
GET /api/deepresearch/reports/{report_id}
WS  /ws/deep-research/{job_id}?tenant_id=...   ← Redis pub/sub stream; tenant_id is query param
```

**Key separation:** The Planning Graph and Execution Graph run in completely different processes.
They share state only through MongoDB (`Deep_Research_Jobs` document).

**Document identity:** `Deep_Research_Jobs._id` is a MongoDB `ObjectId`. The string form of this
ObjectId is used as `job_id` in all API responses and LangGraph `thread_id`. Queries always filter
by `{"_id": ObjectId(job_id), "tenant_id": ...}`.

---

## 2. Canonical Status Enumerations

**File:** `app/enums.py` — **single source of truth for all job status strings.**

All route files, executor, and exporter import from here. Never use raw string literals
for status values — always use the enum.

```python
# app/enums.py

class PlanningJobStatus(str, Enum):
    PLAN_INITIATED      = "PLAN_INITIATED"     # Set on pre-insert before graph runs (Mode 1)
    NEED_CLARIFICATION  = "NEED_CLARIFICATION" # Graph needs user to answer questions
    PLAN_READY          = "PLAN_READY"         # Plan complete; waiting for user approval via /run
    PLAN_EDIT_REQUESTED = "PLAN_EDIT_REQUESTED" # Set when Mode 3 revision starts
    PLAN_CANCELED       = "PLAN_CANCELED"      # User cancelled via /run action="cancel"


class ExecutionJobStatus(str, Enum):
    QUEUED    = "QUEUED"     # /run called; EKS Worker not yet started
    RUNNING   = "RUNNING"   # Worker started; execution graph active
    COMPLETED = "COMPLETED" # Full success — report in S3 + MongoDB
    FAILED    = "FAILED"    # Unrecoverable error
```

### Streaming-only statuses (Redis only — never stored in MongoDB)

These appear in the `job:{job_id}:status` Redis hash and WebSocket events during execution,
but are **never written to MongoDB**:

| Value | Published by | Meaning |
|---|---|---|
| `initializing` | executor.py (streamer) | Worker started, loading files |
| `researching` | supervisor node | Sections running in parallel |
| `reflecting` | supervisor node (phase 2) | LLM reflection round |
| `fusing` | knowledge_fusion node | Merging all findings |
| `writing` | report_writer node | Generating HTML report |
| `reviewing` | report_reviewer node | Quality check |
| `exporting` | executor.py (streamer) | Writing to S3 + MongoDB |

---

## 3. Planning Graph — State Schema

**File:** `graphs/planning/state.py`
**Used by:** All planning nodes + `app/api/routes/plan.py`

### 3.1 Auxiliary Types

#### `ChatMessage`
```
role     : "user" | "assistant"
content  : str
```

#### `UploadedFile`
```
object_id : str    # Redis cache key: file:{object_id}:content
filename  : str
mime_type : str
```

#### `PlanSection`
```
section_id       : str    # stable UUID, used as sub-graph thread_id suffix
title            : str
description      : str    # 1-3 sentence summary of what the section covers
search_strategy  : str    # comma-separated source types e.g. "arxiv,web"
```

#### `ResearchPlan`
```
title              : str
summary            : str              # 2-4 sentence executive summary
sections           : list[PlanSection]
estimated_sources  : int              # total expected source count (UI progress bar)
```

### 3.2 `PlanningState` — Full Field Reference

| Group | Field | Type | Default | Description |
|---|---|---|---|---|
| **Identity** | `topic_id` | `str` | required | Unique session ID (= job_id in DB) |
| | `tenant_id` | `str` | required | Multi-tenancy partition key |
| | `user_id` | `str` | required | |
| | `chat_bot_id` | `str` | `""` | Deprecated; kept for schema compat |
| **Input** | `chat_history` | `list[ChatMessage]` | `[]` | Conversation turns passed at request time |
| | `uploaded_files` | `list[UploadedFile]` | `[]` | File metadata |
| | `file_contents` | `dict[str, str]` | `{}` | `object_id → text` (loaded by context_builder) |
| **Context** | `context_brief` | `str` | `""` | LLM-generated summary of chat + files |
| **Topic** | `original_topic` | `str` | required | Verbatim user query |
| | `refined_topic` | `str` | `""` | query_analyzer's normalised version |
| **Clarification** | `needs_clarification` | `bool` | `False` | True when questions are required |
| | `clarification_questions` | `list[str]` | `[]` | Questions returned to caller |
| | `clarification_answers` | `list[str]` | `[]` | Answers from user (parallel list) |
| | `clarification_count` | `int` | `0` | Completed clarification rounds |
| **Research Params** | `depth_of_research` | `"surface" \| "intermediate" \| "in-depth"` | `"intermediate"` | Controls source count + compression |
| | `audience` | `str` | `""` | e.g. "C-suite executives" |
| | `objective` | `str` | `""` | e.g. "compare approaches" |
| | `domain` | `str` | `""` | e.g. "machine learning" |
| | `recency_scope` | `str` | `""` | e.g. "last_6_months", "all_time" |
| | `source_scope` | `list[SourceType]` | `["web"]` | Active source integrations |
| | `assumptions` | `list[str]` | `[]` | LLM assumptions on ambiguous queries |
| **Plan** | `plan` | `ResearchPlan \| None` | `None` | None until plan_creator runs |
| | `plan_revision_count` | `int` | `0` | Incremented on each user rejection |
| | `plan_feedback` | `str \| None` | `None` | User's rejection feedback text |
| | `checklist` | `list[str]` | `[]` | Quality gates (3-5 items) |
| **Lifecycle** | `status` | `PlanningStatus` | `"pending"` | Graph-internal only — see Section 6 |
| | `error` | `str \| None` | `None` | Set when status = "failed" |

### 3.3 `PlanningStatus` — Graph-Internal Values

> **IMPORTANT:** These are graph-internal state values. They are **NOT stored in MongoDB as-is**.
> The plan endpoint maps them to `PlanningJobStatus` enum values before saving. See Section 6.2.

```python
PlanningStatus = Literal[
    "pending",              # initial state before graph runs
    "analyzing",            # query_analyzer node running
    "needs_clarification",  # query_analyzer decided questions are needed
    "planning",             # plan_creator node running
    "awaiting_approval",    # plan ready, human must approve via /run
    "failed",               # unrecoverable error
]
```

### 3.4 Planning Graph Node → State Changes

| Node | Fields Written to State | Status Set |
|---|---|---|
| `context_builder` | `file_contents`, `context_brief` | (unchanged) |
| `query_analyzer` | `refined_topic`, `depth_of_research`, `audience`, `objective`, `domain`, `recency_scope`, `source_scope`, `assumptions`, `needs_clarification`, `clarification_questions` | `"analyzing"` → `"needs_clarification"` or stays in flow |
| `clarifier` | `clarification_questions`, `needs_clarification=True` | `"needs_clarification"` |
| `plan_creator` | `plan`, `checklist` | `"planning"` |
| `plan_reviewer` | (approves or triggers revision) | leads to `"awaiting_approval"` or loops back |
| *(terminal)* | — | `"awaiting_approval"` |
| *(on error)* | `error` | `"failed"` |

---

## 4. Execution Graph — State Schemas

**File:** `graphs/execution/state.py`

### 4.1 `ExecutionStatus` — All Values

> **Note:** Only `COMPLETED` and `FAILED` (uppercase) are stored in MongoDB.
> The lowercase streaming values appear only in Redis and WebSocket events.

```python
ExecutionStatus = Literal[
    # Streaming-only (Redis + WebSocket) — never in MongoDB:
    "initializing",     # worker started, loading file contents from Redis
    "researching",      # section sub-graphs running in parallel
    "reflecting",       # supervisor reflection round running
    "fusing",           # knowledge_fusion node running
    "writing",          # report_writer streaming HTML
    "reviewing",        # report_reviewer checking quality
    "exporting",        # exporter writing to S3 + MongoDB

    # Stored in MongoDB (via ExecutionJobStatus enum):
    "QUEUED",           # job created, EKS Worker not yet started
    "RUNNING",          # worker active
    "COMPLETED",        # full success
    "FAILED",           # unrecoverable error
]
```

### 4.2 `ToolsEnabled`
```
web           : bool
arxiv         : bool
content_lake  : bool
files         : bool
```

### 4.3 `RawSearchResult` (intermediate, not persisted to MongoDB)
```
result_id    : str        # UUID for deduplication
source_type  : SourceType
query        : str        # the search query that produced this result
url          : str        # empty for file/arxiv results
title        : str
content      : str        # raw extracted text (can be 50K+ tokens)
published_at : str        # ISO-8601 or ""
score        : float      # relevance 0.0–1.0 from search tool
```

### 4.4 `VerifiedUrl` (intermediate, not persisted to MongoDB)
```
url          : str
is_authentic : bool    # False if redirect trap, paywalled, or 404
url_hash     : str     # SHA-256 of normalised URL — Redis/Mongo key
domain       : str     # e.g. "arxiv.org"
```

### 4.5 `Citation` (persisted trimmed form to Deep_Research_Reports)
```
citation_id  : str        # UUID
section_id   : str
source_type  : SourceType
title        : str
url          : str        # empty for file citations
authors      : str
published_at : str
snippet      : str        # short verbatim extract
trust_tier   : int        # 1=files, 2=content_lake, 3=arxiv, 4=web
```
> **Note:** When saved to MongoDB, trimmed to `{id, title, url, source_type}` only.

### 4.6 `SourceScore` (internal graph state only — not persisted to MongoDB)
```
url_hash            : str
url                 : str
domain              : str
tenant_id           : str        # propagated for content_lake tool calls
section_id          : str
source_type         : SourceType
authenticity_score  : float      # 0.0–1.0
relevance_score     : float      # 0.0–1.0
recency_score       : float      # 0.0–1.0
composite_score     : float      # 0.5·relevance + 0.3·recency + 0.2·authenticity
scored_at           : str        # ISO-8601
```
> **Note:** Created by `scorer.py`, merged into `ExecutionState.source_scores` via reducer,
> then read by `knowledge_fusion.py` to sort/rank findings for the LLM prompt.
> **Not written to any MongoDB collection.**

### 4.7 `CompressedFinding` (intermediate, used by knowledge_fusion)
```
section_id          : str
section_title       : str
compressed_text     : str     # depth-dependent token budget
source_types_used   : list[SourceType]
token_estimate      : int
```
Compression targets by depth:
- `surface`: ~2,500 tokens (3 sources)
- `intermediate`: ~5,000 tokens (5 sources)
- `in-depth`: ~8,000 tokens (10 sources)

### 4.8 `SectionResult` (returned from each section sub-graph to supervisor)
```
section_id              : str
section_title           : str
status                  : "completed" | "partial" | "failed"
source_count            : int
search_iterations_used  : int
error                   : str | None    # None when status = "completed"
```

### 4.9 `SectionResearchState` — Full Field Reference

State for each isolated section sub-graph (spawned via `Send()` API).

| Group | Field | Type | Description |
|---|---|---|---|
| **Identity** | `section_id` | `str` | Set by Send(), immutable inside sub-graph |
| | `section_title` | `str` | |
| | `section_description` | `str` | |
| | `search_strategy` | `str` | e.g. "arxiv,web" |
| **Config** | `tenant_id` | `str` | Propagated from ExecutionState for content_lake calls |
| | `max_search_iterations` | `int` | |
| | `max_sources` | `int` | |
| | `compression_target_tokens` | `int` | 2500 / 5000 / 8000 per depth |
| | `max_chars_per_source` | `int` | Per-source input cap for compressor |
| | `file_contents` | `dict[str, str]` | Read-only copy |
| **Search** | `search_queries` | `list[str]` | Generated by search_query_gen |
| | `raw_search_results` | `list[RawSearchResult]` | Accumulated across iterations |
| | `search_iteration` | `int` | Current iteration count (0-indexed) |
| **Processing** | `verified_urls` | `list[VerifiedUrl]` | Output of source_verifier (web only) |
| | `compressed_text` | `str` | Single string output of compressor |
| | `source_scores` | `list[SourceScore]` | Output of scorer |
| | `citations` | `list[Citation]` | Assembled during compression/scoring |
| **Output** | `section_findings` | `str` | Final narrative (same as compressed_text) |
| | `section_citations` | `list[Citation]` | |
| | `section_source_scores` | `list[SourceScore]` | |

### 4.10 `ExecutionState` — Full Field Reference

| Group | Field | Type | Default | Description |
|---|---|---|---|---|
| **Identity** | `job_id` | `str` | required | String form of Deep_Research_Jobs `_id`; also LangGraph thread_id |
| | `report_id` | `str` | required | UUID4 string; pre-assigned by /run |
| | `tenant_id` | `str` | required | Partition key — MUST be on every DB query |
| | `user_id` | `str` | required | |
| **Topic** | `topic` | `str` | `""` | Original user query |
| | `refined_topic` | `str` | `""` | Normalised version from planning |
| **Plan** | `plan` | `ResearchPlan` | required | Approved plan from planning graph |
| | `checklist` | `list[str]` | `[]` | Quality checklist from plan_reviewer |
| | `sections` | `list[dict]` | `[]` | List of PlanSection dicts |
| **Attachments** | `attachments` | `list[UploadedFile]` | `[]` | |
| | `file_contents` | `dict[str, str]` | `{}` | Loaded from Redis by worker |
| | `tools_enabled` | `ToolsEnabled` | required | |
| **Config** | `max_search_iterations` | `int` | from depth | |
| | `max_sources_per_section` | `int` | from depth | |
| | `max_chars_per_source` | `int` | from depth | Per-source input cap for compressor |
| | `report_revision_max` | `int` | from depth | surface=1, intermediate=2, in-depth=3 |
| **Reducers** *(parallel write)* | `section_results` | `list[SectionResult]` + `operator.add` | `[]` | Written by parallel section sub-graphs |
| | `compressed_findings` | `list[CompressedFinding]` + `operator.add` | `[]` | |
| | `citations` | `list[Citation]` + `operator.add` | `[]` | |
| | `source_scores` | `list[SourceScore]` + `operator.add` | `[]` | Internal only — used by knowledge_fusion for LLM ranking; not persisted |
| **Supervisor** | `reflection_count` | `int` | `0` | Completed reflection rounds (max 2) |
| | `knowledge_gaps` | `list[str]` | `[]` | Gaps identified in reflection |
| **Knowledge** | `fused_knowledge` | `str \| None` | `None` | None until knowledge_fusion runs |
| **Report** | `report_html` | `str \| None` | `None` | None until report_writer runs |
| | `review_feedback` | `str \| None` | `None` | None if reviewer approves outright |
| | `revision_count` | `int` | `0` | Writer→reviewer revision loops (max 1) |
| | `s3_key` | `str \| None` | `None` | None until exporter uploads |
| **Lifecycle** | `progress` | `int` | `0` | 0–100 percentage |
| | `status` | `ExecutionStatus` | `"initializing"` | See Section 7 |
| | `error` | `str \| None` | `None` | Set on failed |
| **Observability** | `otel_trace_id` | `str` | `""` | OpenTelemetry trace ID |

### 4.11 Execution Graph Node → State Changes

| Node | Fields Written | Status Transition |
|---|---|---|
| `supervisor` (phase 1) | `status="researching"` | `initializing` → `researching` |
| `supervisor` (phase 2 / reflection) | `reflection_count`, `knowledge_gaps`, `status="reflecting"` | `researching` → `reflecting` |
| `section_subgraph` | `section_results`, `compressed_findings`, `citations`, `source_scores` (reducers — internal only) | (stays `researching`) |
| `knowledge_fusion` | `fused_knowledge`, `status="fusing"` | → `fusing` |
| `report_writer` | `report_html`, `status="writing"` | → `writing` |
| `report_reviewer` | `review_feedback`, `revision_count`, `status="reviewing"` | → `reviewing` |
| `exporter` | `s3_key`, `status`, `error`, `progress` | → `exporting` → `COMPLETED` / `FAILED` |
| *(on crash)* | `error` | → `FAILED` |

---

## 5. MongoDB Collections — Schema Reference

**Database:** `Dev_Kadal` (Atlas cluster)

### 5.1 `Deep_Research_Jobs`

Primary document — exists for the full lifetime of a research session.

**Identity:** `_id` is MongoDB ObjectId (primary key). `job_id` in API responses is the string
representation of `_id`. All queries filter by `{"_id": ObjectId(job_id), "tenant_id": ...}`.

```jsonc
{
  // _id is MongoDB ObjectId — primary key; never stored as a separate field
  "report_id"     : "uuid4 | null",     // set when /run is called; UUID4 string
  "tenant_id"     : "string",           // MANDATORY on every query
  "user_id"       : "string",
  "chat_bot_id"   : "",                 // deprecated, always ""

  "topic"         : "string",           // original user query
  "refined_topic" : "string",

  "llm_model"     : "google/gemini-2.5-pro",  // provider/model_name

  // ── Status (see Sections 6 & 7 for all values and transitions) ─────────
  // Uses PlanningJobStatus or ExecutionJobStatus enum values (always UPPERCASE)
  "status"        : "PLAN_READY",       // see Section 2 for all valid values

  "progress"      : 0,                  // 0–100 (set during execution)

  // ── Plan data ──────────────────────────────────────────────────────────
  "plan"          : {                   // ResearchPlan — null until planning complete
    "title"             : "string",
    "summary"           : "string",
    "sections"          : [
      {
        "section_id"       : "uuid4",
        "title"            : "string",
        "description"      : "string",
        "search_strategy"  : "arxiv,web"
      }
    ],
    "estimated_sources" : 15
  },
  "checklist"     : ["string"],         // quality gates 3-5 items

  // ── Analysis (from query_analyzer) ────────────────────────────────────
  "analysis"      : {
    "depth_of_research" : "intermediate",  // "surface"|"intermediate"|"in-depth"
    "audience"          : "string",
    "objective"         : "string",
    "domain"            : "string",
    "recency_scope"     : "last_6_months",
    "source_scope"      : ["web", "arxiv"],
    "assumptions"       : ["string"]
  },

  // ── Tools ─────────────────────────────────────────────────────────────
  "uploaded_files"  : [{ "object_id": "", "filename": "", "mime_type": "" }],
  "tools_enabled"   : { "web": true, "arxiv": true, "content_lake": false, "files": false },

  // ── Saved planning graph state (for graph resume on modes 2 & 3) ──────
  "planning_state" : { /* full PlanningState subset — see _STATE_FIELDS_TO_SAVE */ },

  // ── Observability ─────────────────────────────────────────────────────
  "metadata"      : {
    "otel_trace_id"    : "string",
    "otel_carrier"     : { "traceparent": "..." },  // W3C carrier for worker trace linking
    "total_tokens_used": 0,
    "total_cost_usd"   : 0.0,
    "pro_calls"        : 0,
    "flash_calls"      : 0
  },

  // ── Timestamps ────────────────────────────────────────────────────────
  "started_at"    : null,         // set by executor.py when worker starts
  "completed_at"  : null,         // set by exporter or executor on finish
  "error"         : null,
  "created_at"    : "ISO-8601",
  "created_date"  : "YYYY-MM-DD", // UTC date; for rate-limit hash updates
  "updated_at"    : "ISO-8601"
}
```

**Indexes** (from `db/indexes.py`):
- `{ tenant_id: 1, user_id: 1 }` — name: `tenant_user`
- `{ status: 1 }` — name: `status`
- **No explicit `job_id` UNIQUE index** — uniqueness enforced by MongoDB `_id` (ObjectId)

---

### 5.2 `Deep_Research_Reports`

Created by `/run` (status=`QUEUED`), finalised by `exporter.py` (status=`COMPLETED`/`FAILED`).

```jsonc
{
  "report_id"        : "uuid4",        // unique UUID4 string
  "job_id"           : ObjectId,       // FK to Deep_Research_Jobs._id (stored as ObjectId)
  "tenant_id"        : "string",       // MANDATORY on every query
  "user_id"          : "string",
  "chat_bot_id"      : "",             // deprecated

  "topic"            : "string",       // original user query
  "title"            : "string",       // plan.title (used as HTML <h1>)
  "refined_topic"    : "string",       // set by exporter
  "llm_model"        : "string",       // fetched from Deep_Research_Jobs by exporter

  "depth_of_research": "intermediate",
  "section_count"    : 5,              // number of plan sections

  // ── Populated by exporter ─────────────────────────────────────────────
  // Uses ExecutionJobStatus enum: "QUEUED" → "COMPLETED" or "FAILED"
  "status"           : "QUEUED",       // → "COMPLETED" or "FAILED" after exporter runs
  "s3_key"           : null,           // set by exporter: deep-research/{tenant}/{user}/{report_id}.html
  "summary"          : null,           // first 500 chars of HTML stripped of tags
  "citation_count"   : 0,             // length of citations array
  "citations"        : [],             // trimmed: [{id, title, url, source_type}]
  "otel_trace_id"    : "",

  // ── Timestamps ────────────────────────────────────────────────────────
  "executed_at"      : null,           // set by exporter
  "created_at"       : "ISO-8601"
}
```

**Indexes** (from `db/indexes.py`):
- `{ report_id: 1 }` — UNIQUE, name: `report_id_unique`
- `{ tenant_id: 1, user_id: 1, executed_at: -1 }` — name: `tenant_user_executed_at`

---

### 5.3 `Background_Jobs`

> **NOTE:** Kadal's `/run` endpoint **no longer inserts** into `Background_Jobs`.
> This insert was present in earlier code but has been removed. `Background_Jobs` may still
> exist in the Atlas cluster for platform use by other services, but `run.py` does not write to it.
> No indexes for `Background_Jobs` are defined in `db/indexes.py`.

---

### 5.4 `Langgraph_Checkpoints`

Managed automatically by `AsyncMongoDBSaver`. Thread_id = job_id.
Used for crash recovery — worker resumes from last completed node.

---

## 6. Planning Graph — Status Lifecycle

### 6.1 Graph-internal `PlanningStatus`

These values live in `PlanningState.status` while the graph runs. They are graph-internal
and are mapped to MongoDB enum values before any DB write.

```
pending
  │
  ▼ (context_builder runs)
  │
  ├──► (Mode 1: query_analyzer runs)
  │      analyzing
  │        │
  │        ├──► needs_clarification   ← graph exits; caller calls /plan again (Mode 2)
  │        │
  │        └──► (plan_creator runs)
  │               planning
  │                 │
  │                 ▼ (plan_reviewer runs)
  │               awaiting_approval   ← graph exits cleanly; plan is ready
  │
  ├──► (Mode 2: clarification answers given → query_analyzer → plan_creator)
  │      analyzing → planning → awaiting_approval
  │
  └──► (Mode 3: plan_feedback given → plan_creator → plan_reviewer)
         planning → awaiting_approval

  (any node failure) → failed
```

### 6.2 MongoDB `status` in `Deep_Research_Jobs` (mapped from graph status)

The plan endpoint maps graph-internal `PlanningStatus` → `PlanningJobStatus` enum before saving:

| Graph `PlanningState.status` | Stored in DB as | When |
|---|---|---|
| *(Mode 1 pre-insert, before graph)* | **`PLAN_INITIATED`** | Immediately on Mode 1 call, before graph runs |
| `awaiting_approval` | **`PLAN_READY`** | After plan_creator + plan_reviewer succeed |
| `needs_clarification` | **`NEED_CLARIFICATION`** | When clarification questions are generated |
| `failed` | **`failed`** | Planning error (raw value — graph sets this) |
| *(Mode 3 starts, before graph re-runs)* | **`PLAN_EDIT_REQUESTED`** | Before graph re-runs in Mode 3 |
| *(cancel action in /run)* | **`PLAN_CANCELED`** | User cancels via /run action="cancel" |

### 6.3 Mode 1 — Pre-insert Pattern

Mode 1 (fresh plan) now **pre-inserts a minimal document** with `status=PLAN_INITIATED` before
running the graph. This reserves the ObjectId immediately. After the graph completes, a full
`replace_one(upsert=True)` overwrites the document.

```python
# Pre-insert (before graph):
{"_id": oid, "tenant_id": ..., "user_id": ..., "topic": ...,
 "status": "PLAN_INITIATED", "created_at": ..., "updated_at": ...}

# Full replace (after graph):
replace_one({"_id": ObjectId(job_id), "tenant_id": tenant_id}, full_doc, upsert=True)
```

### 6.4 Mode Routing Summary

| Request shape | Mode | Graph entry point |
|---|---|---|
| No `job_id` | Mode 1 (fresh) | `context_builder` |
| `job_id` + `clarification_answers` | Mode 2 (answer clarification) | `query_analyzer` |
| `job_id` + `plan_feedback` | Mode 3 (revise plan) | `plan_creator` |

**Loop limits (enforced in `graph.py` routing):**
- Clarification rounds: max **3**
- Plan revision rounds: max **3** (from `settings.loops.plan_revision_max`)
- Plan self-review (reviewer → creator): max **2**

---

## 7. Execution Graph — Status Lifecycle

### 7.1 Full Status Transition (in chronological order)

```
[/run called]
Deep_Research_Jobs:    PLAN_READY → QUEUED
Deep_Research_Reports: (created)  → QUEUED
Redis hash:            status=QUEUED, progress=0

[EKS Worker starts]
Deep_Research_Jobs:    QUEUED → RUNNING  (set by executor.py immediately)
Redis hash:            status=initializing, started_at=... (streaming only)

[supervisor phase 1: dispatches sections]
Redis hash:            status=researching (streaming only)

[each section_subgraph completes]
Redis hash:            status=researching, progress 10%→80% (streaming only)

[supervisor phase 2, if depth warrants reflection]
Redis hash:            status=reflecting (streaming only)

[knowledge_fusion]
Redis hash:            status=fusing, progress=82% (streaming only)

[report_writer]
Redis hash:            status=writing, progress=90% (streaming only)

[report_reviewer]
Redis hash:            status=reviewing, progress=95% (streaming only)
(loop back to writer max 1 time if revision needed)

[exporter node]
Redis hash:            status=exporting, progress=99% (streaming only)
Deep_Research_Reports: QUEUED → COMPLETED (or FAILED if any error)
Deep_Research_Jobs:    RUNNING → COMPLETED (or FAILED) — set by exporter

[executor success confirmation]
Deep_Research_Jobs:    status=COMPLETED, progress=100

[executor exception]
Deep_Research_Jobs:    status=FAILED
```

> **Removed:** `partial_success` no longer exists. Exporter errors set `FAILED`.

### 7.2 `ExecutionStatus` Values — Quick Reference

| Status | Stored In | Set By | Meaning |
|---|---|---|---|
| `QUEUED` | MongoDB + Redis | run.py | Job created, worker not started |
| `RUNNING` | MongoDB | executor.py | Worker started; graph running |
| `initializing` | Redis only | executor.py (streamer) | Worker loading files |
| `researching` | Redis only | supervisor node | Sections running in parallel |
| `reflecting` | Redis only | supervisor node (phase 2) | LLM reflection round |
| `fusing` | Redis only | knowledge_fusion node | Merging all findings |
| `writing` | Redis only | report_writer node | Generating HTML report |
| `reviewing` | Redis only | report_reviewer node | Quality check |
| `exporting` | Redis only | executor.py (streamer) | Writing to S3 + MongoDB |
| `COMPLETED` | MongoDB + Redis | exporter.py + executor.py | Full success |
| `FAILED` | MongoDB + Redis | exporter.py or executor.py | Error (no report, or partial) |

### 7.3 `SectionResult.status` Values

Each section sub-graph returns one of:

| Value | Meaning |
|---|---|
| `completed` | All searches ran, compression succeeded, error=None |
| `partial` | Some searches failed but usable findings produced |
| `failed` | No usable output; supervisor may spawn a gap-fill section |

---

## 8. All MongoDB Writes — What, When, Where

### 8.1 POST `/api/deepresearch/plan` (every call)

**Collection: `Deep_Research_Jobs`**

#### Mode 1 — Two-phase write:

**Phase 1 — Pre-insert** (before graph, reserves ObjectId):
```
insert_one({
  "_id": ObjectId (new),
  "tenant_id": ...,
  "user_id": ...,
  "topic": ...,
  "status": "PLAN_INITIATED",
  "created_at": now_iso,
  "updated_at": now_iso,
})
```

**Phase 2 — Full replace** (after graph completes):
```
replace_one(
  filter: { "_id": ObjectId(job_id), "tenant_id": tenant_id },
  replacement: full_doc (all fields from Section 5.1),
  upsert=True
)
Status set: "NEED_CLARIFICATION" or "PLAN_READY"
```

#### Mode 2 (clarification) — Full replace after graph:
```
replace_one(filter: { "_id": ObjectId(job_id), "tenant_id": tenant_id }, full_doc, upsert=True)
Status set: "NEED_CLARIFICATION" or "PLAN_READY"
```

#### Mode 3 (plan revision) — Two writes:

**Write 1** (before graph, signals revision in-flight):
```
update_one(
  filter: { "_id": ObjectId(job_id), "tenant_id": tenant_id },
  $set: { "status": "PLAN_EDIT_REQUESTED", "updated_at": now_iso }
)
```

**Write 2** (after graph, full replace):
```
replace_one(filter, full_doc, upsert=True)
Status set: "PLAN_READY"
```

Fields updated every call: `planning_state`, `status`, `plan`, `checklist`, `analysis`,
`refined_topic`, `updated_at`, `metadata`.

---

### 8.2 POST `/api/deepresearch/run`

Two MongoDB writes happen in sequence (Background_Jobs is **not** written):

#### Write 1 — `Deep_Research_Jobs` UPDATE

```
Filter:  { "_id": ObjectId(job_id), "tenant_id": tenant_id }
Condition: status must == "PLAN_READY" (400 error otherwise)

$set:
  status        → "QUEUED"        (ExecutionJobStatus.QUEUED)
  report_id     → uuid4 (newly generated UUID4 string)
  updated_at    → now_iso
  metadata.otel_carrier    → W3C carrier dict
  metadata.otel_trace_id   → traceparent value
```

#### Write 2 — `Deep_Research_Reports` INSERT

```
New document:
  report_id          = uuid4 (UUID4 string)
  job_id             = ObjectId(body.job_id)    ← stored as ObjectId, not string
  tenant_id          = from token
  user_id            = from token
  topic              = from job_doc
  title              = plan.title
  chat_bot_id        = ""
  llm_model          = from job_doc
  depth_of_research  = from analysis
  section_count      = len(plan.sections)
  citation_count     = 0
  summary            = null
  citations          = []
  status             = "QUEUED"                ← ExecutionJobStatus.QUEUED
  s3_key             = null
  executed_at        = null
  created_at         = now_iso
```

**On cancel action** (`action="cancel"`):
```
Deep_Research_Jobs $set: { status: "PLAN_CANCELED", updated_at }
```

---

### 8.3 `worker/executor.py` — On Worker Start

**Collection: `Deep_Research_Jobs`** — UPDATE

```
Filter: { "_id": ObjectId(job_id), "tenant_id": tenant_id }
$set:
  status      → "RUNNING"     (ExecutionJobStatus.RUNNING)
  started_at  → now_iso
  updated_at  → now_iso
```

Also writes `started_at` to Redis hash:
```
HSET job:{job_id}:status started_at {started_at}
```

---

### 8.4 `graphs/execution/nodes/exporter.py` — On Completion

Two MongoDB writes happen in order:

#### Write 1 — `Deep_Research_Reports` UPSERT

```
Filter: { report_id, tenant_id }
$set (full doc):
  report_id      = state.report_id
  job_id         = ObjectId(state.job_id)   ← stored as ObjectId
  tenant_id      = state.tenant_id
  user_id        = state.user_id
  topic          = state.topic
  refined_topic  = state.refined_topic
  llm_model      = fetched from Deep_Research_Jobs
  s3_key         = S3 key
  status         = "COMPLETED"              (ExecutionJobStatus.COMPLETED)
  executed_at    = now_iso
  section_count  = len(state.sections)
  citation_count = len(citations)
  summary        = first 500 chars of HTML (tags stripped)
  citations      = [{id, title, url, source_type}]  ← trimmed
  otel_trace_id  = state.otel_trace_id
```

> **Note:** If S3 or MongoDB write fails, `status` is set to `"FAILED"` instead.
> `partial_success` no longer exists — any error → `FAILED`.

#### Write 2 — `Deep_Research_Jobs` UPDATE

```
Filter: { "_id": ObjectId(job_id), "tenant_id": tenant_id }
$set:
  status       → "COMPLETED" (or "FAILED" if errors occurred)
  completed_at → now_iso
  updated_at   → now_iso
```

---

### 8.5 `worker/executor.py` — On Success (after exporter completes)

**Collection: `Deep_Research_Jobs`** — UPDATE (confirmation write)

```
Filter: { "_id": ObjectId(job_id), "tenant_id": tenant_id }
$set:
  status    → "COMPLETED"    (ExecutionJobStatus.COMPLETED)
  progress  → 100
  updated_at → now_iso
```

---

### 8.6 `worker/executor.py` — On Exception (catch block)

**Collection: `Deep_Research_Jobs`** — UPDATE

```
Filter: { "_id": ObjectId(job_id) } (+ tenant_id if already known)
$set:
  status     → "FAILED"      (ExecutionJobStatus.FAILED)
  error      → exception message string
  updated_at → now_iso
```

---

## 9. Redis Keys Reference

| Key Pattern | Type | TTL | Content |
|---|---|---|---|
| `job:{job_id}:status` | Hash | 24 hr | `status`, `progress`, `tenant_id`, `updated_at`, `started_at` |
| `job:{job_id}:events` | Pub/Sub channel | — | JSON events published by streamer and exporter |
| `file:{object_id}:content` | String | platform TTL | Extracted file text |
| `rate:plan:day:{user_id}:{YYYY-MM-DD}` | Hash | 25 hr | `{job_id}: "planning"\|"completed"\|"failed"` |
| `rate:run:{tenant_id}:{user_id}` | Counter | 1 hr | Increments per run call |

**Rate limit logic:**
- **Plan** — daily per-user limit; only non-`"failed"` entries count toward quota
- **Run** — hourly per-user-per-tenant counter; uses INCR+EXPIRE Lua script

**WebSocket events published to `job:{job_id}:events`:**

From `worker/streaming.py` (streamer):
```json
{ "type": "progress",       "status": "researching", "progress": 45, "section": "Section Title" }
{ "type": "section_start",  "section_id": "uuid",    "section_title": "string" }
{ "type": "content_delta",  "chunk": "html fragment" }
{ "type": "completed",      "report_id": "uuid",     "html_url": "presigned-s3-url" }
{ "type": "error",          "status": "FAILED",      "message": "error string" }
```

From `exporter.py` (final status):
```json
{ "type": "completed", "status": "COMPLETED", "progress": 100, "updated_at": "ISO-8601" }
{ "type": "error",     "status": "FAILED",    "progress": 90,  "updated_at": "ISO-8601" }
```

**WebSocket auth:** `tenant_id` is a **query parameter** (`?tenant_id=...`), not a header.
The WebSocket handler verifies tenant via Redis hash → MongoDB fallback and closes with code
`4403` on mismatch.

---

## 10. Complete Status Transition Diagram

```
══════════════════════════════════════════════════════════════
                    PLANNING GRAPH
══════════════════════════════════════════════════════════════

  [POST /plan - Mode 1]
  DB: pre-insert with status=PLAN_INITIATED (reserves ObjectId)
  DB: full replace after graph → NEED_CLARIFICATION or PLAN_READY

  DB status: NEED_CLARIFICATION  ──► User answers ──► [POST /plan - Mode 2]
                                                       DB status: PLAN_READY
                                                              │
  DB status: PLAN_READY  ◄──────────────────────────────────┘
       ▲                        OR (plan approved directly)
       │
  [POST /plan - Mode 3: plan_feedback]
  DB status: PLAN_EDIT_REQUESTED (before graph)
  DB status: PLAN_READY          (after revised plan)

  [POST /run - action="cancel"]
  DB status: PLAN_CANCELED


══════════════════════════════════════════════════════════════
                HANDOFF BOUNDARY (Human Approval)
══════════════════════════════════════════════════════════════

  [POST /run]  (requires job in PLAN_READY)
  Deep_Research_Jobs:    PLAN_READY → QUEUED
  Deep_Research_Reports: (created)  → QUEUED
  Redis hash:            status=QUEUED
  (Background_Jobs: NOT written — removed from this service)


══════════════════════════════════════════════════════════════
                    EXECUTION GRAPH
══════════════════════════════════════════════════════════════

  [EKS Worker picks up job]
  Deep_Research_Jobs:    QUEUED → RUNNING
  Redis hash:            → initializing (streaming only)

  [supervisor dispatches sections in parallel]
  Redis hash:            → researching (streaming only)

  [each section_subgraph completes]
  Redis hash:            → researching (progress increments 10%→80%)

  [supervisor reflection, if needed]
  Redis hash:            → reflecting (streaming only)

  [knowledge_fusion]
  Redis hash:            → fusing (82%) (streaming only)

  [report_writer]
  Redis hash:            → writing (90%) (streaming only)

  [report_reviewer]
  Redis hash:            → reviewing (95%) (streaming only)
  (loop back to writer max 1 time if revision needed)

  [exporter]
  Redis hash:            → exporting (99%) (streaming only)
  Deep_Research_Reports: QUEUED → COMPLETED (or FAILED)
  S3:                    HTML uploaded
  Deep_Research_Jobs:    RUNNING → COMPLETED (or FAILED)

  ┌─────────────────────────────────────────┐
  │ Success:                                │
  │   Deep_Research_Jobs: → COMPLETED       │
  │   executor confirmation: progress=100   │
  │   Redis hash: → COMPLETED (100%)        │
  │   Redis pub/sub: "completed" event      │
  ├─────────────────────────────────────────┤
  │ Exporter error (S3/Mongo failure):      │
  │   Deep_Research_Jobs: → FAILED          │
  │   Deep_Research_Reports: → FAILED       │
  │   Redis hash: → FAILED (90%)            │
  │   Redis pub/sub: "error" event          │
  ├─────────────────────────────────────────┤
  │ Crash (executor exception):             │
  │   Deep_Research_Jobs: → FAILED          │
  │   Redis pub/sub: "error" event          │
  └─────────────────────────────────────────┘


══════════════════════════════════════════════════════════════
     ALL POSSIBLE DB STATUSES FOR Deep_Research_Jobs
══════════════════════════════════════════════════════════════

  PLAN_INITIATED       ← plan.py: pre-inserted before graph runs (Mode 1)
  NEED_CLARIFICATION   ← plan.py: graph returned needs_clarification
  PLAN_READY           ← plan.py: graph returned awaiting_approval
  PLAN_EDIT_REQUESTED  ← plan.py: Mode 3 revision in-flight (before graph)
  PLAN_CANCELED        ← run.py: user called /run with action="cancel"
  QUEUED               ← run.py: user approved plan
  RUNNING              ← executor.py: worker started
  COMPLETED            ← exporter.py + executor.py: full success
  FAILED               ← exporter.py or executor.py: error


══════════════════════════════════════════════════════════════
     ALL POSSIBLE STATUSES FOR Deep_Research_Reports
══════════════════════════════════════════════════════════════

  QUEUED               ← run.py: document created
  COMPLETED            ← exporter.py: report written to S3 + MongoDB
  FAILED               ← exporter.py: S3 or MongoDB write error
```

---

*Last updated: 2026-03-12. Files referenced:*
- `app/enums.py`
- `graphs/planning/state.py`
- `graphs/execution/state.py`
- `app/api/routes/plan.py`
- `app/api/routes/run.py`
- `app/api/routes/status.py`
- `app/api/websocket.py`
- `graphs/execution/nodes/exporter.py`
- `worker/executor.py`
- `db/indexes.py`
