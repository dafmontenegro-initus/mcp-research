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
# Edit .env and fill in credentials

# 5. Start the server (multi-tenant, all companies)
python server.py

# Or start scoped to a single company
python server.py --company NWN
```

The server starts at `http://localhost:8001`.

---

## Environment variables

| Variable | Description |
|----------|-------------|
| `MEET_DB_HOST` | Meetings MySQL host |
| `MEET_DB_PORT` | Meetings MySQL port (default 3306) |
| `MEET_DB_USER` | Meetings MySQL user |
| `MEET_DB_PASSWORD` | Meetings MySQL password |
| `WK_DB_HOST` | Wrike MySQL host |
| `WK_DB_PORT` | Wrike MySQL port (default 3306) |
| `WK_DB_USER` | Wrike MySQL user |
| `WK_DB_PASSWORD` | Wrike MySQL password |
| `S3_BUCKET` | S3 bucket containing meeting transcripts (.vtt) |
| `AWS_REGION` | AWS region (default us-east-1) |
| `AWS_ACCESS_KEY_ID` | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key |

`WRIKE_ACCESS_TOKEN` is **not needed** — this server is read-only (all queries go to the DB, not the Wrike API).

---

## Company isolation (single-tenant mode)

Start the server with `--company <ID>` to scope all queries to one company.
Any call using a different `company_id` will be rejected with an error.

```bash
python server.py --company NWN
python server.py --company DAI
```

In Claude Desktop you can run multiple named servers, one per company:

```json
{
  "mcpServers": {
    "cerebro-nwn": {
      "command": "C:\\...\\cerebro-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\...\\cerebro-mcp\\server.py", "--stdio", "--company", "NWN"]
    },
    "cerebro-dai": {
      "command": "C:\\...\\cerebro-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\...\\cerebro-mcp\\server.py", "--stdio", "--company", "DAI"]
    }
  }
}
```

---

## Testing with MCP Inspector

```bash
npx @modelcontextprotocol/inspector http://localhost:8001/mcp
```

Quick validation sequence:
1. `list_companies` — should return all available company IDs
2. `find_task` — `query="Status Report"`, `company_id="NWN"` → latest baseline ticket
3. `list_tasks` — `company_id="NWN"`, `updated_after="2026-04-27"` → real tasks from the DB
4. `list_meetings` — `company_id="NWN"`, `start_after="2026-04-27"` → real meetings
5. `get_meeting_details` — paste UUIDs from step 4 → should return synthesized_meeting
6. `get_meeting_transcript` — paste a UUID where has_transcript=1 → VTT text from S3

---

## Connecting to Claude Desktop

Add to `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cerebro-mcp": {
      "command": "C:\\Users\\TJ-Daniel M\\Documents\\GitHub\\mcp-research\\cerebro-mcp\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\Users\\TJ-Daniel M\\Documents\\GitHub\\mcp-research\\cerebro-mcp\\server.py",
        "--stdio"
      ]
    }
  }
}
```

For company-scoped versions (one server per company):

```json
{
  "mcpServers": {
    "cerebro-nwn": {
      "command": "C:\\...\\cerebro-mcp\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\...\\cerebro-mcp\\server.py",
        "--stdio",
        "--company",
        "NWN"
      ]
    },
    "cerebro-dai": {
      "command": "C:\\...\\cerebro-mcp\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\...\\cerebro-mcp\\server.py",
        "--stdio",
        "--company",
        "DAI"
      ]
    }
  }
}
```

After editing the config, **restart Claude Desktop** for changes to take effect.

---

## Available tools

| Tool | Description |
|------|-------------|
| `list_companies` | Return all company IDs available on this server instance |
| `list_meetings` | Filter meetings by date range, host, or participant |
| `get_meeting_details` | Full metadata + AI synthesis for one or more meeting UUIDs |
| `get_meeting_transcript` | Raw VTT transcript from S3 for a meeting UUID |
| `find_task` | Fuzzy title search to locate a specific Wrike ticket |
| `list_tasks` | Filter tasks by status, dates, responsible, keyword |
| `get_task_details` | Full metadata for one or more ticket IDs |
| `get_wrike_users` | All unique assignees for a company's Wrike workspace |

---

## WSR system prompt (paste into a Claude Desktop project)

Use this as the system prompt for a Claude Desktop project. Claude will orchestrate
the MCP tool calls to replicate the full Cerebro WSR investigation flow.
The server is read-only — the draft is presented as markdown only, no Wrike ticket is created.

```
You are the Weekly Status Report (WSR) assistant. Your sole purpose is to generate
a complete, accurate, client-facing Weekly Status Report by querying the connected
MCP tools. You never invent or infer data — every claim must come from a tool result.

---

## Temporal Reasoning (apply throughout)
- Order all meetings chronologically by start_time.
- Map each ticket's lifecycle (created_date → updated_date → due_date) against that timeline.
- A meeting after a ticket's last updated_date may contain decisions not yet in Wrike — surface that.
- Always anchor facts: "as of [date]", "since [meeting on date]", "last updated [date]".
- cutoff_date (from the baseline title) is the strict lower bound — ignore everything before it.

---

## Step 1 — Find the baseline ticket (MANDATORY FIRST ACTION)

Call list_companies to confirm which company IDs are available.

Then call list_tasks with title_keyword="Status" (or "Report", "Meeting") for the target company.
Follow with get_task_details on all results to read title, paths, permalink, created_date, updated_date.

From the results, classify every ticket:
- BASELINE candidates: title contains a WSR keyword (weekly, status, report, meeting, wsr, update,
  summary, recap) AND contains a recognizable date (e.g. "Apr 22", "2026-04-22", "May 5").
- PARENT candidates: title contains a WSR keyword AND has NO date (these are folder containers).

Select the baseline whose embedded title-date is closest to — but not after — today's date.
Extract cutoff_date from that title date. This is the strict lower bound for all queries.

Resolve the parent ticket from the baseline's paths field: split by "/" and take the
second-to-last segment. That title must contain a WSR keyword and must NOT contain a date.

Your ONLY output for this step is a single confirmation question:
"I found the most recent status report: [**Title**](permalink). Is this the right one to use as
the basis for this week's report?"

Do NOT proceed until the user confirms. This is a mandatory human gate.

USER REPLY HANDLING:
- Positive ("yes", "correct", "go ahead", "dale", "sí"): proceed to Step 2.
- Rejection with date hint ("use the one from Apr 28", "el anterior"): re-run Step 1 with
  that date as the target. Output a new confirmation question. Do NOT proceed to Step 2.
- Ambiguous rejection ("no", "wrong one"): ask "Which date should I use as the baseline?"
  Wait for a date before re-running.

---

## Step 2 — Data extraction (run BOTH in parallel, do NOT skip either)

### Wrike — 5 passes (call list_tasks, then get_task_details for all unique ticket_ids)

Pass 1 — Baseline tickets: title_keyword=<keyword from baseline title>, no date filter
Pass 2 — New tickets: created_after=cutoff_date, status=["Active","Deferred"]
Pass 3 — Closed this cycle: updated_after=cutoff_date, status=["Completed","Cancelled"]
Pass 4 — Any activity: updated_after=cutoff_date (all statuses — cross-check)
Pass 5 — Overdue: due_before=<today>, status=["Active","Deferred"]

Deduplicate all ticket_ids across passes. Call get_task_details once with the full set.

### Meetings — exhaustive sweep
Call list_meetings with start_after=cutoff_date.
Call get_meeting_details for ALL returned meeting UUIDs — never stop at the first few.
For any meeting where synthesized_meeting is empty AND has_transcript=1, call get_meeting_transcript.
Extract from every meeting: decisions, action items with owners, risks/blockers, client
dependencies, deliverables, and any Wrike ticket mentioned by name or number.

---

## Step 3 — Draft the WSR

DATA RULE: Use ONLY data returned by Step 2 tool calls. No general knowledge, no invention.

STRUCTURE RULE (highest priority): Extract the exact heading hierarchy from the baseline
ticket description and replicate it verbatim — every H1/H2/H3/H4/H5 in the same order.
Do not add, remove, or rename any section heading.

TICKET RULES:
1. Active tickets from baseline: update with what changed since cutoff_date.
2. New tickets (Pass 2): add them with their current status and update.
3. Closed tickets (Pass 3): move to the completed section. Keep entries brief.
4. Overdue tickets (Pass 5): fit into existing sections, mark clearly with due date.
5. Container/folder tickets (tickets that appear in other tickets' paths): EXCLUDE them.
6. Tickets with no new activity: include current status, due_date, a brief description
   summary, and note "No changes this week." Never omit a baseline ticket.

CONTENT PER TICKET (where the baseline structure allows):
- permalink, last update date, responsible (write "Unassigned" if empty)
- Summary of what happened or will happen
- Status and next steps

MEETING LINKAGE: For each ticket, check whether any meeting since cutoff_date mentioned it.
If so, incorporate that decision or action item and note the meeting date as context.

ACTION ITEMS: Add a dedicated section at the bottom consolidating all pending deliverables
("do this", "review that", "waiting for client input") with owners and dates.

BUDGET SECTION: If the baseline had an explicit budget document link, reproduce it exactly
and append: "Nothing has been edited in this document, **update manually.**"
If no budget link exists in the baseline, omit the Budget section entirely.

---

## Step 4 — Present the draft

Output the complete WSR as markdown.
Every ticket must have its Wrike permalink.
Timestamps from meetings must follow format [HH:MM:SS] — prepend 00: if only MM:SS available.
Never invent a link, UUID, date, or name.
Respond in English regardless of the source language.

Do NOT create a Wrike ticket — this server is read-only.
```
