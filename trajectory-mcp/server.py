import sys
import time
from pathlib import Path

import config
from fastmcp import FastMCP

_VERSION = (Path(__file__).parent / "VERSION").read_text().strip()
from mcp.types import ToolAnnotations
from auth import BearerAuthMiddleware

from tools.bamboohr import get_anniversaries, get_birthdays, get_company_holidays, get_time_off
from tools.meetings import (
    list_meetings, get_meeting_details, get_meeting_transcript,
    search_meetings, get_meeting_participants, get_meeting_chat,
    summarize_transcript_for_ticket, get_meeting_ticket_links,
)
from tools.wrike import (
    find_task, list_tasks, get_task_details, get_wrike_users,
    search_tasks, get_task_attachment_content, ingest_document,
    get_project_timeline,
)

mcp = FastMCP(
    "Trajectory",
    version=_VERSION,
    middleware=[BearerAuthMiddleware()],
    instructions=(
        "Read-only MCP server for Meetings and Wrike data. "
        "Designed to power the Weekly Status Report (WSR) investigation flow. "
        "All tools require a company_id — call list_companies() first if unsure which to use. "
        "No data is modified — all operations are read-only."
    ),
)

_read_only = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def list_companies() -> dict:
    """
    Return all company IDs available on this server.

    Queries the database for all distinct project_ids — the authoritative source
    of truth. Call this at the start of any session where the target company is
    not known from the user's message.
    """
    from db import get_meet_conn
    conn = get_meet_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT project_id FROM meetings_assets.meetings_projects "
                "ORDER BY project_id ASC"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    companies = [row["project_id"] for row in rows]
    return {"companies": companies}


import functools
import traceback


def _logged(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.monotonic()
        params = {**dict(zip(fn.__code__.co_varnames, args)), **kwargs}
        print(f"[tool] {fn.__name__} called with {params}", file=sys.stderr, flush=True)
        try:
            result = fn(*args, **kwargs)
        except Exception:
            elapsed = (time.monotonic() - t0) * 1000
            print(f"[tool] {fn.__name__} EXCEPTION ({elapsed:.0f}ms):", file=sys.stderr, flush=True)
            traceback.print_exc(file=sys.stderr)
            raise
        elapsed = (time.monotonic() - t0) * 1000
        is_error = isinstance(result, dict) and "error" in result
        if is_error:
            print(f"[tool] {fn.__name__} ERROR ({elapsed:.0f}ms): {result['error']}", file=sys.stderr, flush=True)
        else:
            print(f"[tool] {fn.__name__} OK ({elapsed:.0f}ms)", file=sys.stderr, flush=True)
        try:
            import trace_db
            trace_db.record(fn.__name__, params, result, elapsed, ok=not is_error)
        except Exception:
            pass
        return result
    return wrapper


def _register(fn):
    mcp.tool(annotations=_read_only)(_logged(fn))


_register(list_companies)
_register(get_time_off)
_register(get_birthdays)
_register(get_anniversaries)
_register(get_company_holidays)
_register(list_meetings)
_register(get_meeting_details)
_register(get_meeting_transcript)
_register(search_meetings)
_register(get_meeting_participants)
_register(get_meeting_chat)
_register(summarize_transcript_for_ticket)
_register(get_meeting_ticket_links)
_register(find_task)
_register(list_tasks)
_register(get_task_details)
_register(get_wrike_users)
_register(search_tasks)
_register(get_task_attachment_content)
_register(ingest_document)
_register(get_project_timeline)

if __name__ == "__main__":
    print("[trajectory-mcp] started — multi-tenant mode", file=sys.stderr)

    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
    else:
        _port = 8080
        if "--port" in sys.argv:
            _pidx = sys.argv.index("--port")
            if _pidx + 1 < len(sys.argv):
                _port = int(sys.argv[_pidx + 1])
        mcp.run(transport="streamable-http", host="0.0.0.0", port=_port)
