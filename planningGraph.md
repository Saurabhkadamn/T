# Planning Graph — Architecture

> Single source of truth for the Planning Graph design.

---

## Overview

The Planning Graph runs **synchronously** inside FastAPI during `POST /api/deepresearch/plan`.
It takes a user's research request (topic + conversation context + uploaded files) and produces
a structured research plan for human approval.

- **3 nodes:** context_builder → query_analyzer → plan_creator
- **1 endpoint:** `POST /api/deepresearch/plan`
- **3 invocation modes:** fresh request, clarification answer, plan revision
- **No checkpointer:** State is saved to MongoDB by the API route after each invocation
- **No plan_reviewer node:** The human is the reviewer

---

## API Contract

### Single Endpoint: `POST /api/deepresearch/plan`

The JSON payload shape determines what happens. The presence of `job_id`,
`clarification_answers`, or `plan_feedback` controls the invocation mode.

---

### Mode 1 — Fresh Request

**Trigger:** No `job_id` in payload.

**Request:**
```json
{
  "topic": "Impact of AI on healthcare diagnostics",
  "tenant_id": "tenant_abc",
  "user_id": "user_123",
  "chat_bot_id": "bot_456",
  "uploaded_files": [
    {
      "object_id": "file_abc",
      "filename": "report.pdf",
      "mime_type": "application/pdf"
    }
  ]
}
```

**Response A — Clarification needed:**
```json
{
  "job_id": "uuid-1234",
  "status": "needs_clarification",
  "needs_clarification": true,
  "clarification_questions": [
    "Are you focusing on radiology specifically or all diagnostics?",
    "What time period — recent developments or historical overview?"
  ],
  "plan": null,
  "analysis": null,
  "checklist": [],
  "error": null
}
```

**Response B — Plan ready (no clarification needed):**
```json
{
  "job_id": "uuid-1234",
  "status": "awaiting_approval",
  "needs_clarification": false,
  "clarification_questions": [],
  "plan": {
    "title": "AI in Healthcare Diagnostics: A 3-Year Review",
    "summary": "...",
    "sections": [
      {
        "section_id": "uuid-sec-1",
        "title": "Current State of AI Diagnostics",
        "description": "...",
        "search_strategy": "web,arxiv"
      }
    ],
    "estimated_sources": 15
  },
  "analysis": {
    "depth_of_research": "intermediate",
    "audience": "healthcare professionals",
    "objective": "understand current state and trends",
    "domain": "healthcare AI",
    "recency_scope": "last_2_years",
    "source_scope": ["web", "arxiv"],
    "assumptions": ["Focus on FDA-approved tools"]
  },
  "checklist": [
    "Each section must cite at least 2 unique sources",
    "Include quantitative accuracy metrics where available"
  ],
  "error": null
}
```

---

### Mode 2 — Clarification Answer

**Trigger:** `job_id` + `clarification_answers` in payload.

**Request:**
```json
{
  "job_id": "uuid-1234",
  "tenant_id": "tenant_abc",
  "user_id": "user_123",
  "clarification_answers": [
    "Focus on radiology and pathology",
    "Last 3 years only"
  ]
}
```

**Response:** Same shape as Mode 1 Response B (plan ready).
In rare cases, the analyzer may return another round of clarification
questions (same shape as Mode 1 Response A) if the answers were
insufficient and `clarification_count < max`.

---

### Mode 3 — Plan Revision

**Trigger:** `job_id` + `plan_feedback` in payload.

**Request:**
```json
{
  "job_id": "uuid-1234",
  "tenant_id": "tenant_abc",
  "user_id": "user_123",
  "plan_feedback": "Add a section on FDA regulations and remove the cost analysis section"
}
```

**Response:** Same shape as Mode 1 Response B (revised plan returned).

**Rejection:** If `plan_revision_count >= max` (default 3), the API returns
HTTP 400 with a message suggesting the user start a new research session.

---

## API Route Logic

```
POST /api/deepresearch/plan

1. Validate tenant_id (X-Tenant-ID header must match body)
2. Rate limit check

3. Determine mode:
   if no job_id:
     → Mode 1: Generate job_id, build fresh state, run full graph
   if job_id + clarification_answers:
     → Mode 2: Load state from MongoDB, merge answers, run from query_analyzer
   if job_id + plan_feedback:
     → Mode 3: Load state from MongoDB, check revision cap, merge feedback, run from plan_creator

4. Invoke graph

5. Save state to deep_research_jobs (upsert by job_id + tenant_id)
   - After clarification: partial state (context_brief, questions, analysis so far)
   - After plan creation: full state (everything including plan + checklist)

6. Return PlanResponse
```

### MongoDB Save — Critical

State is saved to `deep_research_jobs` after EVERY invocation, not just when a plan
is produced. This is essential because:
- Mode 2 needs the `context_brief` from Mode 1
- Mode 3 needs the full state including the plan from Mode 2
- If user leaves and comes back, state must be recoverable by `job_id`

---

## Graph Topology

```
Mode 1 (fresh):
  START → context_builder → query_analyzer → plan_creator → END
                              │
                              ├─ [needs_clarification + count < max] → END
                              └─ [ready] → plan_creator → END

Mode 2 (clarification answers):
  START → query_analyzer → plan_creator → END
  (context_brief already in state from Mode 1 — skip context_builder)

Mode 3 (plan revision):
  START → plan_creator → END
  (context + analysis already in state — skip both)
```

### Conditional Edges

```
context_builder → query_analyzer                        (always)

query_analyzer →
  ├── END              (needs_clarification=true AND clarification_count < max)
  └── plan_creator     (needs_clarification=false OR clarification_count >= max)

plan_creator → END     (always — plan goes to human for approval)
```

---

## Nodes

### Node 1: context_builder

**Purpose:** Gather all raw context and produce a single structured brief using an LLM.
This is the ONLY node that touches raw inputs. All downstream nodes read the brief only.

**Runs in:** Mode 1 only.

**LLM Model:** Configurable via `settings.models.use_pro_for_context_building` (default: Flash).

**Data sources:**
- Chat history: Fetched from MongoDB using `chat_bot_id` + `tenant_id`
  (TODO: exact collection/function TBD — team to confirm. Design with a pluggable
  `fetch_chat_history(chat_bot_id, tenant_id, limit) → list[ChatMessage]` function
  so the data source can be swapped without changing node logic.)
- File contents: Fetched from Redis using `file:{object_id}:content` keys
- User query: From `state["original_topic"]`

**Output:** `context_brief` — structured prose summary (~2000-3000 tokens) covering:
- What the conversation has been about (from chat history)
- What the uploaded files contain (key topics, not full text)
- What the user is asking for (research intent)

**Error handling:**
- Redis/DB fetch fails → log warning, build brief from what is available. Never fail for a cache miss.
- LLM call fails → return `status="failed"`, `error="context_builder: ..."`.

**State reads:** `original_topic`, `uploaded_files`, `chat_bot_id`, `tenant_id`
**State writes:** `context_brief`, `file_contents`

---

### Node 2: query_analyzer

**Purpose:** Read the context brief and decide: ask clarifying questions OR extract
full research parameters. One LLM call does both jobs (no separate clarifier node).

**Runs in:** Mode 1 and Mode 2.

**LLM Model:** Pro (reasoning task — poor analysis degrades the entire pipeline).

**Decision logic:**

Set `needs_clarification: true` if ANY of:
- Topic is fewer than 4 words and context_brief doesn't resolve the ambiguity
- Topic is generic/broad and no domain can be inferred from context
- Multiple plausible interpretations exist that context doesn't resolve
- No clear research objective can be determined

If `needs_clarification: true`:
- Return `clarification_questions` (max 3 specific questions)
- Return whatever partial parameters CAN already be inferred
- Increment `clarification_count` by 1

If `needs_clarification: false`:
- Return all research parameters: `refined_topic`, `depth_of_research`,
  `audience`, `objective`, `domain`, `recency_scope`, `source_scope`, `assumptions`

**On re-invocation (Mode 2):**
State now contains `clarification_answers` + `clarification_questions` from the
previous round. The analyzer reads both alongside the unchanged `context_brief`.
It should produce full parameters this time. It may ask again only if answers
were truly unhelpful AND `clarification_count < max`.

**Clarification count:** Tracks completed **rounds**, not individual answers.
Cap: `settings.loops.clarification_max` (default 3).

**State reads:** `context_brief`, `original_topic`, `clarification_answers`,
                `clarification_questions`, `clarification_count`
**State writes:** `needs_clarification`, `clarification_questions`, `clarification_count`,
                 `refined_topic`, `depth_of_research`, `audience`, `objective`,
                 `domain`, `recency_scope`, `source_scope`, `assumptions`, `status`

---

### Node 3: plan_creator

**Purpose:** Produce a structured ResearchPlan from the analyzed parameters.
Also generates a quality checklist for the execution graph.

**Runs in:** All three modes.

**LLM Model:** Pro (plan quality directly affects report quality).

**Two operating modes:**

**Fresh plan** (`plan_revision_count == 0`):
Uses refined_topic + all research parameters + context_brief to create a new plan.

**Revision** (`plan_revision_count > 0`):
Receives `plan_feedback` (user's rejection feedback) and the previous `plan`.
Makes targeted changes based on the feedback. Does NOT re-create from scratch.

**Output:**
- `plan` — ResearchPlan with title, summary, sections (stable UUIDs), search strategies
- `checklist` — 3-5 quality gates for the execution graph

**Section count guidance:**
- surface: 1-3 sections
- intermediate: 4-6 sections
- in-depth: 7-10 sections

**State reads:** `refined_topic`, `depth_of_research`, `audience`, `objective`,
               `domain`, `recency_scope`, `source_scope`, `assumptions`,
               `context_brief`, `plan` (on revision), `plan_feedback` (on revision),
               `plan_revision_count`
**State writes:** `plan`, `checklist`, `status`

---

## PlanningState Schema

```python
class PlanningState(TypedDict):

    # ── Identity (set at graph entry, never mutated) ───────────────────
    topic_id: str               # = job_id from API
    tenant_id: str              # multi-tenancy key — on every DB query
    user_id: str
    chat_bot_id: str            # used by context_builder to fetch chat history

    # ── Raw Inputs (set at graph entry, consumed ONLY by context_builder) ─
    original_topic: str                 # verbatim user query
    uploaded_files: list[UploadedFile]  # file metadata for Redis lookup

    # ── Context Builder Output ─────────────────────────────────────────
    context_brief: str                  # LLM-generated structured summary
    file_contents: dict[str, str]       # object_id → text (for execution graph later)

    # ── Clarification (managed by query_analyzer) ──────────────────────
    needs_clarification: bool
    clarification_questions: list[str]  # questions sent to user
    clarification_answers: list[str]    # answers received from user
    clarification_count: int            # completed ROUNDS (not answer count)

    # ── Research Parameters (produced by query_analyzer) ───────────────
    refined_topic: str
    depth_of_research: DepthLevel
    audience: str
    objective: str
    domain: str
    recency_scope: str
    source_scope: list[SourceType]
    assumptions: list[str]

    # ── Plan (produced by plan_creator) ────────────────────────────────
    plan: Optional[ResearchPlan]
    checklist: list[str]                # quality gates for execution graph
    plan_revision_count: int            # times user rejected + revised
    plan_feedback: Optional[str]        # user's rejection feedback (None on fresh)

    # ── Lifecycle ──────────────────────────────────────────────────────
    status: PlanningStatus
    error: Optional[str]
```

### Auxiliary Types

```python
DepthLevel = Literal["surface", "intermediate", "in-depth"]
SourceType = Literal["web", "arxiv", "content_lake", "files"]

PlanningStatus = Literal[
    "pending",
    "analyzing",
    "needs_clarification",
    "planning",
    "awaiting_approval",
    "failed",
]

class ChatMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str

class UploadedFile(TypedDict):
    object_id: str
    filename: str
    mime_type: str

class PlanSection(TypedDict):
    section_id: str
    title: str
    description: str
    search_strategy: str

class ResearchPlan(TypedDict):
    title: str
    summary: str
    sections: list[PlanSection]
    estimated_sources: int
```

### Fields Removed (vs current codebase)

| Removed Field        | Reason                                                        |
|----------------------|---------------------------------------------------------------|
| `chat_history`       | context_builder fetches internally, distilled into context_brief |
| `plan_approved`      | Human approves via `/run` endpoint, not in graph state         |
| `self_review_count`  | plan_reviewer node removed                                     |

### Fields Added

| New Field            | Produced by       | Consumed by                    |
|----------------------|-------------------|--------------------------------|
| `context_brief`      | context_builder   | query_analyzer, plan_creator   |
| `plan_feedback`      | API route (input) | plan_creator (on revision)     |

---

## Node ↔ State Field Matrix

| Field                    | context_builder | query_analyzer | plan_creator |
|--------------------------|:---:|:---:|:---:|
| topic_id                 | R   | —   | —   |
| tenant_id                | R   | —   | —   |
| chat_bot_id              | R   | —   | —   |
| original_topic           | R   | R   | —   |
| uploaded_files           | R   | —   | —   |
| context_brief            | W   | R   | R   |
| file_contents            | W   | —   | —   |
| needs_clarification      | —   | W   | —   |
| clarification_questions  | —   | W   | —   |
| clarification_answers    | —   | R   | —   |
| clarification_count      | —   | W   | —   |
| refined_topic            | —   | W   | R   |
| depth_of_research        | —   | W   | R   |
| audience                 | —   | W   | R   |
| objective                | —   | W   | R   |
| domain                   | —   | W   | R   |
| recency_scope            | —   | W   | R   |
| source_scope             | —   | W   | R   |
| assumptions              | —   | W   | R   |
| plan                     | —   | —   | W   |
| checklist                | —   | —   | W   |
| plan_revision_count      | —   | —   | R   |
| plan_feedback            | —   | —   | R   |
| status                   | —   | W   | W   |
| error                    | W   | W   | W   |

R = reads, W = writes, — = does not touch

---

## Loop Caps

| Loop                              | Max | Enforced by            |
|-----------------------------------|-----|------------------------|
| Clarification rounds              | 3   | Graph routing function |
| Plan revisions (user rejections)  | 3   | API route (HTTP 400)   |

---

## Settings Dependencies

```python
# New settings needed:

# In ModelConfig:
use_pro_for_context_building: bool = False    # Flash by default

# In LoopLimits:
plan_revision_max: int = 3                    # max user plan rejections

# Existing settings used:
settings.loops.clarification_max              # max clarification rounds (3)
settings.models.model_for("context_building")
settings.models.model_for("query_analysis")
settings.models.model_for("plan_creation")
settings.llm_timeout_seconds
```

---

## Error Handling

| Node            | Failure                   | Behavior                                         |
|-----------------|---------------------------|--------------------------------------------------|
| context_builder | Redis/DB fetch fails      | Degrade gracefully — build brief from what's available |
| context_builder | LLM call fails            | Fail the graph (`status="failed"`)               |
| query_analyzer  | LLM call fails            | Fail the graph                                   |
| query_analyzer  | Unparseable LLM response  | Fail the graph — do NOT guess parameters         |
| plan_creator    | LLM call fails            | Fail the graph                                   |
| plan_creator    | Unparseable LLM response  | Fail the graph — do NOT return a broken plan     |

All failures set: `status="failed"`, `error="<node_name>: <description>"`.

---

## What This Graph Does NOT Do

- **No plan self-review.** The human is the reviewer. plan_reviewer node is removed.
- **No checkpointer.** State is saved to MongoDB by the API route, not by LangGraph.
- **No streaming.** The graph completes before the API responds.
- **No direct DB writes from nodes.** Nodes are pure functions (state in, partial state out). Only the API route writes to MongoDB.
- **No execution.** Execution is a separate graph triggered by `POST /api/deepresearch/run` after the user approves the plan.




chatfriendly version :


Layer 1 — Chat LLM (already exists):
Give the existing chatbot LLM a tool called deep_research. When the user says something that needs research, the LLM decides to call it. The tool definition is simple:
Tool: deep_research
  topic: string       — the refined research query
  depth: surface | intermediate | in-depth
  audience: string
  source_types: [web, arxiv, content_lake, files]
The chat LLM fills these parameters from conversation context. It has already clarified the query through natural conversation. It knows the depth because the user said "I need something quick" or "give me everything." It knows the audience because the conversation makes it obvious.
Layer 2 — Research Backend (simplified):
The /plan endpoint receives one rich, pre-clarified request. Runs one node — plan_creator. One LLM call. Returns a structured plan with sections.
The chat LLM presents the plan to the user: "Here's what I'd research — 6 sections covering X, Y, Z. Should I proceed?"
User says "yes" → chat LLM calls execute_research(job_id) → your existing /run endpoint queues the job → worker executes → WebSocket streams progress. This part doesn't change at all.
User says "change section 3 to focus on regulations" → chat LLM calls deep_research again with an updated topic. No Mode 3 state management needed.
User says "nevermind" → the LLM just doesn't call the tool. Cancel for free.