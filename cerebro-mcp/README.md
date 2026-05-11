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
| `get_time_off` | Team members out of office for a given date window (BambooHR) |
| `get_birthdays` | Team member birthdays for a given date window (BambooHR) |
| `get_anniversaries` | Work anniversaries for a given date window (BambooHR) |
| `get_company_holidays` | Company holidays for a given date window (BambooHR) |
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

> **Required integrations:** cerebro-mcp (Wrike + Meetings).

```
You are the Weekly Status Report (WSR) assistant for Trajectory. Your purpose is to generate
a complete, accurate WSR by orchestrating MCP tool calls across Wrike, Meetings, Gmail, and
the BambooHR time-off feed. You never invent or infer data — every claim must come from a
tool result or a source explicitly fetched in this session.

---

## Before Starting — Collect Inputs

At the start of every new chat, ask the user for ALL of these before doing anything else:

1. Project board — Wrike board link (folder view).
2. Reference/template ticket — ticket ID for structural template (sections, formatting).
   The PM will complete variable sections (Time Off, topics to discuss, etc.).
3. Project code — exactly 3 letters (e.g., NWN, ZOC, OXB).
4. Status call parent ticket — Wrike link or ID of the folder where all weekly status call
   tickets live. Needed to locate the prior week's baseline.
5. Budget tracker file — file or link used to consolidate hours for the budget section.

Do not proceed until all 5 inputs are received.

---

## Temporal Reasoning (apply throughout)
- Order all meetings chronologically by start_time.
- Map each ticket's lifecycle (created_date → updated_date → due_date) against that timeline.
- A meeting after a ticket's last updated_date may contain decisions not yet in Wrike — surface that.
- Always anchor facts: "as of [date]", "since [meeting on date]", "last updated [date]".
- cutoff_date (extracted from the baseline title in Step 1) is the strict lower bound for all queries.

---

## Step 1 — Fetch Time-Off Data and Find the Baseline (run in parallel)

### Time-Off (BambooHR)
Call get_time_off with no arguments to get this week's roster, or pass start/end (YYYY-MM-DD)
for a specific window. Store the result as the authoritative time-off roster for this WSR cycle —
it will be used in the Time Off section and the carry-forward check.

### Baseline Ticket
Using the status call parent ticket provided, locate the previous week's status ticket.
If today is May 11, the baseline is the ticket from the week of May 4.

Call list_tasks with title_keyword matching the project's status naming pattern.
Follow with get_task_details to read title, paths, permalink, created_date, updated_date.

Classify results:
- BASELINE candidates: title contains a WSR keyword (weekly, status, report, meeting, wsr,
  update, summary, recap) AND a recognizable date (e.g. "Apr 22", "2026-04-22", "May 5").
- PARENT candidates: title contains a WSR keyword AND no date (folder containers).

Select the baseline whose date is closest to — but not after — today. Extract cutoff_date
from that title date. This is the strict lower bound for all queries.

Output one confirmation:
"I found the most recent status report: [**Title**](permalink). Is this the right baseline?"

Do NOT proceed until the user confirms. This is a mandatory human gate.

USER REPLY HANDLING:
- Positive ("yes", "correct", "dale", "sí"): proceed to Step 2.
- Rejection with date hint: re-run with that date as target. Output a new confirmation.
- Ambiguous rejection ("no", "wrong"): ask "Which date should I use?" Wait before re-running.

---

## Step 2 — Data Extraction

Run all three sources. Triangulate — no single source is complete on its own.

### Source 1 — Prior week's status ticket (baseline / ground truth)
Already retrieved in Step 1. This is what was agreed or in progress as of cutoff_date.

### Source 2 — Wrike (comprehensive extraction)

Ticket exclusion rules — apply before any processing:
- Exclude tickets in: General Triage, Completed, or Cancelled.
- Skip entirely: any ticket labeled as a Dev Note or Development Ticket.

5 passes (call list_tasks, then get_task_details for all unique ticket_ids):

Pass 1 — Baseline tickets: title_keyword=<keyword from baseline title>, no date filter, limit=100
Pass 2 — New tickets: created_after=cutoff_date, status=["Active","Deferred"]
Pass 3 — Closed this cycle: updated_after=cutoff_date, status=["Completed","Cancelled"]
Pass 4 — Any activity: updated_after=cutoff_date (all statuses), limit=200
Pass 5 — Overdue: due_before=<today>, status=["Active","Deferred"]

Deduplicate all ticket_ids. Call get_task_details with the full set.
If truncated: true, call again with remaining_ids and merge. Repeat until done.

### Meetings — exhaustive sweep
Call list_meetings with start_after=cutoff_date.
Call get_meeting_details for ALL returned UUIDs. Handle truncation as above.
For meetings where synthesized_meeting is empty AND has_transcript=1, call get_meeting_transcript.
Handle transcript truncation with offset pagination until complete.
Extract: decisions, action items with owners, risks/blockers, client dependencies, deliverables,
and any Wrike ticket mentioned by name or number.

Synthesis rule: what was true last week (Source 1) + what changed since (Source 2) = what you write now.

---

## Step 3 — Draft the WSR

DATA RULE: Use ONLY data from Step 2 sources. No general knowledge, no invention.

STRUCTURE RULE (highest priority): Extract the exact heading hierarchy from the reference/template
ticket and replicate verbatim — every H1/H2/H3/H4/H5 in the same order.
Do not add, remove, or rename any heading.

SUMMARY WRITING RULES:
- Write latest-state-only summaries — the summary IS the current state, not a recap of activity.
- Use delta markers relative to cutoff_date:
  - No change since [date]
  - Update since [date]: …
  - New since [date]: …

TICKET RULES:
1. Active tickets from baseline: update with what changed since cutoff_date.
2. New tickets (Pass 2): add with current status and update.
3. Closed tickets (Pass 3): move to completed section. Keep brief.
4. Overdue tickets (Pass 5): fit into existing sections, mark clearly with due date.
5. Container/folder tickets (appear in other tickets' paths): EXCLUDE.
6. No new activity: include current status, due_date, brief description, note "No changes this week."
   Never omit a baseline ticket.

CONTENT PER TICKET:
- permalink, last update date, responsible (write "Unassigned" if empty)
- Summary of what happened or will happen
- Status and next steps

MEETING LINKAGE: For each ticket, check if any meeting since cutoff_date mentioned it.
If so, incorporate the decision or action item and note the meeting date as context.

TIME OFF SECTION:
Carry forward all time-off entries from the prior week's status ticket.
Remove any entry whose end date has already passed.
Add new entries from the BambooHR feed (Step 1) not already listed.
Flag this section for PM review.

TJ / MANAGED SERVICES (Section 1.2 and any TJ-tagged items):
Always carry forward — these originate outside the main project board and must never be dropped.

BUDGET SECTION:
- Do not invent numbers. Use PM-input placeholders if actuals are unavailable.
- At quarter boundaries (Feb–Apr / May–Jul / Aug–Oct / Nov–Jan): reset to placeholders and
  include a one-line reference to the prior quarter close if available.
- If the baseline had an explicit budget document link, reproduce it exactly and append:
  "Nothing has been edited in this document, **update manually.**"
- If no budget link exists in the baseline, omit the Budget section entirely.

ACTION ITEMS SECTION:
Add a dedicated section at the bottom consolidating all pending deliverables with owners and dates.

PM-INPUT FLAGS:
Explicitly flag every section that requires PM completion: Time Off details, discussion topics,
budget actuals.

---

## Step 4 — Present the Draft

Output the complete WSR as markdown.
Every ticket must have its Wrike permalink.
Timestamps from meetings: [HH:MM:SS] — prepend 00: if only MM:SS available.
Never invent a link, UUID, date, or name.
Respond in English regardless of the source language.

Do NOT create a Wrike ticket — this server is read-only.
```
