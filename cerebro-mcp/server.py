import sys

import config
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from tools.meetings import list_meetings, get_meeting_details, get_meeting_transcript
from tools.wrike import find_task, list_tasks, get_task_details, get_wrike_users

# Parse --company before FastMCP initializes so validate_company works at import time.
_company_arg = None
if "--company" in sys.argv:
    _idx = sys.argv.index("--company")
    if _idx + 1 < len(sys.argv):
        _company_arg = sys.argv[_idx + 1].upper()

config.SCOPED_COMPANY = _company_arg

mcp = FastMCP(
    "cerebro-mcp",
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
    Return the company IDs available on this server instance.

    Call this at the start of any session where the target company is not clear
    from the user's message. In single-tenant mode (server started with --company),
    returns the one scoped company. In multi-tenant mode, queries the database for
    all distinct project_ids in meetings_projects — the authoritative source of truth.
    """
    if config.SCOPED_COMPANY:
        return {"mode": "single-tenant", "companies": [config.SCOPED_COMPANY]}

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
    return {"mode": "multi-tenant", "companies": companies}


mcp.tool(annotations=_read_only)(list_companies)
mcp.tool(annotations=_read_only)(list_meetings)
mcp.tool(annotations=_read_only)(get_meeting_details)
mcp.tool(annotations=_read_only)(get_meeting_transcript)
mcp.tool(annotations=_read_only)(find_task)
mcp.tool(annotations=_read_only)(list_tasks)
mcp.tool(annotations=_read_only)(get_task_details)
mcp.tool(annotations=_read_only)(get_wrike_users)

if __name__ == "__main__":
    if config.SCOPED_COMPANY:
        print(f"[cerebro-mcp] started — scoped to company: {config.SCOPED_COMPANY}", file=sys.stderr)
    else:
        print("[cerebro-mcp] started — multi-tenant mode", file=sys.stderr)

    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=8001)
