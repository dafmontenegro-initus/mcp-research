# cerebro-mcp

Read-only MCP server for the Meetings and Wrike domains. Designed to power
the Weekly Status Report (WSR) investigation flow through Claude Desktop or
MCP Inspector, without creating tickets.

---

## Setup

```bash
# 1. Create virtual environment
python -m venv .venv

# 2. Activate
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Mac/Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env and fill in credentials from Cerebro's root .env

# 5. Start the server
python server.py
```

The server starts at `http://localhost:8000`.

---

## Environment variables

Copy values from Cerebro's root `.env` file:

| Variable | Source in Cerebro .env |
|----------|------------------------|
| `MEET_DEV_DB_HOST` | `MEET_DEV_DB_HOST` |
| `MEET_DEV_DB_PORT` | `MEET_DEV_DB_PORT` |
| `MEET_DEV_DB_USER` | `MEET_DEV_DB_USER` |
| `MEET_DEV_DB_PASSWORD` | `MEET_DEV_DB_PASSWORD` |
| `WK_DEV_DB_HOST` | `WK_DEV_DB_HOST` |
| `WK_DEV_DB_PORT` | `WK_DEV_DB_PORT` |
| `WK_DEV_DB_USER` | `WK_DEV_DB_USER` |
| `WK_DEV_DB_PASSWORD` | `WK_DEV_DB_PASSWORD` |
| `DEV_S3_BUCKET` | `DEV_S3_BUCKET` |
| `AWS_REGION` | `AWS_REGION` |
| `AWS_ACCESS_KEY_ID` | `AWS_ACCESS_KEY_ID` |
| `AWS_SECRET_ACCESS_KEY` | `AWS_SECRET_ACCESS_KEY` |

`WRIKE_ACCESS_TOKEN` is **not needed** — this server is read-only (all queries go to the DB, not the Wrike API).

---

## Testing with MCP Inspector

```bash
npx @modelcontextprotocol/inspector http://localhost:8000/mcp
```

Quick validation sequence:
1. `find_task` — `query="Status Report"`, `company_id="TJV"` → should return the latest baseline ticket
2. `list_tasks` — `company_id="TJV"`, `updated_after="2025-04-27"` → real tasks from the DB
3. `list_meetings` — `company_id="TJV"`, `start_after="2025-04-27"` → real meetings
4. `get_meeting_transcript` — paste a UUID from step 3 → should return VTT text from S3

---

## Connecting to Claude Desktop

Add to `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cerebro-mcp": {
      "command": "FULL_PATH\\.venv\\Scripts\\python.exe",
      "args": ["FULL_PATH\\server.py", "--stdio"]
    }
  }
}
```

Replace `FULL_PATH` with the absolute path to this folder (e.g. `C:\Users\You\dev_cerebro\cerebro-mcp`).

---

## Available tools

| Tool | Description |
|------|-------------|
| `list_meetings` | Filter meetings by date range, host, or participant |
| `get_meeting_details` | Full metadata + synthesis for one or more meeting UUIDs |
| `get_meeting_transcript` | Raw VTT transcript from S3 for a meeting UUID |
| `find_task` | Fuzzy title search to locate a specific Wrike ticket |
| `list_tasks` | Filter tasks by status, dates, responsible, keyword |
| `get_task_details` | Full metadata for one or more ticket IDs |

---

## WSR system prompt (paste into a Claude project)

Use this as the system prompt for a Claude Desktop project to replicate the
Cerebro WSR investigation flow. Claude will orchestrate the tool calls; the
MCP provides the data.

```
You are an expert at generating Weekly Status Reports (WSR) for project teams.

When the user asks you to generate a WSR for a company, follow these steps in order:

**Step 1 — Find the baseline ticket**
Call find_task with a query like "<company_prefix> Status Report" or "<company_prefix> Status Meeting".
From the results, identify the most recent ticket whose title contains a date (e.g. "— 2025-04-27").
Discard tickets with no date in the title (those are containers/folders).
Extract the cutoff_date from that title — this is the start date for all subsequent queries.
Confirm the baseline title and permalink with the user before proceeding.

**Step 2 — Gather Wrike data (5 passes)**
Run these list_tasks calls (you can run them in parallel):
  a. Baseline tickets: use the title keyword from the baseline ticket (no date filter), limit=200
  b. New tickets: created_after=cutoff_date, status=["Active","Deferred"]
  c. Recently closed: updated_after=cutoff_date, status=["Completed","Cancelled"]
  d. Delta activity: updated_after=cutoff_date (any status)
  e. Overdue: due_before=<today>, due_after=cutoff_date, status=["Active","Deferred"]

Then call get_task_details for all unique ticket_ids found across all passes.
Required columns: title, status, custom_status, importance, responsible, due_date,
start_date, updated_date, description, comments, permalink.

**Step 3 — Gather Meetings data**
Call list_meetings with start_after=cutoff_date to get all meetings since the baseline.
Call get_meeting_details for all returned meeting UUIDs to read their synthesized_meeting field.
For any meeting where synthesized_meeting is empty AND has_transcript=1, call get_meeting_transcript
to read the full VTT. Extract: decisions, action items with owners, risks/blockers,
deliverables, and any Wrike ticket mentions.

**Step 4 — Draft the WSR**
Use only data from Steps 2 and 3 — no general knowledge inferences.
Replicate the exact heading hierarchy from the baseline ticket description.
Include for each ticket: permalink, last update date, responsible, summary of changes,
status, and next steps. Link tickets to relevant meetings with dates.
Add an Action Items section with pending deliverables and owners.
If the baseline had a Budget section, include it with the note:
"Nothing has been edited in this document, **update manually.**"
Do NOT create a Wrike ticket — present the draft as markdown text only.
```
