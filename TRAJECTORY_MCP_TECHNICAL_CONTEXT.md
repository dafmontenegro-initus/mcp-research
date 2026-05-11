# trajectory-mcp — Technical Context Document

> **Purpose of this document**: Provide deep technical context about trajectory-mcp to an AI assistant so it can understand the server's architecture, tool surface, data model, and design decisions — and propose extensions or integrations. This document complements the README; it does not repeat it.

---

## 1. Identity & Purpose

**trajectory-mcp** is a read-only MCP (Model Context Protocol) server that exposes Trajectory's operational data — meetings and Wrike tasks — to AI assistants (Claude Desktop, MCP Inspector, etc.) without requiring any orchestration layer or LLM intermediary.

**Who uses it**: Claude instances operating inside Claude Desktop or any MCP-compatible client. The primary documented use case is autonomous Weekly Status Report (WSR) generation.

**What it is not**: It is not a reasoning system, not an orchestrator, and not a write path. It is a thin, secure query facade over two MySQL mirrors and an S3 bucket.

**Relationship to the parent orchestrator**: The Lambda orchestrator is the parent system. trajectory-mcp exposes the same underlying data (meetings catalog, Wrike task mirror, S3 transcripts) but via the MCP protocol instead of a Lambda event interface. The two systems are independent — trajectory-mcp does not call the orchestrator and does not share code with it.

---

## 2. Architecture in Depth

### 2.1 Structural Overview

```
Claude Desktop / MCP Inspector
         │
         │  MCP protocol (HTTP streamable or stdio)
         ▼
    server.py  ──  FastMCP instance, tool registration
         │
         ├── tools/meetings.py  ──  list_meetings, get_meeting_details, get_meeting_transcript
         │       │
         │       ├── db.py (get_meet_conn)  ──  MySQL: meetings_assets.meetings
         │       └── boto3 S3              ──  S3: {bucket}/{company_id}/meetings/transcripts/
         │
         └── tools/wrike.py  ──  find_task, list_tasks, get_task_details, get_wrike_users
                 │
                 └── db.py (get_wrike_conn)  ──  MySQL: wrike.{COMPANY}_FULL
```

There are no agent loops, no LLM calls, no tool chaining logic inside the server. The server receives a tool call, runs a SQL query or S3 GET, and returns the result. All reasoning is done by the calling AI assistant.

### 2.2 Transport Modes

| Mode | Invocation | Used by |
|------|-----------|---------|
| HTTP (streamable-http) | `python server.py` → `localhost:8001` | MCP Inspector, direct HTTP clients |
| stdio | `python server.py --stdio` | Claude Desktop (subprocess pipe) |

FastMCP detects the `--stdio` flag at startup and switches transports. The tool surface is identical in both modes.

### 2.3 Company ID Resolution

`config.py` maintains two alias maps — one for the meetings DB, one for the Wrike DB — that translate caller-supplied IDs to canonical table/query identifiers:

```python
# config.py (simplified)
MEET_COMPANY_ALIASES = {
    "TJV": "NWN",
    "BMT": "BMC",
}
WRIKE_COMPANY_ALIASES = {
    "TJV": "TJV",   # pass-through
    "NWN": "NWN",
}
```

This is transparent to callers — they always use the same `company_id` they know. The resolution step prevents cross-tenant data leakage caused by alias confusion.

### 2.4 Database Connectivity

`db.py` exposes two factory functions:

```python
def get_meet_conn() -> pymysql.Connection   # meetings_assets DB
def get_wrike_conn() -> pymysql.Connection  # wrike DB
```

Both return `DictCursor`-enabled connections with UTF-8mb4 charset. Connections are created per-call (not pooled at the module level). The DB credentials are split: meetings and Wrike use separate host/user/password env vars, so a compromise of one does not expose the other.

---

## 3. Tool Surface

All seven tools are annotated with MCP's `ToolAnnotations`:

```python
annotations=ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
)
```

This signals to any MCP client that no tool causes side effects.

### 3.1 Meetings Tools

#### `list_meetings`

Discovers meetings in a date range. The primary entry point for WSR data gathering.

| Parameter | Type | Description |
|-----------|------|-------------|
| `company_id` | str | Required |
| `start_after` | str | ISO date — meetings starting after this date |
| `end_before` | str | ISO date — meetings ending before this date |
| `host_email` | str | Optional filter: exact host match |
| `participant_email` | str | Optional filter: LIKE match in participants JSON |
| `limit` | int | Max results, up to 200 |

Returns per-meeting: `meeting_uuid`, `title`, `start_time`, `end_time`, `host_email`, `duration`, `has_transcript`, `has_synthesis` (derived from `synthesized_meeting` nullability).

**SQL note**: Filters on `status != 'inactive'` implicitly — deleted meetings are never returned.

#### `get_meeting_details`

Fetches full metadata + AI synthesis for a batch of known UUIDs.

| Parameter | Type | Description |
|-----------|------|-------------|
| `meeting_uuids` | list[str] | Up to 50 UUIDs |
| `company_id` | str | Required |

Returns all `list_meetings` fields plus: `synthesized_meeting` (AI-generated meeting summary), `zoom_summary`, full `participants_emails` array.

**When to prefer this over transcripts**: When `has_synthesis=true`, this is faster and cheaper than fetching the raw VTT. Synthesis is already done; this call just reads it from the DB.

#### `get_meeting_transcript`

Downloads and returns the raw VTT transcript for a single meeting from S3.

| Parameter | Type | Description |
|-----------|------|-------------|
| `meeting_uuid` | str | Single UUID |
| `company_id` | str | Required |

S3 key pattern: `{bucket}/{company_id}/meetings/transcripts/{uuid}/part1.vtt`, `part2.vtt`, etc. The tool concatenates all parts and returns the full VTT text.

**Warning**: Full transcripts can be 50,000+ tokens. Callers should fetch transcripts only when synthesis is unavailable (`has_synthesis=false`) or when verbatim content is required.

Returns: `transcript` (full VTT text), `parts` (number of S3 parts merged).

### 3.2 Wrike Tools

All Wrike tools read from a per-company MySQL table: `wrike.{COMPANY}_FULL`. This is a mirror of the Wrike SaaS API, not a live API call.

#### `find_task`

Fuzzy title search. Useful for locating a known ticket by partial name.

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | str | Partial title string (LIKE match) |
| `company_id` | str | Required |
| `limit` | int | Default 10, max 50 |

Returns: `ticket_id`, `title`, `status`, `custom_status`, `responsible`, `due_date`, `start_date`, `created_date`, `updated_date`, `permalink`.

**Primary WSR use**: Finding the baseline WSR ticket. Search for `"Status Report"`, sort by date in title, pick the most recent one to anchor `cutoff_date`.

#### `list_tasks`

Filtered task enumeration with up to seven independent filter dimensions.

| Parameter | Type | Description |
|-----------|------|-------------|
| `company_id` | str | Required |
| `status` | list[str] | IN filter: `["Active", "Deferred", "Completed"]` |
| `responsible` | str | LIKE match on assignee name |
| `title_keyword` | str | LIKE match on title |
| `created_after` | str | ISO date |
| `updated_after` | str | ISO date |
| `due_before` | str | ISO date |
| `due_after` | str | ISO date |
| `limit` | int | Max 500, default 100 |

Results ordered by `updated_date DESC`.

**WSR 5-pass pattern** (documented in README):

| Pass | Filter | Purpose |
|------|--------|---------|
| 1 | Title contains "Status Report" | Baseline tickets (structure template) |
| 2 | `created_after=cutoff_date`, status Active | New work this cycle |
| 3 | `updated_after=cutoff_date`, status Completed | Closed/completed this cycle |
| 4 | `updated_after=cutoff_date` (any status) | Activity delta cross-check |
| 5 | `due_before=today`, status Active | Overdue tickets |

#### `get_task_details`

Fetches full content for a batch of known ticket IDs.

| Parameter | Type | Description |
|-----------|------|-------------|
| `ticket_ids` | list[str] | Up to 100 ticket IDs |
| `company_id` | str | Required |

Returns all `list_tasks` fields plus: `description` (full HTML/markdown content), `comments` (latest activity thread), `paths` (Wrike folder hierarchy).

**When to use**: After `list_tasks` identifies relevant tickets, call this to get the `description` and `comments` needed to write meaningful WSR bullet points.

#### `get_wrike_users`

Returns all unique `responsible` field values for a company.

| Parameter | Type | Description |
|-----------|------|-------------|
| `company_id` | str | Required |

Returns: list of assignee name strings as they appear in the DB.

**Why this exists**: The `responsible` field in Wrike is a free-text name. Without knowing the exact variant ("John Smith" vs "J. Smith"), LIKE filters produce inconsistent results. This tool provides the authoritative list before filtering by person.

---

## 4. Data Model

### 4.1 Meetings: `meetings_assets.meetings`

Key columns used by trajectory-mcp:

| Column | Type | Notes |
|--------|------|-------|
| `meeting_uuid` | varchar | Primary key |
| `meeting_title` | varchar | Display name |
| `start_time` | datetime | UTC |
| `end_time` | datetime | UTC |
| `host_email` | varchar | Zoom host |
| `participants_emails` | JSON | Array of email strings |
| `duration` | int | Minutes |
| `status` | varchar | `'inactive'` = soft-deleted |
| `has_transcript` | tinyint | 1 if VTT exists in S3 |
| `synthesized_meeting` | text | AI-generated summary (nullable) |
| `zoom_summary` | text | Zoom-native summary (nullable) |

### 4.2 Wrike: `wrike.{COMPANY}_FULL`

Key columns used by trajectory-mcp:

| Column | Type | Notes |
|--------|------|-------|
| `ticket_id` | varchar | Wrike native ID |
| `title` | varchar | Task name |
| `status` | varchar | `Active`, `Completed`, `Deferred`, etc. |
| `custom_status` | varchar | Client-defined status label |
| `importance` | varchar | `Normal`, `High`, `Critical` |
| `responsible` | varchar | Assignee display name |
| `due_date` | date | Nullable |
| `start_date` | date | Nullable |
| `created_date` | datetime | |
| `updated_date` | datetime | Used for ordering; indexed |
| `description` | text | Full task body (HTML/markdown) |
| `comments` | text | Latest activity thread |
| `permalink` | varchar | Direct Wrike URL |
| `paths` | text | Folder hierarchy path |

### 4.3 S3 Transcripts

Layout: `{DEV_S3_BUCKET}/{company_id}/meetings/transcripts/{meeting_uuid}/part1.vtt`

Multi-part transcripts (long Zoom recordings split at 2-hour boundaries) use sequentially numbered parts. trajectory-mcp concatenates all parts automatically.

---

## 5. Security Model

### 5.1 Read-Only Enforcement

Three independent layers:

1. **MCP annotations**: `readOnlyHint=True`, `destructiveHint=False` — client-visible signal
2. **No write SQL**: No INSERT/UPDATE/DELETE anywhere in the codebase
3. **DB user privileges**: MySQL users provisioned with SELECT-only grants

### 5.2 SQL Injection Prevention

All query values use `%s` parameterized placeholders via pymysql. Column names and table names are never interpolated from user input — they are hardcoded strings in the tool implementations.

Exception: the Wrike table name includes the company ID (`wrike.{COMPANY}_FULL`). This is resolved through `config.py`'s alias map — the company ID is never directly interpolated from caller input into the table name.

### 5.3 Company Isolation

Every tool requires `company_id`. All queries are scoped by it. There is no global query across companies. The alias resolution in `config.py` is the single normalization point — no tool does its own alias logic.

### 5.4 Batch Limits

Hard limits prevent unbounded queries:

| Tool | Limit |
|------|-------|
| `list_meetings` | 200 rows |
| `get_meeting_details` | 50 UUIDs per call |
| `list_tasks` | 500 rows |
| `get_task_details` | 100 ticket IDs per call |
| `find_task` | 50 rows |

### 5.5 No Wrike API Token

trajectory-mcp never calls the Wrike REST API. All Wrike data comes from the MySQL mirror. This means:
- No OAuth token management
- No Wrike rate limits
- No write capability (Wrike API is the only write path in the parent orchestrator)

---

## 6. Configuration

All secrets and connection parameters are loaded from `.env` via `python-dotenv`.

### 6.1 Required Environment Variables

```bash
# Meetings database
MEET_DEV_DB_HOST=...
MEET_DEV_DB_PORT=3306
MEET_DEV_DB_USER=...
MEET_DEV_DB_PASSWORD=...

# Wrike database
WK_DEV_DB_HOST=...
WK_DEV_DB_PORT=3306
WK_DEV_DB_USER=...
WK_DEV_DB_PASSWORD=...

# S3 (meeting transcripts)
DEV_S3_BUCKET=...
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

`config.py` validates that all required vars are present at import time and raises a descriptive error if any are missing.

### 6.2 Claude Desktop Integration

```json
{
  "mcpServers": {
    "trajectory-mcp": {
      "command": "C:\\path\\to\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\server.py", "--stdio"]
    }
  }
}
```

### 6.3 Dependencies

```
fastmcp>=2.0        # MCP server framework
pymysql>=1.1.0      # MySQL connectivity
boto3>=1.34         # S3 transcript access
python-dotenv>=1.0  # .env loading
```

---

## 7. WSR Use Case — Primary Workflow

The README documents a complete Claude system prompt for WSR generation. The orchestration pattern Claude follows:

```
1. find_task("Status Report", company_id)
   → Locate baseline WSR ticket, extract cutoff_date from title

2. list_tasks (5 passes: baseline, new, closed, delta, overdue)
   → Build complete task inventory for the reporting period

3. get_task_details(all_relevant_ticket_ids)
   → Get description + comments for meaningful bullet points

4. list_meetings(start_after=cutoff_date, end_before=today)
   → Discover all meetings in scope

5. get_meeting_details(meeting_uuids)   [if has_synthesis=true]
   OR
   get_meeting_transcript(meeting_uuid)  [if has_synthesis=false]
   → Get meeting content

6. Draft WSR markdown (NO ticket creation — trajectory-mcp is read-only)
```

**Why no RAG**: WSR requires temporal completeness — all tasks/meetings in a date range, not the most semantically similar ones. SQL date filters are exact; RAG would miss in-scope items that don't match semantically.

**Why trajectory-mcp for WSR**: An MCP-enabled Claude can run this workflow autonomously with full visibility into each step, no Lambda timeout constraints, and no need for the HITL survey protocol.

---

## 8. Comparison with the Parent Orchestrator

| Aspect | Lambda Orchestrator | trajectory-mcp |
|--------|-----------------|-------------|
| **Invocation** | Lambda event | MCP protocol call |
| **Reasoning** | Internal LangGraph ReAct loops | External (calling AI assistant) |
| **Memory** | S3 checkpoint per session | None (stateless) |
| **Write capability** | Yes (Wrike ticket creation) | No |
| **RAG** | Separate Lambda invocation | Not available |
| **HITL** | Stateless survey pattern | Not needed (no writes) |
| **Flavors/modes** | `default`, `wsr`, `wsr_scheduled` | N/A — single tool surface |
| **Auth** | company_id + user_id in event | company_id per tool call |
| **Timeout risk** | Lambda 15-min hard limit | None |
| **Latency** | Cold start + LLM reasoning time | DB query latency only |

---

## 9. Current Limitations

### 9.1 No Environment Routing

All env vars use the `DEV_` prefix. There is no `prod` or `qa` variant — the server always reads from a single configured environment. Adding multi-environment support would require either multiple `.env` files or runtime env switching.

### 9.2 No RAG Access

Semantic search (the separate RAG Lambda in the parent orchestrator) is not accessible from trajectory-mcp. Callers can only filter by exact SQL predicates (date, status, assignee, title LIKE). For WSR this is intentional (see Section 7), but for ad-hoc meeting discovery, RAG would improve recall.

### 9.3 Transcript Token Budget

`get_meeting_transcript` returns raw VTT text without summarization or truncation. A full-day meeting can exceed 50,000 tokens. The server provides no chunking or pagination — callers must be aware of their context window limits.

### 9.4 Connection-Per-Call DB Pattern

`db.py` creates a new MySQL connection on each tool call rather than maintaining a connection pool. For low-concurrency Claude Desktop use this is acceptable. At higher concurrency (e.g., multiple simultaneous MCP clients), connection overhead would become measurable.

### 9.5 No Structured Output Contract

All tools return Python dicts/lists serialized to JSON. There are no typed response schemas published to MCP clients. Claude infers field types from example values. This means clients cannot validate response structure ahead of time.

### 9.6 Single Tenant Per Server Instance

One `.env` file means one set of DB credentials. To serve multiple environments simultaneously, multiple server instances must be run on different ports.

---

## 10. Codebase Snapshot

| File | Role | ~Lines |
|------|------|--------|
| `server.py` | FastMCP instance, tool registration, transport setup | ~50 |
| `config.py` | Env loading, validation, company alias maps | ~60 |
| `db.py` | MySQL connection factories (meetings + wrike) | ~30 |
| `tools/meetings.py` | `list_meetings`, `get_meeting_details`, `get_meeting_transcript` | ~190 |
| `tools/wrike.py` | `find_task`, `list_tasks`, `get_task_details`, `get_wrike_users` | ~247 |
| `requirements.txt` | Dependency manifest | ~5 |
| `.env.example` | Configuration template | ~20 |
| `README.md` | Setup, testing, WSR system prompt | ~200 |

---

*Document generated: 2026-05-07. Reflects trajectory-mcp as of commit 764f82c.*
