# Deep Research Microservice — Technical Architecture Document

**Project:** Kadal.ai Deep Research Tool  
**Platform:** LearningMate Kadal AI Workbench  
**Author:** [Your Name]  
**Version:** 2.0  
**Date:** February 2026  
**Status:** Architecture Proposal — Ready for Review

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Overview](#2-system-overview)
3. [High-Level Architecture](#3-high-level-architecture)
4. [Component Breakdown](#4-component-breakdown)
5. [State Machine](#5-state-machine)
6. [Agent Orchestration — LangGraph](#6-agent-orchestration--langgraph)
7. [Multi-Model Strategy](#7-multi-model-strategy)
8. [Knowledge Fusion — Competitive Differentiator](#8-knowledge-fusion--competitive-differentiator)
9. [API Contract](#9-api-contract)
10. [Data Model — MongoDB Collections](#10-data-model--mongodb-collections)
11. [Worker Flow — EKS Job](#11-worker-flow--eks-job)
12. [Streaming Interface](#12-streaming-interface)
13. [Search & Tool Integration](#13-search--tool-integration)
14. [Observability Stack](#14-observability-stack)
15. [Caching Strategy — Redis](#15-caching-strategy--redis)
16. [Error Handling & Resilience](#16-error-handling--resilience)
17. [Security & Multi-Tenancy](#17-security--multi-tenancy)
18. [Project Structure](#18-project-structure)
19. [Deployment Architecture](#19-deployment-architecture)
20. [Phased Build Plan](#20-phased-build-plan)
21. [Open Questions for Team](#21-open-questions-for-team)

---

## 1. Executive Summary

### Objective

Introduce a **Deep Research Tool** into the Kadal platform as a set of backend APIs that integrate with the existing Chat v7 service. The tool enables users to conduct in-depth, multi-source research on any topic and receive a comprehensive, well-sourced HTML report.

### What It Does

1. User enables the Deep Research toggle in the existing chat
2. Submits a research topic (optionally with file attachments)
3. An AI agent analyzes the query, asks clarifying questions if needed, and generates a research plan
4. User reviews/edits/approves the plan
5. A background EKS worker executes the research — searching web, Arxiv, Content Lake, and uploaded files in parallel
6. Report streams to the user in real-time as HTML chunks via WebSocket/SSE
7. Final HTML report is persisted to S3 with metadata in MongoDB
8. User can revisit all historical reports

### Architecture Pattern

**Adaptive Supervisor with Parallel Research + Knowledge Fusion** — inspired by the best patterns from open-source deep research tools (LangChain Open Deep Research, Google Gemini Agent, Together AI), adapted to Kadal's unique advantages:

- **From LangChain ODR:** Supervisor-researcher parallel pattern with isolated context windows per section
- **From Google Gemini Agent:** Iterative reflection with knowledge gap detection after research
- **From Together AI:** Multi-model tiers (cheap model for simple tasks, expensive model for reasoning)
- **From Microsoft Enterprise DR:** Credibility critic + reflection critic for source quality
- **Kadal-Specific:** Knowledge Fusion across 4 source types (web + Content Lake + Arxiv + uploaded files), persistent source scoring that improves over time, chat history context for better query understanding

### Key Architectural Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| API Framework | **FastAPI** | Async-native, OpenAPI docs, existing team familiarity |
| Agent Orchestration | **LangGraph** | State machine with checkpointing, conditional routing, streaming, Send() API for parallelism |
| Agent Pattern | **Supervisor-Researcher** | Parallel section research with isolated context, supervisor reflection for gap detection |
| Database | **MongoDB** (existing) | Reuse existing connection, schema flexibility, existing collections |
| Caching | **Redis** (existing) | File content cache, job status, pub/sub for streaming relay |
| Background Jobs | **EKS Jobs** (existing pattern) | Isolated execution, K8s-native retry, resource limits |
| Report Storage | **AWS S3** | HTML reports, versioning, presigned URL access, CloudFront optional |
| Infra Observability | **OpenTelemetry** (open source) | Vendor-neutral tracing/metrics across FastAPI, Worker, MongoDB, Redis |
| LLM Observability | **Langfuse** (open source, self-hosted) | Prompt/response tracking, token/cost analytics, LLM quality evaluation |
| Primary LLM | **Gemini 2.5 Pro** | Strong reasoning, planning, report writing |
| Secondary LLM | **Gemini 2.5 Flash** | Cheap for summarization, compression, scoring — 40-60% cost reduction |
| Search Tools | **Tavily, SerperAPI, Arxiv, Content Lake** | MCP servers where applicable |

### Competitive Differentiators

| Differentiator | Why No Public Tool Has This |
|---------------|----------------------------|
| **Knowledge Fusion** | Combines web + Content Lake + Arxiv + uploaded files. ChatGPT/Perplexity only search the web |
| **Source Intelligence** | Persistent source scoring in MongoDB. System gets smarter with every job per tenant |
| **Chat Context** | Uses prior conversation to understand intent without asking clarifications |
| **Internal Knowledge** | Content Lake gives access to organizational documents, courses, videos that are not on the web |

### In Scope

- New Deep Research APIs (`/plan`, `/run`, `/status`, `/reports`)
- New background job type (`DEEP_RESEARCH`) and EKS worker flow
- Data models for jobs and reports (3+ MongoDB collections)
- UI integration points (technical level, not pixel spec)
- LangGraph agent orchestration with checkpointing and parallel execution
- Dual observability (OpenTelemetry + Langfuse, both open source self-hosted)
- Multi-model tier strategy for cost optimization

### Out of Scope (Phase 2+)

- Visualization generation based on report
- Caching of reports and fetching from semantic cache instead of regenerating

---

## 2. System Overview

### User Journey (End-to-End)

```
┌─────────────────────────────────────────────────────────────────────┐
│                         USER JOURNEY                                │
│                                                                     │
│  1. User is in Kadal Chat → enables "Deep Research" toggle          │
│                    │                                                │
│  2. Types research topic → optionally attaches files                │
│                    │                                                │
│  3. POST /plan ────┘                                                │
│        │                                                            │
│        ├── Context Builder: fetches chat history + file content     │
│        ├── Query Analyzer: identifies depth, audience, domain       │
│        ├── If unclear → asks clarification questions (max 3 loops) │
│        ├── Plan Creator: generates section-by-section plan          │
│        └── Plan Self-Review: validates coverage → returns to user  │
│                    │                                                │
│  4. User reviews plan                                               │
│        ├── Edits/refines → plan regenerated (max 3 loops)          │
│        └── Approves → POST /run                                     │
│                    │                                                │
│  5. Background EKS Job starts                                       │
│        ├── Supervisor spawns parallel section researchers           │
│        ├── Each researcher: search → verify → compress → score     │
│        ├── Supervisor reflects: knowledge gaps? → spawn more if so │
│        ├── Knowledge Fusion: combine web + internal + academic     │
│        ├── Report Writer: generates HTML with citations            │
│        ├── Report Reviewer: validates against checklist            │
│        ├── Streams progress + content chunks via WebSocket         │
│        └── Export: S3 upload + MongoDB updates                     │
│                    │                                                │
│  6. Report complete                                                 │
│        ├── HTML saved to S3                                         │
│        ├── Metadata + citations saved to MongoDB                    │
│        └── User can view, close, or start new research              │
│                    │                                                │
│  7. Historical access                                               │
│        └── GET /reports → all past reports for this user            │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              KADAL PLATFORM                              │
│                                                                          │
│  ┌─────────────────────┐         ┌──────────────────────────────────┐   │
│  │  Chat Service (v7)  │         │  Deep Research Service (NEW)     │   │
│  │  (existing)         │────────→│  FastAPI Microservice             │   │
│  │                     │  HTTP   │                                    │   │
│  │  - Chat UI & Tools  │         │  POST /api/deepresearch/plan      │   │
│  │  - Attachments      │         │  POST /api/deepresearch/run       │   │
│  │  - Web Search       │         │  GET  /api/deepresearch/status    │   │
│  │  - Kadal Search     │         │  GET  /api/deepresearch/reports   │   │
│  │  - Deep Research    │         │  WS   /ws/deep-research/{job_id}  │   │
│  │    toggle (tool)    │         └────┬───────────┬──────────────────┘   │
│  └─────────────────────┘              │           │                      │
│                                       │           │                      │
│          ┌────────────────────────────┘           │                      │
│          │                                        │                      │
│          ▼                                        ▼                      │
│  ┌───────────────┐                    ┌───────────────────────┐         │
│  │   MongoDB      │                    │  Redis (existing)     │         │
│  │   (existing)   │                    │                       │         │
│  │                │                    │  - File content cache │         │
│  │  Collections:  │                    │  - Job status cache   │         │
│  │  - background_ │                    │  - Pub/Sub relay for  │         │
│  │    jobs        │                    │    WebSocket streaming│         │
│  │  - deep_       │                    │  - Source scores cache│         │
│  │    research_   │                    └───────────────────────┘         │
│  │    jobs (NEW)  │                                                      │
│  │  - deep_       │                    ┌───────────────────────┐         │
│  │    research_   │                    │  AWS S3               │         │
│  │    reports     │                    │  Bucket:              │         │
│  │    (NEW)       │                    │  kadal-deep-research- │         │
│  │  - source_     │                    │  reports              │         │
│  │    scores(NEW) │                    │  Key pattern:         │         │
│  │  - langgraph_  │                    │  deep-research/       │         │
│  │    checkpoints │                    │  {tenant_id}/         │         │
│  │    (NEW)       │                    │  {user_id}/           │         │
│  │  - chat_history│                    │  {report_id}.html     │         │
│  │  - agent_chat_ │                    └───────────────────────┘         │
│  │    history     │                                                      │
│  └───────┬───────┘                    ┌───────────────────────┐         │
│          │                             │  Observability        │         │
│          ▼                             │  (all open source,    │         │
│  ┌───────────────────────────┐        │   self-hosted)        │         │
│  │  EKS Job Scheduler        │        │                       │         │
│  │  (existing pattern)       │        │  OpenTelemetry:       │         │
│  │                           │        │  - Traces → Tempo     │         │
│  │  Polls background_jobs    │        │  - Metrics → Prom     │         │
│  │  where type=DEEP_RESEARCH │        │  - Logs → Loki        │         │
│  │  and status=QUEUED        │        │  - Dashboard: Grafana │         │
│  │                           │        │                       │         │
│  │  Spawns EKS Job:          │        │  Langfuse (self-host):│         │
│  │  deep-research-worker     │        │  - LLM traces         │         │
│  └───────────┬───────────────┘        │  - Token/cost tracking│         │
│              │                         │  - Prompt management  │         │
│              ▼                         └───────────────────────┘         │
│  ┌───────────────────────────┐                                           │
│  │  Deep Research Worker     │        ┌───────────────────────┐         │
│  │  (EKS Job — runs once)    │───────→│  External Services    │         │
│  │                           │  MCP   │                       │         │
│  │  LangGraph Execution:     │        │  - Tavily Search      │         │
│  │  Supervisor-Researcher    │        │  - SerperAPI          │         │
│  │  Pattern with parallel    │        │  - Arxiv API          │         │
│  │  section research         │        │  - Content Lake API   │         │
│  └───────────────────────────┘        │  - LLM (Gemini Pro)   │         │
│                                        │  - LLM (Gemini Flash) │         │
│                                        └───────────────────────┘         │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Component Breakdown

### 4.1 Chat Service (Existing — No Modifications)

- Hosts the chat UI and tools (attachments, web search, Kadal search, context, clear history, download)
- When user enables Deep Research toggle, Chat Service calls Deep Research APIs
- Agent library call: `GET /api/agents?scope=specialized` → returns `deep_research_gemini` if enabled
- Chat History stored in existing `chat_history` collection with `topic_id` + `session_id`
- Agent Chat History stored in `agent_chat_history` with `topic_id` + `chatbot_id`

### 4.2 Deep Research Service (New — FastAPI Microservice)

**Responsibilities:**
- Receive research topics from Chat Service
- Run LangGraph planning pipeline (synchronous)
- Create background jobs for research execution
- Serve job status and historical reports
- Relay WebSocket streaming from Redis pub/sub to connected clients

**Does NOT:**
- Contain any frontend logic (all checks are API-side)
- Execute the research itself (that's the Worker's job)
- Directly call LLM APIs for research execution (only for planning)

### 4.3 Deep Research Worker (New — EKS Job)

A containerized Python process that runs as a Kubernetes Job on EKS. One job = one pod = one research execution.

**Responsibilities:**
- Load job context from MongoDB
- Execute LangGraph research pipeline (Supervisor-Researcher pattern)
- Spawn parallel section researchers via Send() API
- Run supervisor reflection for knowledge gap detection
- Fuse knowledge across web + Content Lake + Arxiv + files
- Generate HTML report with inline citations
- Upload to S3, update 3 MongoDB collections
- Stream progress + content via Redis pub/sub

### 4.4 EKS Job Scheduler (Existing Pattern)

Uses existing `background_jobs` collection. The scheduler:
1. Polls `background_jobs` for `type=DEEP_RESEARCH`, `status=QUEUED`
2. Spawns an EKS Job named `deep-research-worker`
3. Passes `job_id` as environment variable to the worker container

---

## 5. State Machine

### 5.1 All States

```python
class ResearchState(str, Enum):
    INITIALIZATION       = "initialization"
    QUERY_UNDERSTANDING  = "query_understanding"
    CLARIFICATION_NEEDED = "clarification_needed"
    PLANNING             = "planning"
    PLANS_READY          = "plans_ready"
    PLAN_REFINEMENT      = "plan_refinement"
    RESEARCH_QUEUED      = "research_queued"
    RESEARCH             = "research"
    EDITING_QUEUED       = "editing_queued"
    EDITING              = "editing"
    EXPORT               = "export"
    COMPLETED            = "completed"
    FAILED               = "failed"
```

### 5.2 State Transition Diagram

```
                    ┌──────────────────┐
                    │  INITIALIZATION  │
                    └────────┬─────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │     QUERY_UNDERSTANDING      │◄──────────────┐
              └──────────────┬───────────────┘               │
                             │                                │
                    ┌────────┴────────┐                      │
                    │                  │                      │
                    ▼                  ▼                      │
          (query is clear)    (query is vague)               │
                    │                  │                      │
                    │                  ▼                      │
                    │    ┌─────────────────────┐             │
                    │    │ CLARIFICATION_NEEDED│─────────────┘
                    │    └─────────────────────┘  (loop max 3)
                    │
                    ▼
              ┌──────────────┐
              │   PLANNING   │◄──────────────────────────────┐
              └──────┬───────┘                               │
                     ▼                                        │
              ┌──────────────┐                               │
              │  PLANS_READY │──→ User reviews plan          │
              └──────┬───────┘                               │
                     │                                        │
            ┌────────┴─────────┐                             │
            ▼                   ▼                             │
     (user approves)    (user edits)                         │
            │            ┌─────────────────┐                 │
            │            │ PLAN_REFINEMENT │─────────────────┘
            │            └─────────────────┘   (loop max 3)
            ▼
     ┌───────────────┐
     │RESEARCH_QUEUED│──→ background_jobs created
     └───────┬───────┘
             │  (EKS scheduler picks up)
             ▼
     ┌───────────────┐
     │   RESEARCH    │──→ Supervisor + parallel section researchers
     └───────┬───────┘
             ▼
     ┌───────────────┐
     │EDITING_QUEUED │
     └───────┬───────┘
             ▼
     ┌───────────────┐
     │    EDITING    │──→ Knowledge Fusion + Report Writing + Review
     └───────┬───────┘
             ▼
     ┌───────────────┐
     │    EXPORT     │──→ HTML → S3, metadata → MongoDB
     └───────┬───────┘
             ▼
     ┌───────────────┐
     │   COMPLETED   │──→ progress=100, all 3 collections updated
     └───────────────┘

     Any state ──→ FAILED (on unrecoverable error)
```

### 5.3 Loop Limits

| Loop | Max Iterations | On Limit |
|------|---------------|----------|
| `query_understanding` ↔ `clarification_needed` | 3 | Proceed with best understanding |
| `planning` ↔ `plans_ready` ↔ `plan_refinement` | 3 | Present final plan |
| Search depth (surface → intermediate → in-depth) | 3 levels | Use available results |
| Supervisor reflection rounds | 2 | Proceed to report with available data |
| Report revision (reviewer → writer) | 1 | Accept current report |

---

## 6. Agent Orchestration — LangGraph

### 6.1 Architecture Pattern: Adaptive Supervisor-Researcher

Based on analysis of top open-source deep research implementations (LangChain ODR ranked #6 on Deep Research Bench, Google Gemini Agent, Together AI, Microsoft Enterprise DR), this architecture uses the **Supervisor-Researcher** pattern with these key innovations:

- **Parallel section research** via LangGraph Send() API — each section is researched in an isolated context window by a dedicated sub-graph
- **Supervisor reflection** — after parallel research, the supervisor checks for knowledge gaps and can spawn additional targeted research
- **Context compression** — raw search results (50K+ tokens) are compressed to essentials (3-5K tokens) before passing downstream. This is critical and missing from the original spec
- **Knowledge Fusion** — a dedicated node that combines web + Content Lake + Arxiv + file content with conflict resolution. This is Kadal's unique differentiator
- **Multi-model tiers** — Gemini Flash for simple tasks (search summarization, compression, scoring), Gemini Pro for reasoning (analysis, planning, report writing). Reduces LLM cost by 40-60%

### 6.2 Two Separate Graphs

**Graph 1: Planning** — runs synchronously inside FastAPI during `/plan`  
**Graph 2: Execution** — runs asynchronously inside EKS Worker during `/run`

Separated by the human-in-the-loop approval boundary.

### 6.3 Planning Graph (Synchronous)

```
┌─────────────────────────────────────────────────────┐
│  PLANNING GRAPH — runs in FastAPI (/plan endpoint)  │
│                                                      │
│  ┌─────────────────────┐                            │
│  │  CONTEXT BUILDER     │  ← NOT an LLM call        │
│  │  (pure code)         │     - Fetch chat history   │
│  │                      │       from MongoDB         │
│  │                      │     - Fetch file content   │
│  │                      │       from Redis cache     │
│  │                      │     - Build context blob   │
│  └──────────┬──────────┘                            │
│             │                                        │
│             ▼                                        │
│  ┌─────────────────────┐                            │
│  │  QUERY ANALYZER      │  ← 1 LLM call (Pro)       │
│  │                      │                            │
│  │  Analyzes:           │                            │
│  │  - Topic intent      │                            │
│  │  - Chat history      │  Uses chat context to      │
│  │    context           │  infer audience, domain,   │
│  │  - File content      │  objective WITHOUT asking  │
│  │    summary           │  the user when possible    │
│  │                      │                            │
│  │  Outputs:            │                            │
│  │  - needs_clarify     │                            │
│  │  - clarify_questions │                            │
│  │  - depth             │                            │
│  │  - audience          │                            │
│  │  - objective         │                            │
│  │  - domain            │                            │
│  │  - recency_scope     │                            │
│  │  - source_scope[]    │                            │
│  │  - assumptions[]     │                            │
│  └──────────┬──────────┘                            │
│             │                                        │
│        ┌────┴────┐                                  │
│        │         │                                  │
│    (clear)   (unclear)                              │
│        │    ┌────▼─────────────┐                    │
│        │    │ Return questions  │                    │
│        │    │ + assumptions     │                    │
│        │    │ to user           │→ user answers     │
│        │    └──────────────────┘  → re-call /plan   │
│        │         (max 3 loops)                      │
│        │                                             │
│        ▼                                             │
│  ┌─────────────────────┐                            │
│  │  PLAN CREATOR        │  ← 1 LLM call (Pro)       │
│  │                      │                            │
│  │  Creates:            │                            │
│  │  - sections[] with   │                            │
│  │    titles, desc,     │                            │
│  │    search_strategy   │                            │
│  │  - checklist[] for   │                            │
│  │    execution agent   │                            │
│  └──────────┬──────────┘                            │
│             │                                        │
│             ▼                                        │
│  ┌─────────────────────┐                            │
│  │  PLAN SELF-REVIEW    │  ← 1 LLM call (Pro)       │
│  │                      │                            │
│  │  Checks:             │                            │
│  │  - Does plan cover   │                            │
│  │    all criteria?     │                            │
│  │  - Any gaps?         │                            │
│  │  - Sections balanced?│                            │
│  │                      │                            │
│  │  If gaps → revise    │                            │
│  │  (max 2 self-reviews)│                            │
│  └──────────┬──────────┘                            │
│             │                                        │
│          RETURN plan to user                        │
│          (status: plans_ready)                      │
└─────────────────────────────────────────────────────┘
```

**Planning Graph State:**

```python
class PlanningState(TypedDict):
    # Input
    topic_id: str
    tenant_id: str
    user_id: str
    chat_bot_id: str

    # Context (built by Context Builder)
    chat_history: list[dict]
    uploaded_files: list[dict]       # [{object_id, file_name, file_url}]
    file_contents: list[str]         # fetched from Redis cache

    # Query Analysis
    original_topic: str
    refined_topic: str
    needs_clarification: bool
    clarification_questions: list[str]
    clarification_answers: list[dict]
    clarification_count: int         # track loop iterations (max 3)

    # Query Analyzer Outputs
    depth_of_research: str           # surface | intermediate | in-depth
    audience: str
    objective: str                   # business | academic | industrial
    domain: str
    recency_scope: str
    source_scope: list[str]          # ["tavily", "serperAPI", "content_lake", "arxiv"]
    assumptions: list[str]           # assumptions made from chat context

    # Plan
    plan: dict | None
    plan_approved: bool
    plan_revision_count: int         # track loop iterations (max 3)
    checklist: list[dict]            # to-do for executor agent
    self_review_count: int           # max 2

    # Control
    status: str
    error: str | None
```

### 6.4 Execution Graph (Async — EKS Worker)

```
┌──────────────────────────────────────────────────────────────────┐
│  EXECUTION GRAPH — runs in EKS Worker                            │
│                                                                   │
│  ┌────────────────────────────────────────┐                      │
│  │  SUPERVISOR                             │                      │
│  │                                         │                      │
│  │  Reads plan sections                    │                      │
│  │  Determines execution strategy:         │                      │
│  │  - Independent sections → PARALLEL      │                      │
│  │  - Dependent sections → SEQUENTIAL      │                      │
│  │                                         │                      │
│  │  Configures per depth_of_research:      │                      │
│  │  - surface:  1 search iter, 3 sources   │                      │
│  │  - mid:      2 search iter, 5 sources   │                      │
│  │  - in-depth: 3 search iter, 10 sources  │                      │
│  │                                         │                      │
│  │  Spawns section researchers via Send()  │                      │
│  └──────────────────┬─────────────────────┘                      │
│                     │                                             │
│       ┌─────────────┼─────────────┐                              │
│       │             │             │                               │
│       ▼             ▼             ▼                               │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐                         │
│  │Section 1 │ │Section 2 │ │Section N │  ← PARALLEL             │
│  │Researcher│ │Researcher│ │Researcher│    via Send() API        │
│  │(sub-graph│ │(sub-graph│ │(sub-graph│    each has ISOLATED     │
│  │ isolated │ │ isolated │ │ isolated │    context window        │
│  │ context) │ │ context) │ │ context) │                          │
│  │          │ │          │ │          │                          │
│  │ SEARCH   │ │ SEARCH   │ │ SEARCH   │  1 LLM call (Flash)    │
│  │ ┌──────┐ │ │ ┌──────┐ │ │ ┌──────┐ │  Generate search        │
│  │ │Query │ │ │ │Query │ │ │ │Query │ │  queries for section    │
│  │ │Gen   │ │ │ │Gen   │ │ │ │Gen   │ │                          │
│  │ └──┬───┘ │ │ └──┬───┘ │ │ └──┬───┘ │                          │
│  │    │     │ │    │     │ │    │     │                          │
│  │ ┌──▼───┐ │ │ ┌──▼───┐ │ │ ┌──▼───┐ │  Parallel across        │
│  │ │Search│ │ │ │Search│ │ │ │Search│ │  enabled sources:       │
│  │ │Tools │ │ │ │Tools │ │ │ │Tools │ │  Tavily | Serper |      │
│  │ │(par) │ │ │ │(par) │ │ │ │(par) │ │  Arxiv | Content Lake | │
│  │ └──┬───┘ │ │ └──┬───┘ │ │ └──┬───┘ │  Files                  │
│  │    │     │ │    │     │ │    │     │                          │
│  │ ┌──▼───┐ │ │ ┌──▼───┐ │ │ ┌──▼───┐ │  Check URL authenticity │
│  │ │Verify│ │ │ │Verify│ │ │ │Verify│ │  (web sources only,     │
│  │ │URLs  │ │ │ │URLs  │ │ │ │URLs  │ │   skip arxiv/CL)        │
│  │ └──┬───┘ │ │ └──┬───┘ │ │ └──┬───┘ │                          │
│  │    │     │ │    │     │ │    │     │                          │
│  │ ┌──▼────┐│ │ ┌──▼────┐│ │ ┌──▼────┐│  ★ CRITICAL NODE ★      │
│  │ │COMPRES││ │ │COMPRES││ │ │COMPRES││  1 LLM call (Flash)     │
│  │ │S      ││ │ │S      ││ │ │S      ││  50K tokens → 3-5K     │
│  │ │findings│ │ │findings│ │ │findings│  Keeps only relevant    │
│  │ └──┬────┘│ │ └──┬────┘│ │ └──┬────┘│  facts + citations     │
│  │    │     │ │    │     │ │    │     │                          │
│  │ ┌──▼───┐ │ │ ┌──▼───┐ │ │ ┌──▼───┐ │  1 LLM call (Flash)    │
│  │ │Score │ │ │ │Score │ │ │ │Score │ │  Score each source:     │
│  │ │Srcs  │ │ │ │Srcs  │ │ │ │Srcs  │ │  authenticity, recency, │
│  │ └──┬───┘ │ │ └──┬───┘ │ │ └──┬───┘ │  relevancy. Check past  │
│  │    │     │ │    │     │ │    │     │  scores from MongoDB    │
│  │    │     │ │    │     │ │    │     │                          │
│  │  STREAM  │ │  STREAM  │ │  STREAM  │  Emit progress events   │
│  │  progress│ │  progress│ │  progress│  via Redis pub/sub      │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘                         │
│       │             │             │                               │
│       └─────────────┼─────────────┘                              │
│                     │                                             │
│  ┌──────────────────▼────────────────────────┐                   │
│  │  SUPERVISOR REFLECTION                     │ 1 LLM call (Pro) │
│  │                                            │                   │
│  │  Input: all compressed section findings    │                   │
│  │  + original checklist                      │                   │
│  │                                            │                   │
│  │  Asks: "Do I have enough data? Are there   │                   │
│  │  knowledge gaps? Uncovered checklist items?"│                   │
│  │                                            │                   │
│  │  If gaps → generate targeted queries       │                   │
│  │         → spawn 1-2 more researchers       │                   │
│  │         → max 2 reflection rounds          │                   │
│  │                                            │                   │
│  │  If sufficient → proceed                   │                   │
│  └──────────────────┬────────────────────────┘                   │
│                     │                                             │
│  ┌──────────────────▼────────────────────────┐                   │
│  │  KNOWLEDGE FUSION                          │ 1 LLM call (Pro) │
│  │  (Kadal's unique differentiator)           │                   │
│  │                                            │                   │
│  │  Combines findings from:                   │                   │
│  │  - Web research (Tavily, Serper)           │                   │
│  │  - Content Lake (internal docs)            │                   │
│  │  - Arxiv (academic papers)                 │                   │
│  │  - Uploaded files (user context)           │                   │
│  │                                            │                   │
│  │  Conflict resolution priority:             │                   │
│  │  1. User's uploaded files (most specific)  │                   │
│  │  2. Content Lake (verified internal)       │                   │
│  │  3. Academic papers (peer-reviewed)        │                   │
│  │  4. Web results (broadest, least verified) │                   │
│  │                                            │                   │
│  │  Outputs: fused_knowledge with source tags │                   │
│  └──────────────────┬────────────────────────┘                   │
│                     │                                             │
│  ┌──────────────────▼────────────────────────┐                   │
│  │  REPORT WRITER                             │ 1 LLM call (Pro) │
│  │                                            │                   │
│  │  Input: fused knowledge + checklist        │                   │
│  │  Output: HTML report with inline citations │                   │
│  │                                            │                   │
│  │  Streams html_chunks via Redis pub/sub     │                   │
│  │  as it generates each section              │                   │
│  └──────────────────┬────────────────────────┘                   │
│                     │                                             │
│  ┌──────────────────▼────────────────────────┐                   │
│  │  REPORT REVIEWER                           │ 1 LLM call (Pro) │
│  │                                            │                   │
│  │  Checks against:                           │                   │
│  │  - Original checklist                      │                   │
│  │  - Topic coverage completeness             │                   │
│  │  - Citation presence for all claims        │                   │
│  │                                            │                   │
│  │  If major issues → back to writer (max 1)  │                   │
│  │  If acceptable → proceed                   │                   │
│  └──────────────────┬────────────────────────┘                   │
│                     │                                             │
│  ┌──────────────────▼────────────────────────┐                   │
│  │  EXPORT                                    │ No LLM call      │
│  │                                            │                   │
│  │  1. Upload HTML to S3                      │                   │
│  │  2. Save source scores to MongoDB          │                   │
│  │  3. Update deep_research_reports           │                   │
│  │  4. Update deep_research_jobs (progress=100│)                  │
│  │  5. Update background_jobs (COMPLETED)     │                   │
│  │  6. Stream "completed" event               │                   │
│  └───────────────────────────────────────────┘                   │
└──────────────────────────────────────────────────────────────────┘
```

**Execution Graph State:**

```python
class ExecutionState(TypedDict):
    # Job Context
    job_id: str
    report_id: str
    tenant_id: str
    user_id: str
    topic: str
    refined_topic: str
    llm_model: str

    # Plan
    plan: dict
    checklist: list[dict]
    sections: list[dict]
    current_section_index: int
    total_sections: int

    # Attachments & Tools
    attachments: list[dict]
    file_contents: list[str]
    tools_enabled: dict              # {content_lake: true/false, ...}

    # Depth Configuration (set by supervisor based on query analysis)
    max_search_iterations: int       # 1 (surface) | 2 (mid) | 3 (in-depth)
    max_sources_per_section: int     # 3 | 5 | 10

    # Research Results (accumulated via reducer)
    section_results: Annotated[list[dict], operator.add]
    compressed_findings: Annotated[list[dict], operator.add]
    citations: Annotated[list[dict], operator.add]
    source_scores: Annotated[list[dict], operator.add]

    # Supervisor
    reflection_count: int            # max 2
    knowledge_gaps: list[str]

    # Fusion
    fused_knowledge: str

    # Report
    report_html: str
    review_feedback: str
    revision_count: int              # max 1
    s3_key: str

    # Progress
    progress: int                    # 0-100
    status: str
    error: str | None

    # Observability
    otel_trace_id: str
```

### 6.5 Section Researcher Sub-Graph

Each section researcher runs as an isolated sub-graph spawned via `Send()`:

```python
class SectionResearchState(TypedDict):
    # Input from supervisor
    section_id: str
    section_title: str
    section_description: str
    search_strategy: str             # which sources to use
    max_search_iterations: int
    max_sources: int
    file_contents: list[str]         # shared attachment content

    # Search
    search_queries: list[str]
    raw_search_results: list[dict]   # raw from all sources
    search_iteration: int

    # Processing
    verified_urls: list[dict]
    compressed_findings: str         # ★ CRITICAL — compressed from raw
    source_scores: list[dict]
    citations: list[dict]

    # Output (returned to supervisor)
    section_findings: str
    section_citations: list[dict]
    section_source_scores: list[dict]
```

### 6.6 Checkpointing (Crash Recovery)

```python
from langgraph.checkpoint.mongodb import AsyncMongoDBSaver

checkpointer = AsyncMongoDBSaver(
    mongo_client,
    db_name="kadal_platform",
    collection_name="langgraph_checkpoints"
)

execution_graph = execution_graph_builder.compile(checkpointer=checkpointer)

# thread_id = job_id → unique checkpoint per job
config = {"configurable": {"thread_id": job_id}}
final_state = await execution_graph.ainvoke(state, config)
```

On crash and retry: worker loads checkpoint from MongoDB, resumes from last completed node. Completed sections are NOT re-executed. LLM tokens and search API calls are NOT wasted.

---

## 7. Multi-Model Strategy

### 7.1 Why Different Models For Different Tasks

Following the pattern used by LangChain ODR (4 models) and Together AI (4 models), using a cheaper model for simple tasks dramatically reduces cost without reducing quality.

### 7.2 Model Assignment

| Task | Model | Why | Approx Tokens |
|------|-------|-----|---------------|
| Query analysis + clarification | Gemini 2.5 Pro | Needs strong reasoning about user intent | ~2K |
| Plan creation | Gemini 2.5 Pro | Needs structure + domain understanding | ~3K |
| Plan self-review | Gemini 2.5 Pro | Needs judgment about coverage gaps | ~2K |
| Search query generation | Gemini 2.5 Flash | Simple task — just reformulate for search | ~500 |
| Source summarization/compression | Gemini 2.5 Flash | Long input, extract key facts only | ~2K |
| Source scoring | Gemini 2.5 Flash | Factual checking against criteria | ~1K |
| Supervisor reflection | Gemini 2.5 Pro | Needs judgment about knowledge gaps | ~3K |
| Knowledge fusion | Gemini 2.5 Pro | Complex reasoning — conflict resolution | ~4K |
| Report writing | Gemini 2.5 Pro | Needs quality writing + structure | ~8K |
| Report review | Gemini 2.5 Pro | Needs judgment about completeness | ~3K |

### 7.3 Cost Estimate Per Job (5-Section Research)

```
PLANNING: 3 Pro calls × ~2.5K tokens avg    = ~7.5K Pro tokens
EXECUTION:
  Per section: 3 Flash calls × ~1.2K avg     = ~3.5K Flash tokens
  5 sections parallel                         = ~17.5K Flash tokens
  Post-research: 4 Pro calls × ~4.5K avg     = ~18K Pro tokens

TOTAL PER JOB:
  Pro:   7 calls, ~25.5K tokens    ≈ $0.15-0.25
  Flash: 15 calls, ~17.5K tokens   ≈ $0.01-0.02
  TOTAL                            ≈ $0.17-0.27 per research job

COMPARISON (all Pro, 9 sequential agents):   ≈ $1.00-2.00 per job
SAVINGS: 5-8x cheaper with multi-model strategy
```

---

## 8. Knowledge Fusion — Competitive Differentiator

### 8.1 Why This Matters

Every public deep research tool (ChatGPT Deep Research, Perplexity, Gemini Deep Research) only searches the **public web**. Kadal's system searches 4 sources:

```
┌───────────────────────────────────────────────────┐
│                 KNOWLEDGE SOURCES                  │
│                                                    │
│  ┌──────────┐  ┌──────────┐  ┌────────┐  ┌─────┐ │
│  │   Web     │  │ Content  │  │ Arxiv  │  │Files│ │
│  │ (Tavily,  │  │  Lake    │  │        │  │     │ │
│  │  Serper)  │  │(internal)│  │(papers)│  │(user│ │
│  │          │  │          │  │        │  │ ctx)│ │
│  │ Public   │  │ YOUR org │  │Academic│  │User │ │
│  │ internet │  │ docs,    │  │research│  │spec │ │
│  │ articles │  │ courses, │  │ peer-  │  │docs │ │
│  │ blogs    │  │ videos   │  │reviewed│  │     │ │
│  └────┬─────┘  └────┬─────┘  └───┬────┘  └──┬──┘ │
│       │              │            │           │    │
│       └──────────────┼────────────┼───────────┘    │
│                      │            │                 │
│                      ▼            │                 │
│           ┌─────────────────────┐                  │
│           │  KNOWLEDGE FUSION   │                  │
│           │  (1 LLM call, Pro)  │                  │
│           │                     │                  │
│           │  Resolves conflicts │                  │
│           │  Tags source types  │                  │
│           │  Prioritizes:       │                  │
│           │  Files > CL >       │                  │
│           │  Arxiv > Web        │                  │
│           └─────────────────────┘                  │
└───────────────────────────────────────────────────┘
```

### 8.2 Fusion Prompt Strategy

```python
FUSION_SYSTEM_PROMPT = """You are synthesizing research findings from multiple source types 
for an enterprise research report.

SOURCE PRIORITY (when sources conflict):
1. User's uploaded files — most specific to their need, highest trust
2. Organization's Content Lake — verified internal knowledge
3. Academic papers from Arxiv — peer-reviewed, high credibility
4. Web search results — broadest coverage but least verified

RULES:
- If internal knowledge CONFIRMS web knowledge → high confidence claim
- If internal knowledge CONTRADICTS web → flag the conflict, prefer internal,
  note the discrepancy
- If ONLY web sources → mark as "external source only"
- If academic paper supports → cite as "peer-reviewed"
- If uploaded file provides context → prioritize user's specific framing
- NEVER silently drop a source — tag every claim with its source type

OUTPUT FORMAT:
For each finding, tag with: [web], [internal], [academic], [file], or [multi-source]
"""
```

### 8.3 Source Intelligence (Gets Smarter Over Time)

```
Job #1 for TENANT_X:
  → Tavily returns example.com → score: unknown → verify → score: 0.7
  → Save to source_scores collection

Job #5 for TENANT_X:
  → example.com appears again
  → Check source_scores: "seen 4 times, avg score 0.75"
  → Fast-track: use cached score, skip deep verification

Job #100 for TENANT_X:
  → 500+ domains scored
  → Pre-filter search results by domain reliability
  → Research quality improves WITHOUT code changes
```

---

## 9. API Contract

### 9.1 POST `/api/deepresearch/plan`

**Purpose:** Analyze research topic, clarify if needed, generate research plan.

**Request:**
```json
{
  "topic_id": "TOPIC-2025-0001",
  "tenant_id": "TENANT_X",
  "user_id": "user_123",
  "chat_bot_id": "UUID",
  "topic": "Compare Microsoft and Google AI strategies for enterprise SaaS.",
  "attachments": [
    { "source": "repository", "object_id": "OBJ123", "file_name": "report.pdf", "file_url": "https://..." },
    { "source": "gdrive", "file_id": "gdrive_abc" }
  ],
  "tools": {
    "content_lake": true
  },
  "llm_model": "gemini-2.5-pro",
  "clarification_answers": []
}
```

**Response (plan generated):**
```json
{
  "job_id": "DR-2025-0001",
  "topic_id": "TOPIC-2025-0001",
  "status": "plans_ready",
  "refined_topic": "Compare Microsoft and Google AI strategies for enterprise SaaS",
  "analysis": {
    "depth_of_research": "in-depth",
    "audience": "business executives",
    "objective": "business",
    "domain": "enterprise technology",
    "recency_scope": "2023-2026",
    "source_scope": ["tavily", "serperAPI", "content_lake"],
    "assumptions": [
      "Focus on B2B enterprise SaaS based on chat context about CRM tools",
      "Budget-sensitive comparison based on earlier pricing discussion"
    ]
  },
  "plan": {
    "plan_id": "PLAN-abc123",
    "sections": [
      {
        "id": "sec1",
        "title": "1. Market Overview",
        "description": "Current state of enterprise AI SaaS market",
        "search_strategy": ["tavily", "serperAPI"]
      },
      {
        "id": "sec2",
        "title": "2. Microsoft AI Strategy",
        "description": "Copilot ecosystem, Azure AI, enterprise integrations",
        "search_strategy": ["tavily", "serperAPI", "content_lake"]
      }
    ]
  },
  "checklist": [
    "Cover both companies 2024-2025 announcements",
    "Include pricing comparison",
    "Cite at least 3 primary sources per section",
    "Address enterprise adoption metrics"
  ]
}
```

**Response (clarification needed):**
```json
{
  "job_id": "DR-2025-0001",
  "topic_id": "TOPIC-2025-0001",
  "status": "clarification_needed",
  "assumptions": [
    "Assuming B2B enterprise focus based on chat context",
    "Assuming business audience"
  ],
  "clarification_questions": [
    "Should the research focus on a particular industry vertical?",
    "What time range — last 1 year or broader?"
  ]
}
```

### 9.2 POST `/api/deepresearch/run`

**Request:**
```json
{
  "job_id": "DR-2025-0001",
  "tenant_id": "TENANT_X",
  "user_id": "user_123",
  "action": "approve"
}
```

**Response:**
```json
{
  "job_id": "DR-2025-0001",
  "report_id": "REP-2025-0001",
  "status": "research_queued",
  "message": "Research job queued. Connect to WebSocket for real-time updates.",
  "ws_url": "/ws/deep-research/DR-2025-0001"
}
```

**Internal actions:**
1. Insert into `background_jobs` with `type=DEEP_RESEARCH`, `status=QUEUED`
2. Update `deep_research_jobs.status` = `RESEARCH_QUEUED`
3. Create `deep_research_reports` document with `status=QUEUED`

### 9.3 GET `/api/deepresearch/status/{job_id}`

**Response:**
```json
{
  "job_id": "DR-2025-0001",
  "status": "research",
  "progress": 40,
  "current_section": "sec2",
  "current_section_title": "2. Microsoft AI Strategy",
  "started_at": "2025-11-18T10:05:00Z"
}
```

Reads Redis hash first (fast), falls back to MongoDB.

### 9.4 GET `/api/deepresearch/reports`

```
GET /api/deepresearch/reports?tenant_id=TENANT_X&user_id=user_123&page=1&limit=20
```

**Response:**
```json
{
  "reports": [
    {
      "report_id": "REP-2025-0001",
      "job_id": "DR-2025-0001",
      "topic": "Compare Microsoft and Google AI strategies for enterprise SaaS.",
      "llm_model": "gemini-2.5-pro",
      "executed_at": "2025-11-18T10:05:00Z",
      "status": "COMPLETED",
      "summary": "Short executive summary for listing views.",
      "citation_count": 23
    }
  ],
  "total": 15,
  "page": 1,
  "limit": 20
}
```

### 9.5 GET `/api/deepresearch/reports/{report_id}`

**Response:**
```json
{
  "report_id": "REP-2025-0001",
  "html_url": "<presigned_s3_url>",
  "html_url_expires_at": "2025-11-18T11:05:00Z",
  "citations": [
    {
      "id": "src1",
      "title": "Microsoft AI Strategy 2025",
      "url": "https://...",
      "source_type": "web"
    }
  ]
}
```

---

## 10. Data Model — MongoDB Collections

### 10.1 `background_jobs` (Existing — Add New Type)

```json
{
  "_id": "ObjectID",
  "type": "DEEP_RESEARCH",
  "ref_id": "DR-2025-0001",
  "tenant_id": "TENANT_X",
  "status": "QUEUED",
  "created_at": "2025-11-18T10:00:00Z",
  "started_at": null,
  "completed_at": null,
  "error": null
}
```

**Indexes:** `{ "type": 1, "status": 1 }`, `{ "ref_id": 1 }`

### 10.2 `deep_research_jobs` (New)

```json
{
  "_id": "ObjectID",
  "job_id": "DR-2025-0001",
  "report_id": "REP-2025-0001",
  "tenant_id": "TENANT_X",
  "user_id": "user_123",
  "topic": "Compare Microsoft and Google AI strategies for enterprise SaaS.",
  "refined_topic": "Compare Microsoft and Google AI strategies for enterprise SaaS",
  "chat_bot_id": "UUID",
  "llm_model": "gemini-2.5-pro",
  "status": "QUEUED",
  "created_at": "2025-11-18T10:00:00Z",
  "started_at": null,
  "completed_at": null,
  "progress": 0,
  "plan": {
    "plan_id": "PLAN-abc123",
    "sections": [
      { "id": "sec1", "title": "1. Objectives", "description": "...", "search_strategy": ["tavily"] }
    ]
  },
  "checklist": ["Cover 2024-2025 announcements", "Include pricing"],
  "analysis": {
    "depth_of_research": "in-depth",
    "audience": "business executives",
    "objective": "business",
    "domain": "enterprise technology",
    "recency_scope": "2023-2026",
    "source_scope": ["tavily", "serperAPI", "content_lake"],
    "assumptions": ["B2B focus based on chat context"]
  },
  "attachments": [
    { "source": "repository", "object_id": "OBJ123" },
    { "source": "gdrive", "file_id": "..." }
  ],
  "tools": { "content_lake": true },
  "metadata": {
    "otel_trace_id": "abc123",
    "total_tokens_used": 0,
    "total_cost_usd": 0.0,
    "pro_calls": 0,
    "flash_calls": 0
  },
  "error": null
}
```

**Indexes:** `{ "job_id": 1 }` (unique), `{ "tenant_id": 1, "user_id": 1 }`, `{ "status": 1 }`

### 10.3 `deep_research_reports` (New)

**Note:** Clear History in chat MUST NOT touch this collection.

```json
{
  "_id": "ObjectID",
  "report_id": "REP-2025-0001",
  "job_id": "DR-2025-0001",
  "tenant_id": "TENANT_X",
  "user_id": "user_123",
  "topic": "Compare Microsoft and Google AI strategies for enterprise SaaS.",
  "chat_bot_id": "deep_research_gemini",
  "llm_model": "gemini-2.5-pro",
  "executed_at": "2025-11-18T10:05:00Z",
  "status": "COMPLETED",
  "summary": "Short executive summary for listing views.",
  "html_s3_key": "deep-research/TENANT_X/USER_123/REP-2025-0001.html",
  "citations": [
    { "id": "src1", "title": "Microsoft AI Strategy 2025", "url": "https://...", "source_type": "web" }
  ]
}
```

**Indexes:** `{ "report_id": 1 }` (unique), `{ "tenant_id": 1, "user_id": 1, "executed_at": -1 }`

### 10.4 `source_scores` (New — For Source Intelligence)

```json
{
  "_id": "ObjectID",
  "url_hash": "sha256_of_url",
  "url": "https://example.com/article",
  "domain": "example.com",
  "tenant_id": "TENANT_X",
  "authenticity_score": 0.85,
  "was_url_authentic": true,
  "recency": "2025-01-15",
  "times_used": 5,
  "times_outdated": 0,
  "relevant_topics": ["AI strategy", "enterprise SaaS"],
  "last_scored_at": "2025-11-18T10:05:00Z",
  "scores_history": [
    { "job_id": "DR-2025-0001", "score": 0.85, "scored_at": "..." }
  ]
}
```

**Indexes:** `{ "url_hash": 1, "tenant_id": 1 }`, `{ "domain": 1 }`

### 10.5 `langgraph_checkpoints` (New — For Crash Recovery)

Managed automatically by LangGraph's AsyncMongoDBSaver. Stores graph state after each node completion.

---

## 11. Worker Flow — EKS Job

### 11.1 Pseudo-Steps

```
 1. Fetch one `background_jobs` doc: type=DEEP_RESEARCH, status=QUEUED
 2. Mark background_jobs.status = RUNNING, started_at = now
 3. Load `deep_research_jobs` by ref_id = job_id
 4. Mark deep_research_jobs.status = RUNNING, started_at = now
 5. Build execution context:
    - Topic, plan sections, checklist
    - Attachments (fetch file content from Redis by object_id)
    - Tools config (web search: always on; Content Lake: if enabled)
    - Depth config (surface/mid/in-depth → sets search iterations + source limits)
 6. SUPERVISOR: spawn parallel section researchers via Send()
 7. FOR EACH SECTION (parallel):
    a. Generate search queries (Flash)
    b. Execute parallel search across enabled sources
    c. Verify URLs (web sources only)
    d. COMPRESS findings — 50K tokens → 3-5K (Flash) ★ CRITICAL
    e. Score sources using past scores from MongoDB (Flash)
    f. Emit streaming progress event
 8. SUPERVISOR REFLECTION: check for knowledge gaps (Pro)
    - If gaps → spawn targeted researchers (max 2 rounds)
 9. KNOWLEDGE FUSION: combine web + CL + Arxiv + files (Pro)
10. REPORT WRITER: generate HTML with inline citations (Pro)
    - Stream html_chunks via Redis pub/sub
11. REPORT REVIEWER: validate against checklist (Pro)
    - If major issues → back to writer (max 1 revision)
12. Upload HTML to S3:
    - Key: deep-research/{tenant_id}/{user_id}/{report_id}.html
13. Update deep_research_reports: status=COMPLETED, html_s3_key, citations
14. Update deep_research_jobs: status=COMPLETED, progress=100
15. Update background_jobs: status=COMPLETED
16. Stream "completed" event
```

### 11.2 EKS Job Spec

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: deep-research-DR-2025-0001
  namespace: kadal-deepresearch
spec:
  backoffLimit: 2
  activeDeadlineSeconds: 1800
  ttlSecondsAfterFinished: 3600
  template:
    spec:
      restartPolicy: Never
      serviceAccountName: deepresearch-worker
      containers:
        - name: research-worker
          image: kadal-registry/deep-research-worker:latest
          resources:
            requests: { cpu: "500m", memory: "1Gi" }
            limits: { cpu: "2000m", memory: "4Gi" }
          env:
            - name: JOB_ID
              value: "DR-2025-0001"
            - name: MONGO_URI
              valueFrom: { secretKeyRef: { name: kadal-secrets, key: mongo-uri } }
            - name: REDIS_HOST
              valueFrom: { configMapKeyRef: { name: kadal-config, key: redis-host } }
            - name: GEMINI_API_KEY
              valueFrom: { secretKeyRef: { name: kadal-secrets, key: gemini-api-key } }
            - name: S3_BUCKET
              value: "kadal-deep-research-reports"
            - name: LANGFUSE_HOST
              value: "http://langfuse-service:3000"
            - name: OTEL_COLLECTOR_ENDPOINT
              valueFrom: { configMapKeyRef: { name: kadal-config, key: otel-endpoint } }
```

---

## 12. Streaming Interface

### 12.1 Architecture

```
EKS Worker ──PUBLISH──→ Redis Pub/Sub ──→ FastAPI WS Handler ──→ Browser
                         channel:                subscribes to
                         job:{job_id}:events     same channel
```

### 12.2 WebSocket Endpoint

```
WebSocket: /ws/deep-research/{job_id}
Client subscribes after calling /run
```

### 12.3 Event Types

```json
// Section start
{ "job_id": "DR-2025-0001", "event": "section_start", "section_id": "sec2", "title": "2. Market landscape" }

// Content chunk (streamed HTML delta)
{ "job_id": "DR-2025-0001", "event": "content_delta", "section_id": "sec2", "html_chunk": "<p>First paragraph...</p>" }

// Progress update (0-100)
{ "job_id": "DR-2025-0001", "event": "progress", "progress": 40 }

// Completion
{ "job_id": "DR-2025-0001", "event": "completed", "report_id": "REP-2025-0001" }

// Error
{ "job_id": "DR-2025-0001", "event": "error", "message": "We couldn't complete this deep research request. Please try again, or simplify the topic." }
```

---

## 13. Search & Tool Integration

### 13.1 Search Sources

| Source | When Used | MCP Server | Needs Verification |
|--------|----------|------------|-------------------|
| **Tavily** | Always (web search) | Yes | Yes |
| **SerperAPI** | Always (web search) | Yes | Yes |
| **Arxiv** | When academic depth needed | Yes | No (trusted) |
| **Content Lake** | When `tools.content_lake = true` | Via existing API | No (internal) |
| **Uploaded Files** | When attachments present | N/A (Redis cache) | No (user-provided) |

### 13.2 Search Depth (Adaptive)

```
depth = "surface":      1 search iteration,  max 3 sources/section
depth = "intermediate": 2 search iterations, max 5 sources/section
depth = "in-depth":     3 search iterations, max 10 sources/section
                        + supervisor reflection enabled
```

### 13.3 Parallel Search Pattern

```
For each section researcher:
  │
  ├── LLM generates search queries (1 Flash call)
  │
  ├── PARALLEL search across enabled sources:
  │   ├── Tavily ──────────┐
  │   ├── SerperAPI ───────┤ (concurrent async)
  │   ├── Arxiv ───────────┤
  │   ├── Content Lake ────┤
  │   └── File content ────┘
  │
  ├── Source Verifier (web URLs only)
  │
  ├── ★ COMPRESS ★ (1 Flash call)
  │   50K+ raw tokens → 3-5K relevant facts + citations
  │
  ├── Score sources (1 Flash call)
  │   Check past scores from source_scores collection
  │
  └── Return compressed findings to supervisor
```

---

## 14. Observability Stack

### 14.1 Architecture (All Open Source, Self-Hosted)

```
Services (FastAPI + Worker)
    │
    ├── OTel SDK ──→ OTel Collector ──→ Tempo (traces)
    │                                ──→ Prometheus (metrics)
    │                                ──→ Loki (logs)
    │                                       │
    │                                       ▼
    │                                    Grafana (unified dashboard)
    │
    └── Langfuse SDK ──→ Langfuse (self-hosted)
                         (LLM traces, token/cost, prompt management)
```

### 14.2 OpenTelemetry — What It Tracks

**Auto-instrumented:** FastAPI requests, MongoDB queries, Redis commands, outgoing HTTP calls

**Custom spans:** `create_research_plan`, `execute_research_section`, `parallel_search`, `compress_findings`, `source_verification`, `knowledge_fusion`, `report_generation`, `s3_upload`

**Metrics:**
- `deep_research.jobs.created/completed/failed` (counters)
- `deep_research.jobs.active` (gauge)
- `deep_research.job.duration_seconds` (histogram)
- `deep_research.llm.call.duration_seconds` (histogram, tagged by model tier)
- `deep_research.llm.tokens.total` (counter, tagged by model: pro/flash)
- `deep_research.search.call.duration_seconds` (histogram, per source)

### 14.3 Langfuse — What It Tracks

- Every LLM call: prompt, response, model, tokens, cost, latency
- Tagged by model tier: Pro vs Flash
- Trace per job, session_id = job_id
- Linked to OTel via shared trace_id

---

## 15. Caching Strategy — Redis

```
job:{job_id}:status           → Hash {status, progress, section, updated_at}  TTL: 24hr
job:{job_id}:events           → Pub/Sub channel for WebSocket relay
file:{object_id}:content      → String (existing platform cache)
source:{url_hash}:{tenant_id} → Hash {score, recency, times_used}  TTL: 7 days
rate:plan:{tenant_id}:{uid}   → Counter with TTL (rate limiting)
rate:run:{tenant_id}:{uid}    → Counter with TTL (rate limiting)
```

---

## 16. Error Handling & Resilience

### 16.1 On Any Error — Update All 3 Collections

```
deep_research_jobs.status   = FAILED, error = { code, message }
deep_research_reports.status = FAILED
background_jobs.status      = FAILED
+ emit streaming error event
```

### 16.2 Partial Failure Strategy

If one section fails during research: record failure, continue other sections. Final report notes incomplete sections. Job marked COMPLETED unless >50% of sections fail.

### 16.3 Idempotent Operations

All worker operations use upsert (not insert) for safe retries. S3 put_object is naturally idempotent.

---

## 17. Security & Multi-Tenancy

Every MongoDB query MUST include `tenant_id`. S3 keys namespaced by tenant. Reports accessed via presigned URLs (1hr expiry). `deep_research_reports` NOT touched by "Clear History". All credentials via K8s Secrets.

---

## 18. Project Structure

```
deep-research-service/
├── README.md
├── ARCHITECTURE.md                   # This document
├── CLAUDE.md                         # Instructions for Claude Code
├── .env                              # Local dev config
├── Dockerfile.api
├── Dockerfile.worker
├── docker-compose.yml
├── requirements.txt
├── pyproject.toml
│
├── app/                              # FastAPI application
│   ├── __init__.py
│   ├── main.py                       # FastAPI app, lifespan, middleware
│   ├── config.py                     # Settings (pydantic-settings)
│   │
│   ├── api/
│   │   ├── router.py
│   │   ├── plan.py                   # POST /plan
│   │   ├── run.py                    # POST /run
│   │   ├── status.py                 # GET /status/{job_id}
│   │   ├── reports.py                # GET /reports
│   │   └── websocket.py             # WS /ws/deep-research/{job_id}
│   │
│   ├── models/                       # Pydantic v2 models
│   │   ├── plan.py
│   │   ├── run.py
│   │   ├── status.py
│   │   ├── reports.py
│   │   └── common.py
│   │
│   ├── db/
│   │   ├── mongodb.py                # Motor async client
│   │   └── redis.py                  # redis.asyncio client
│   │
│   ├── services/
│   │   ├── plan_service.py
│   │   ├── job_service.py
│   │   ├── status_service.py
│   │   └── report_service.py
│   │
│   └── telemetry/
│       ├── otel.py                   # OpenTelemetry init
│       └── langfuse.py               # Langfuse init
│
├── worker/                           # EKS Worker
│   ├── __init__.py
│   ├── main.py                       # Entrypoint
│   ├── executor.py                   # Runs execution graph
│   └── streaming.py                  # Redis pub/sub publisher
│
├── graphs/                           # LangGraph definitions
│   ├── planning/
│   │   ├── graph.py                  # Planning graph builder
│   │   ├── state.py                  # PlanningState
│   │   └── nodes/
│   │       ├── context_builder.py    # Fetch chat history + files (no LLM)
│   │       ├── query_analyzer.py     # Analyze topic (Pro)
│   │       ├── clarifier.py          # Return questions to user
│   │       ├── plan_creator.py       # Generate plan (Pro)
│   │       └── plan_reviewer.py      # Self-review plan (Pro)
│   │
│   └── execution/
│       ├── graph.py                  # Execution graph builder
│       ├── state.py                  # ExecutionState
│       ├── section_subgraph.py       # Section researcher sub-graph
│       └── nodes/
│           ├── supervisor.py         # Spawn researchers + reflection
│           ├── search_query_gen.py   # Generate queries (Flash)
│           ├── searcher.py           # Parallel search across sources
│           ├── source_verifier.py    # URL authenticity (web only)
│           ├── compressor.py         # ★ Compress findings (Flash)
│           ├── scorer.py             # Score sources (Flash)
│           ├── knowledge_fusion.py   # Combine all source types (Pro)
│           ├── report_writer.py      # Generate HTML report (Pro)
│           ├── report_reviewer.py    # Validate against checklist (Pro)
│           └── exporter.py           # S3 upload + MongoDB updates
│
├── tools/                            # Search tool integrations
│   ├── tavily_search.py
│   ├── serper_search.py
│   ├── arxiv_search.py
│   ├── content_lake.py
│   ├── file_content.py              # Redis cache lookup
│   └── mcp_client.py
│
├── k8s/                              # DO NOT BUILD NOW — DevOps will create
│   └── (deferred — needs cluster access, namespace, registry setup)
│
└── tests/                            # DO NOT BUILD NOW — write after code works
    └── (deferred — write after end-to-end flow is working locally)
```

---

## 19. Deployment Architecture

```
AWS EKS Cluster
├── Namespace: kadal-deepresearch
│
├── Deployment: deep-research-api (2-3 replicas)
│   ├── Port: 8000, Health: /health
│   ├── Resources: 500m/1Gi (request), 1/2Gi (limit)
│   └── ServiceAccount: deepresearch-api
│
├── Service: deep-research-api-svc (ClusterIP → 8000)
│
├── Job: deep-research-{job_id} (created dynamically)
│   ├── Resources: 500m/1Gi (request), 2/4Gi (limit)
│   ├── backoffLimit: 2, activeDeadlineSeconds: 1800
│   └── ttlSecondsAfterFinished: 3600
│
├── ConfigMap: kadal-config
│   └── REDIS_HOST, OTEL_ENDPOINT, S3_BUCKET, CONTENT_LAKE_URL
│
└── Secret: kadal-secrets
    └── MONGO_URI, REDIS_PASSWORD, GEMINI_API_KEY, LANGFUSE keys, TAVILY key
```

---

## 20. Phased Build Plan

### Phase 1: Core System (Ship First — 2-3 Weeks)

**Goal:** Working end-to-end system. Can demo to stakeholders.

**Build:**
- FastAPI skeleton + all endpoints
- MongoDB CRUD for all 3 collections
- Redis caching + pub/sub for streaming
- Planning graph: Context Builder → Query Analyzer → Plan Creator (combined with self-review)
- Execution graph: SEQUENTIAL section research (not parallel yet)
  - Search → Compress → Write section content
  - Simple report generation (combine sections)
- Basic WebSocket streaming (progress + completed events)
- OpenTelemetry auto-instrumentation
- Langfuse basic integration

**Mock:** LLM calls (use canned responses for testing), search tools (return fake results)

**Skip:** Parallel execution, supervisor reflection, knowledge fusion, source scoring, report reviewer

### Phase 2: Quality & Performance (2-3 Weeks)

**Goal:** Production-quality research results. Cost-optimized.

**Add:**
- Parallel section research via Send() API
- Supervisor reflection (knowledge gap detection)
- Knowledge Fusion node (web + CL + Arxiv + files)
- Context compression node (CRITICAL for quality)
- Source verification for web URLs
- Multi-model tiers (Flash for cheap tasks, Pro for reasoning)
- Report reviewer (validate against checklist)
- `content_delta` streaming events (not just progress)
- Langfuse detailed LLM tracing with cost tracking

### Phase 3: Intelligence & Scale (Ongoing)

**Goal:** System gets smarter over time. Kadal's competitive edge.

**Add:**
- Source Intelligence (persistent scoring in `source_scores`)
- Adaptive search depth based on query analysis
- Chat context deep integration (infer audience/domain without asking)
- Report revision loop (reviewer → writer → reviewer)
- Rate limiting per user/tenant
- LLM fallback (if Gemini down → fallback model)
- Search source fallback (if Tavily down → continue with others)
- EKS Job monitoring + alerting via Grafana

---

## 21. Open Questions for Team

| # | Question | Impact | Needs Answer From |
|---|----------|--------|-------------------|
| 1 | Is the EKS scheduler a long-running poller or does the API directly create EKS Jobs? | Deployment design | DevOps |
| 2 | Can we add `langgraph_checkpoints` and `source_scores` MongoDB collections? | Crash recovery, source intelligence | Tech Lead |
| 3 | Should we use Gemini Flash for cheap tasks (compression, scoring)? This saves 40-60% on LLM cost. | Cost | Product / Tech Lead |
| 4 | Rate limits per user/tenant for `/plan` and `/run`? | Resource protection | Product |
| 5 | If Gemini is unavailable, should we fall back to another model? | Resilience | Product |
| 6 | If Tavily is down, continue with remaining sources? | UX | Product |
| 7 | New S3 bucket `kadal-deep-research-reports` or folder in existing bucket? | Infrastructure | DevOps |
| 8 | WebSocket authentication — same JWT as REST? | Security | Platform |
| 9 | Content Lake API auth — how does worker authenticate? | Integration | Platform |
| 10 | Max concurrent jobs per tenant? | Resource management | Product / DevOps |

---

*Document End — Version 2.0*  
*Changes from v1.0: Added Supervisor-Researcher pattern, Knowledge Fusion, Multi-Model Strategy, Context Compression, Phased Build Plan, Source Intelligence, Adaptive Search Depth*