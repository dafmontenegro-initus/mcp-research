from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from tools.meetings import list_meetings, get_meeting_details, get_meeting_transcript
from tools.wrike import find_task, list_tasks, get_task_details, get_wrike_users

mcp = FastMCP(
    "cerebro-mcp",
    instructions=(
        "Read-only MCP server for Meetings and Wrike data. "
        "Designed to power the Weekly Status Report (WSR) investigation flow. "
        "All tools require company_id. Company aliases are resolved automatically "
        "(e.g. 'TJV' → 'NWN'). No data is modified — all operations are read-only."
    ),
)

_read_only = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

mcp.tool(annotations=_read_only)(list_meetings)
mcp.tool(annotations=_read_only)(get_meeting_details)
mcp.tool(annotations=_read_only)(get_meeting_transcript)
mcp.tool(annotations=_read_only)(find_task)
mcp.tool(annotations=_read_only)(list_tasks)
mcp.tool(annotations=_read_only)(get_task_details)
mcp.tool(annotations=_read_only)(get_wrike_users)

if __name__ == "__main__":
    import sys
    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=8001)
