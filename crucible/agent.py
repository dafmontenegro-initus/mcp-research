"""
Main Reflexion loop for crucible.

Flow:
  1. Initialize MCP session
  2. Discovery phase — collect real IDs (UUIDs, ticket IDs, user emails) from NWN
  3. For each tool (fresh LLM context per tool):
       a. Generate test cases via LLM
       b. Execute each case, evaluate result
       c. If verdict.interesting → generate follow-ups (up to MAX_FOLLOWUPS)
  4. Write final report
"""
from __future__ import annotations

import json
import signal
import sys
from typing import Any

from rich.console import Console
from rich.table import Table

import config
from llm import TestCase, Verdict, evaluate_result, generate_followups, generate_test_cases
from mcp_client import MCPClient, Tool
from report import Report

console = Console()

# Tools that write to external systems — skip in QA to avoid side effects.
_SKIP_TOOLS = {"ingest_document"}

# Tools used only during discovery — tested separately in the discovery phase,
# not re-tested in the main loop.
_DISCOVERY_TOOLS = {"list_companies"}


def _print_verdict(case: TestCase, verdict: Verdict, duration_ms: int) -> None:
    icons = {"pass": "[green]✓[/]", "fail": "[red]✗[/]", "warning": "[yellow]⚠[/]", "security": "[bold red]🔒[/]"}
    icon = icons.get(verdict.verdict, "•")
    followup = " [dim](follow-up)[/]" if case.is_followup else ""
    slow = f" [yellow]{duration_ms}ms ⚡[/]" if duration_ms > config.SLOW_MS else f" [dim]{duration_ms}ms[/]"
    console.print(f"  {icon} {case.name}{followup}{slow}")
    if verdict.verdict != "pass":
        console.print(f"     [dim]{verdict.summary}[/]")


def _discovery_phase(client: MCPClient) -> dict:
    """
    Collect real IDs from NWN for use in test cases.
    Returns a dict the LLM can reference when generating test cases.
    """
    console.rule("[bold]Discovery phase[/]")
    discovery: dict[str, Any] = {"companies": [], "nwn": {}}

    # List all companies
    r = client.call_tool("list_companies", {})
    if not r.is_error and isinstance(r.raw, dict):
        discovery["companies"] = [c for c in r.raw.get("companies", []) if c]
        console.print(f"  companies: {discovery['companies']}")

    # Real NWN meeting UUIDs
    r = client.call_tool("list_meetings", {"company_id": "NWN", "limit": 5})
    if not r.is_error and isinstance(r.raw, dict):
        meetings = r.raw.get("meetings", [])
        discovery["nwn"]["meeting_uuids"] = [m.get("meeting_uuid") or m.get("uuid") for m in meetings if m]
        console.print(f"  NWN meetings: {len(meetings)} sampled")

    # Real NWN ticket IDs
    r = client.call_tool("list_tasks", {"company_id": "NWN", "limit": 5})
    if not r.is_error and isinstance(r.raw, dict):
        tasks = r.raw.get("tasks", [])
        discovery["nwn"]["ticket_ids"] = [t.get("ticket_id") or t.get("id") for t in tasks if t]
        console.print(f"  NWN tasks: {len(tasks)} sampled")

    # NWN user names (get_wrike_users returns list of name strings, not dicts)
    r = client.call_tool("get_wrike_users", {"company_id": "NWN"})
    if not r.is_error and isinstance(r.raw, dict):
        users = r.raw.get("users", [])
        discovery["nwn"]["user_names"] = [u for u in users[:5] if isinstance(u, str)]
        console.print(f"  NWN users: {len(users)} found")

    # Pick a second company for cross-tenant tests
    others = [c for c in discovery["companies"] if c != "NWN"]
    if others:
        discovery["other_company"] = others[0]
        console.print(f"  cross-tenant company: {others[0]}")

    return discovery


def test_tool(
    client: MCPClient,
    tool: Tool,
    discovery: dict,
    report: Report,
) -> None:
    console.rule(f"[bold cyan]{tool.name}[/]")
    companies = discovery.get("companies", ["NWN"])

    with console.status(f"Generating test cases for [cyan]{tool.name}[/]…"):
        try:
            cases = generate_test_cases(
                tool_name=tool.name,
                tool_description=tool.description,
                input_schema=tool.input_schema,
                companies=companies,
                discovery=discovery,
            )
        except Exception as e:
            console.print(f"  [red]LLM error generating test cases:[/]")
            for line in str(e).splitlines():
                console.print(f"  [dim]{line}[/]")
            return

    console.print(f"  Generated [bold]{len(cases)}[/] test cases")

    followup_budget = config.MAX_FOLLOWUPS
    i = 0
    while i < len(cases):
        case = cases[i]
        i += 1

        # Execute
        call = client.call_tool(tool.name, case.arguments)

        # Evaluate
        try:
            verdict = evaluate_result(
                tool_name=tool.name,
                case=case,
                result=call.raw,
                duration_ms=call.duration_ms,
                is_error=call.is_error,
                error_message=call.error_message,
            )
        except Exception as e:
            console.print(f"  [red]LLM evaluation error: {e}[/]")
            verdict = Verdict(
                verdict="warning", severity="minor", category="correctness",
                summary="Evaluation failed", detail=str(e), interesting=False,
            )

        _print_verdict(case, verdict, call.duration_ms)
        report.record(tool.name, case, call.raw, call.duration_ms, verdict)

        # Reflexion: add follow-ups if interesting and budget allows
        if verdict.interesting and followup_budget > 0:
            try:
                followups = generate_followups(tool.name, case, call.raw, verdict)
                followups = followups[:followup_budget]
                cases.extend(followups)
                followup_budget -= len(followups)
                if followups:
                    console.print(f"  [dim]→ {len(followups)} follow-up(s) queued[/]")
            except Exception:
                pass


def run(tool_filter: str | None = None, dry_run: bool = False) -> None:
    console.rule("[bold magenta]crucible[/] — autonomous QA agent")

    with MCPClient() as client:
        with console.status("Initializing MCP session…"):
            info = client.initialize()
        console.print(f"  Connected to [bold]{info.get('serverInfo', {}).get('name', '?')}[/] "
                      f"v{info.get('serverInfo', {}).get('version', '?')}")

        with console.status("Listing tools…"):
            tools = client.list_tools()
        console.print(f"  {len(tools)} tools available")

        if dry_run:
            console.print("\n[bold yellow]Dry run — stopping here.[/]")
            console.print("Tools found:")
            t = Table("Name", "Parameters")
            for tool in tools:
                params = list(tool.input_schema.get("properties", {}).keys())
                t.add_row(tool.name, ", ".join(params) or "(none)")
            console.print(t)
            return

        discovery = _discovery_phase(client)

        report = Report()

        tools_to_test = [
            t for t in tools
            if t.name not in _SKIP_TOOLS
            and t.name not in _DISCOVERY_TOOLS
            and (tool_filter is None or t.name == tool_filter)
        ]
        console.print(f"\nTesting [bold]{len(tools_to_test)}[/] tools…\n")

        # Graceful Ctrl+C: write partial report and exit cleanly.
        def _handle_interrupt(sig, frame):
            console.print("\n\n[yellow]Interrupted — writing partial report…[/]")
            path = report.finalize()
            console.print(f"[bold yellow]Partial report:[/] {path}")
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_interrupt)

        for tool in tools_to_test:
            test_tool(client, tool, discovery, report)
            report.flush()  # persist after every tool — partial runs are never lost

        console.rule("[bold]Finalizing report[/]")
        with console.status("Writing report…"):
            path = report.finalize()
        console.print(f"\n[bold green]Report written to:[/] {path}")
