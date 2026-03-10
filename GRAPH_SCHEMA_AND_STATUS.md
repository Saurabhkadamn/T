# Kadal AI — Graph State Schemas, MongoDB Writes & Status Reference

> Complete reference for both LangGraph graphs: state schemas, all MongoDB operations,
> status values, and lifecycle transitions. Keep this in sync with any schema changes.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Planning Graph — State Schema](#2-planning-graph--state-schema)
3. [Execution Graph — State Schemas](#3-execution-graph--state-schemas)
4. [MongoDB Collections — Schema Reference](#4-mongodb-collections--schema-reference)
5. [Planning Graph — Status Lifecycle](#5-planning-graph--status-lifecycle)
6. [Execution Graph — Status Lifecycle](#6-execution-graph--status-lifecycle)
7. [All MongoDB Writes — What, When, Where](#7-all-mongodb-writes--what-when-where)
8. [Redis Keys Reference](#8-redis-keys-reference)
9. [Complete Status Transition Diagram](#9-complete-status-transition-diagram)

---

## 1. Architecture Overview

```
Client
  │
  ▼
POST /api/deepresearch/plan         ← Planning Graph (runs SYNCHRONOUSLY inside FastAPI)
  │   returns: plan or clarification questions
  │   MongoDB: Deep_Research_Jobs upserted after every call
  │
  ▼
POST /api/deepresearch/run          ← User approves plan
  │   MongoDB: Deep_Research_Jobs updated + Deep_Research_Reports inserted + Background_Jobs inserted
  │   Redis:   job:{job_id}:status hash created
  │
  ▼
EKS Worker picks up job
  │
  ▼
Execution Graph (runs ASYNCHRONOUSLY in EKS Worker)
  │   MongoDB: Deep_Research_Jobs updated at start and end
  │            Deep_Research_Reports updated on completion
  │   Redis:   job:{job_id}:status updated throughout
  │   S3:      HTML report uploaded
  │
  ▼
GET /api/deepresearch/status/{job_id}   ← Redis (fast) → MongoDB (fallback)
GET /api/deepresearch/reports/{report_id}
WS  /ws/deep-research/{job_id}          ← Redis pub/sub stream
```

**Key separation:** The Planning Graph and Execution Graph run in completely different processes.
They share state only through MongoDB (`Deep_Research_Jobs` document).

---

## 2. Planning Graph — State Schema

**File:** `graphs/planning/state.py`
**Used by:** All planning nodes + `app/api/routes/plan.py`

### 2.1 Auxiliary Types

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

### 2.2 `PlanningState` — Full Field Reference

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
| **Lifecycle** | `status` | `PlanningStatus` | `"pending"` | See Section 5 |
| | `error` | `str \| None` | `None` | Set when status = "failed" |

### 2.3 `PlanningStatus` — All Values

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

### 2.4 Planning Graph Node → State Changes

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

## 3. Execution Graph — State Schemas

**File:** `graphs/execution/state.py`

### 3.1 `ExecutionStatus` — All Values

```python
ExecutionStatus = Literal[
    "queued",           # job created, EKS Worker not yet started
    "initializing",     # worker started, loading file contents from Redis
    "researching",      # section sub-graphs running in parallel
    "reflecting",       # supervisor reflection round running
    "fusing",           # knowledge_fusion node running
    "writing",          # report_writer streaming HTML
    "reviewing",        # report_reviewer checking quality
    "exporting",        # exporter writing to S3 + MongoDB
    "completed",        # full success
    "partial_success",  # some sections failed but report still generated
    "failed",           # unrecoverable error, no report produced
]
```

### 3.2 `ToolsEnabled`
```
web           : bool
arxiv         : bool
content_lake  : bool
files         : bool
```

### 3.3 `RawSearchResult` (intermediate, not persisted to MongoDB)
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

### 3.4 `VerifiedUrl` (intermediate, not persisted to MongoDB)
```
url          : str
is_authentic : bool    # False if redirect trap, paywalled, or 404
url_hash     : str     # SHA-256 of normalised URL — Redis/Mongo key
domain       : str     # e.g. "arxiv.org"
```

### 3.5 `Citation` (persisted trimmed form to Deep_Research_Reports)
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

### 3.6 `SourceScore` (internal graph state only — not persisted to MongoDB)
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

### 3.7 `CompressedFinding` (intermediate, used by knowledge_fusion)
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

### 3.8 `SectionResult` (returned from each section sub-graph to supervisor)
```
section_id              : str
section_title           : str
status                  : "completed" | "partial" | "failed"
source_count            : int
search_iterations_used  : int
error                   : str | None    # None when status = "completed"
```

### 3.9 `SectionResearchState` — Full Field Reference

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

### 3.10 `ExecutionState` — Full Field Reference

| Group | Field | Type | Default | Description |
|---|---|---|---|---|
| **Identity** | `job_id` | `str` | required | Primary key; also LangGraph thread_id |
| | `report_id` | `str` | required | Pre-assigned report document ID |
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
| | `status` | `ExecutionStatus` | `"initializing"` | See Section 6 |
| | `error` | `str \| None` | `None` | Set on failed / partial_success |
| **Observability** | `otel_trace_id` | `str` | `""` | OpenTelemetry trace ID |

### 3.11 Execution Graph Node → State Changes

| Node | Fields Written | Status Transition |
|---|---|---|
| `supervisor` (phase 1) | `status="researching"` | `initializing` → `researching` |
| `supervisor` (phase 2 / reflection) | `reflection_count`, `knowledge_gaps`, `status="reflecting"` | `researching` → `reflecting` |
| `section_subgraph` | `section_results`, `compressed_findings`, `citations`, `source_scores` (reducers — internal only) | (stays `researching`) |
| `knowledge_fusion` | `fused_knowledge`, `status="fusing"` | → `fusing` |
| `report_writer` | `report_html`, `status="writing"` | → `writing` |
| `report_reviewer` | `review_feedback`, `revision_count`, `status="reviewing"` | → `reviewing` |
| `exporter` | `s3_key`, `status`, `error`, `progress` | → `exporting` → `completed` / `partial_success` |
| *(on crash)* | `error` | → `failed` |

---

## 4. MongoDB Collections — Schema Reference

**Database:** `Dev_Kadal` (Atlas cluster)

### 4.1 `Deep_Research_Jobs`

Primary document — exists for the full lifetime of a research session.

```jsonc
{
  "job_id"        : "uuid4",            // unique; used as LangGraph thread_id
  "report_id"     : "uuid4 | null",     // set when /run is called
  "tenant_id"     : "string",           // MANDATORY on every query
  "user_id"       : "string",
  "chat_bot_id"   : "",                 // deprecated, always ""

  "topic"         : "string",           // original user query
  "refined_topic" : "string",

  "llm_model"     : "google/gemini-2.5-pro",  // provider/model_name

  // ── Status (see Section 5 & 6 for all values and transitions) ─────────
  "status"        : "plans_ready",      // see Section 5.2 for all values

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

**Indexes:**
- `{ job_id: 1 }` — UNIQUE
- `{ tenant_id: 1, user_id: 1 }`
- `{ status: 1 }`

---

### 4.2 `Deep_Research_Reports`

Created by `/run`, finalised by `exporter.py`.

```jsonc
{
  "report_id"        : "uuid4",        // unique
  "job_id"           : "uuid4",        // FK to Deep_Research_Jobs
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
  "status"           : "research_queued",  // → "completed" after exporter runs
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

**Indexes:**
- `{ report_id: 1 }` — UNIQUE
- `{ tenant_id: 1, user_id: 1, executed_at: -1 }`

---

### 4.3 `Background_Jobs`

Platform-level job queue. Consumed by EKS Job scheduler.

```jsonc
{
  "job_id"            : "uuid4",       // same as Deep_Research_Jobs.job_id
  "job_type"          : "DEEP_RESEARCH",
  "tenant_id"         : "string",
  "source_path"       : "",
  "call_back_url"     : null,
  "job_submitted_date": "ISO-8601",
  "job_status"        : "QUEUED",      // platform scheduler updates this
  "error"             : null,
  "job_completed_date": null
}
```

**Indexes:**
- `{ job_type: 1, job_status: 1 }`
- `{ job_id: 1 }`

---

### 4.4 `Langgraph_Checkpoints`

Managed automatically by `AsyncMongoDBSaver`. Thread_id = job_id.
Used for crash recovery — worker resumes from last completed node.

---

## 5. Planning Graph — Status Lifecycle

### 5.1 Graph-internal `PlanningStatus`

These are the values inside the `PlanningState.status` field while the graph runs:

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

### 5.2 MongoDB `status` in `Deep_Research_Jobs` (mapped from graph status)

The plan endpoint maps `PlanningStatus` → DB status before saving:

| Graph `PlanningState.status` | Stored in DB as | Meaning |
|---|---|---|
| `awaiting_approval` | **`plans_ready`** | Plan complete, waiting for user approval |
| `needs_clarification` | **`needs_clarification`** | Questions sent back to user |
| `failed` | **`failed`** | Planning error |
| *(other)* | same value | Raw graph status |

### 5.3 Mode Routing Summary

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

## 6. Execution Graph — Status Lifecycle

### 6.1 Full Status Transition (in DB order)

```
[/run called]
research_queued                ← set by run.py in Deep_Research_Jobs + Deep_Research_Reports
      │                           set by run.py in Redis job:{job_id}:status
      ▼
[EKS Worker starts]
research                       ← set by executor.py immediately on start (Deep_Research_Jobs)
      │
      ▼ (supervisor phase 1: dispatches sections)
researching                    ← status in Redis (via streamer); graph-internal status
      │
      ▼ (all sections complete)
reflecting                     ← supervisor phase 2 (LLM reflection round, max 2)
      │
      ▼ (gaps resolved or max rounds reached)
fusing                         ← knowledge_fusion node
      │
      ▼
writing                        ← report_writer node
      │
      ▼
reviewing                      ← report_reviewer node
      │         ┌──────────────────────┐
      │         │ needs_revision?       │
      │         └─────── max 1 loop ───┘
      ▼
exporting                      ← exporter node running

      ├──► completed            ← exporter sets in Deep_Research_Jobs + Deep_Research_Reports
      │                            Redis progress=100
      │
      └──► partial_success      ← exporter sets if S3 or MongoDB write had errors
                                   Redis progress=90

[exception in executor]
      └──► failed               ← executor.py sets in Deep_Research_Jobs
```

### 6.2 `ExecutionStatus` Values — Quick Reference

| Status | Set By | Stored In | Meaning |
|---|---|---|---|
| `queued` | *(initial value in state)* | Redis | Job created, worker not started |
| `initializing` | executor.py (state init) | Redis (streamer) | Worker loading files |
| `researching` | supervisor node | Redis (streamer) | Sections running in parallel |
| `reflecting` | supervisor node (phase 2) | Redis (streamer) | LLM reflection round |
| `fusing` | knowledge_fusion node | Redis (streamer) | Merging all findings |
| `writing` | report_writer node | Redis (streamer) | Generating HTML report |
| `reviewing` | report_reviewer node | Redis (streamer) | Quality check |
| `exporting` | executor.py (streamer) | Redis (streamer) | Writing to S3 + MongoDB |
| `completed` | exporter.py | MongoDB + Redis | Full success |
| `partial_success` | exporter.py | MongoDB + Redis | Report generated with errors |
| `failed` | executor.py (catch block) | MongoDB + Redis | Unrecoverable error |

### 6.3 `SectionResult.status` Values

Each section sub-graph returns one of:

| Value | Meaning |
|---|---|
| `completed` | All searches ran, compression succeeded, error=None |
| `partial` | Some searches failed but usable findings produced |
| `failed` | No usable output; supervisor may spawn a gap-fill section |

---

## 7. All MongoDB Writes — What, When, Where

### 7.1 POST `/api/deepresearch/plan` (every call)

**Collection: `Deep_Research_Jobs`** — `replace_one(upsert=True)`

| Trigger | Fields Written | DB Status Set |
|---|---|---|
| Mode 1 fresh plan | Full document (all fields) | `needs_clarification` OR `plans_ready` |
| Mode 2 clarification answered | Full document (replace) | `needs_clarification` OR `plans_ready` |
| Mode 3 plan revision | Full document (replace) | `plans_ready` |

**Document written:** All fields listed in Section 4.1.
Fields updated every call: `planning_state`, `status`, `plan`, `checklist`, `analysis`, `refined_topic`, `updated_at`, `metadata`.

---

### 7.2 POST `/api/deepresearch/run`

Three MongoDB writes happen in sequence:

#### Write 1 — `Deep_Research_Jobs` UPDATE

```
Filter:  { job_id, tenant_id }
Condition: status must == "plans_ready" (400 error otherwise)

$set:
  status        → "research_queued"
  report_id     → uuid4 (newly generated)
  updated_at    → now_iso
  metadata.otel_carrier    → W3C carrier dict
  metadata.otel_trace_id   → traceparent value
```

#### Write 2 — `Deep_Research_Reports` INSERT

```
New document:
  report_id          = uuid4
  job_id             = body.job_id
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
  status             = "research_queued"
  s3_key             = null
  executed_at        = null
  created_at         = now_iso
```

#### Write 3 — `Background_Jobs` INSERT (non-fatal)

```
New document:
  job_id              = body.job_id
  job_type            = "DEEP_RESEARCH"
  tenant_id           = from token
  source_path         = ""
  call_back_url       = null
  job_submitted_date  = now_iso
  job_status          = "QUEUED"
  error               = null
  job_completed_date  = null
```

**On cancel action:**
```
Deep_Research_Jobs $set: { status: "cancelled", updated_at }
```

---

### 7.3 `worker/executor.py` — On Worker Start

**Collection: `Deep_Research_Jobs`** — UPDATE

```
Filter: { job_id, tenant_id }
$set:
  status      → "research"
  started_at  → now_iso
  updated_at  → now_iso
```

---

### 7.4 `graphs/execution/nodes/exporter.py` — On Completion

Two MongoDB writes happen in order:

#### Write 1 — `Deep_Research_Reports` UPSERT

```
Filter: { report_id, tenant_id }
$set (full doc):
  report_id      = state.report_id
  job_id         = state.job_id
  tenant_id      = state.tenant_id
  user_id        = state.user_id
  topic          = state.topic
  refined_topic  = state.refined_topic
  llm_model      = fetched from Deep_Research_Jobs
  s3_key         = S3 key
  status         = "completed"
  executed_at    = now_iso
  section_count  = len(state.sections)
  citation_count = len(citations)
  summary        = first 500 chars of HTML (tags stripped)
  citations      = [{id, title, url, source_type}]  ← trimmed
  otel_trace_id  = state.otel_trace_id
```

#### Write 2 — `Deep_Research_Jobs` UPDATE

```
Filter: { job_id, tenant_id }
$set:
  status       → "completed" (or "partial_success" if errors occurred)
  completed_at → now_iso
  updated_at   → now_iso
```

---

### 7.5 `worker/executor.py` — On Success (after exporter completes)

**Collection: `Deep_Research_Jobs`** — UPDATE (confirmation write)

```
Filter: { job_id, tenant_id }
$set:
  status    → "completed"
  progress  → 100
  updated_at → now_iso
```

---

### 7.6 `worker/executor.py` — On Exception (catch block)

**Collection: `Deep_Research_Jobs`** — UPDATE

```
Filter: { job_id } (+ tenant_id if already known)
$set:
  status     → "failed"
  error      → exception message string
  updated_at → now_iso
```

---

## 8. Redis Keys Reference

| Key Pattern | Type | TTL | Content |
|---|---|---|---|
| `job:{job_id}:status` | Hash | 24 hr | `status`, `progress`, `tenant_id`, `updated_at`, `started_at` |
| `job:{job_id}:events` | Pub/Sub channel | — | JSON events: `section_start`, `content_delta`, `progress`, `completed`, `error` |
| `file:{object_id}:content` | String | platform TTL | Extracted file text |
| `rate:plan:day:{user_id}:{YYYY-MM-DD}` | Hash | 25 hr | `{job_id}: "planning"\|"completed"\|"failed"` |
| `rate:run:{tenant_id}:{user_id}` | Counter | 1 hr | Increments per run call |

**Rate limit logic:**
- **Plan** — daily per-user limit; only non-`"failed"` entries count toward quota
- **Run** — hourly per-user-per-tenant counter; uses INCR+EXPIRE Lua script

**WebSocket events published to `job:{job_id}:events`:**
```json
{ "type": "progress",       "status": "researching", "progress": 45, "section": "Section Title" }
{ "type": "section_start",  "section_id": "uuid",    "section_title": "string" }
{ "type": "content_delta",  "chunk": "html fragment" }
{ "type": "completed",      "report_id": "uuid",     "html_url": "presigned-s3-url" }
{ "type": "error",          "status": "failed",      "message": "error string" }
```

---

## 9. Complete Status Transition Diagram

```
══════════════════════════════════════════════════════════════
                    PLANNING GRAPH
══════════════════════════════════════════════════════════════

  [POST /plan - Mode 1]
  DB: (document created with planning_state)
  DB status: needs_clarification  ──► User answers ──► [POST /plan - Mode 2]
                                                        DB status: plans_ready
                                                               │
  DB status: plans_ready  ◄────────────────────────────────────┘
       ▲                         OR (plan approved directly)
       │
  [POST /plan - Mode 3: plan_feedback]
  DB status: plans_ready  (revised plan)


══════════════════════════════════════════════════════════════
                  HANDOFF BOUNDARY (Human Approval)
══════════════════════════════════════════════════════════════

  [POST /run]  (requires job in plans_ready)
  Deep_Research_Jobs:    plans_ready → research_queued
  Deep_Research_Reports: (created)   → research_queued
  Background_Jobs:       (created)   → QUEUED
  Redis hash:            → research_queued


══════════════════════════════════════════════════════════════
                    EXECUTION GRAPH
══════════════════════════════════════════════════════════════

  [EKS Worker picks up job]
  Deep_Research_Jobs:    research_queued → research
  Redis hash:            → initializing (via streamer)

  [supervisor dispatches sections in parallel]
  Redis hash:            → researching

  [each section_subgraph completes]
  Redis hash:            → researching (progress increments 10%→80%)

  [supervisor reflection, if needed]
  Redis hash:            → reflecting

  [knowledge_fusion]
  Redis hash:            → fusing (82%)

  [report_writer]
  Redis hash:            → writing (90%)

  [report_reviewer]
  Redis hash:            → reviewing (95%)
  (loop back to writer max 1 time if revision needed)

  [exporter]
  Redis hash:            → exporting (99%)
  Deep_Research_Reports: research_queued → completed
  S3:                    HTML uploaded

  ┌─────────────────────────────────────────┐
  │ Success:                                │
  │   Deep_Research_Jobs: → completed       │
  │   Redis hash: → completed (100%)        │
  │   Redis pub/sub: "completed" event      │
  ├─────────────────────────────────────────┤
  │ Partial (S3/Mongo error in exporter):   │
  │   Deep_Research_Jobs: → partial_success │
  │   Redis hash: → partial_success (90%)   │
  │   Redis pub/sub: "error" event          │
  ├─────────────────────────────────────────┤
  │ Crash (executor exception):             │
  │   Deep_Research_Jobs: → failed          │
  │   Redis pub/sub: "error" event          │
  └─────────────────────────────────────────┘


══════════════════════════════════════════════════════════════
     ALL POSSIBLE DB STATUSES FOR Deep_Research_Jobs
══════════════════════════════════════════════════════════════

  needs_clarification   ← plan.py: graph returned needs_clarification
  plans_ready           ← plan.py: graph returned awaiting_approval
  cancelled             ← run.py: user called /run with action="cancel"
  research_queued       ← run.py: user approved plan
  research              ← executor.py: worker started
  completed             ← exporter.py + executor.py: full success
  partial_success       ← exporter.py: report generated with storage errors
  failed                ← executor.py: unrecoverable exception


══════════════════════════════════════════════════════════════
     ALL POSSIBLE STATUSES FOR Deep_Research_Reports
══════════════════════════════════════════════════════════════

  research_queued       ← run.py: document created
  completed             ← exporter.py: report written to S3 + MongoDB
```

---

*Last updated: auto-generated from source code. Files referenced:*
- `graphs/planning/state.py`
- `graphs/execution/state.py`
- `app/api/routes/plan.py`
- `app/api/routes/run.py`
- `graphs/execution/nodes/exporter.py`
- `worker/executor.py`
- `db/indexes.py`
