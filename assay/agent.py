"""
Main evaluation loop for assay.

Flow:
  1. Initialize MCP session (single client for discovery)
  2. Discovery phase — collect real IDs from NWN
  3. For each tool (sequential):
       a. Generate test cases via the generator (qwen3.6:27b)
       b. For each case: call the tool, then evaluate the result with the
          same model in evaluator mode (both intent + contract in one pass)
       c. If verdict.interesting → generate follow-ups (up to MAX_FOLLOWUPS)
  4. Write final report (report.md + findings.json + claude_context.json)
"""
from __future__ import annotations

import signal
import sys
from typing import Any

from rich.console import Console
from rich.table import Table

import config
from llm import TestCase, Verdict, evaluate, generate_followups, generate_test_cases
from mcp_client import MCPClient, Tool
from report import Report

console = Console()

# Tools that write to external systems — skip to avoid side effects.
_SKIP_TOOLS = {"ingest_document"}

# Tools used only during discovery — not re-tested in the main loop.
_DISCOVERY_TOOLS = {"list_companies", "get_rag_health", "get_index_stats", "list_models"}


def _live(tool_name: str, msg: str) -> None:
    """Print a tool-prefixed progress line."""
    console.print(f"[dim cyan]\\[{tool_name}][/] {msg}")


def _print_verdict(tool_name: str, case: TestCase, verdict: Verdict, duration_ms: int) -> None:
    """Print the final verdict line(s) for one test case."""
    final = verdict.verdict
    if final == "pass":
        icon = "[green]✓[/]"
    elif final == "unreachable":
        icon = "[yellow]⚙[/]"
    else:
        icon = "[red]✗[/]"

    followup = " [dim](follow-up)[/]" if case.is_followup else ""
    slow = f" [yellow]{duration_ms}ms ⚡[/]" if duration_ms > config.SLOW_MS else f" [dim]{duration_ms}ms[/]"
    category_tag = f" [dim][{verdict.category}][/]" if final != "pass" else ""

    _live(tool_name, f"{icon} {case.name}{followup}{slow}{category_tag}")

    if final == "unreachable":
        _live(tool_name, f"   [yellow]Infrastructure failure (excluded from findings):[/] [dim]{verdict.summary}[/]")
        return

    if final != "pass":
        _live(tool_name, f"   [dim]{verdict.summary}[/]")


def _discovery_phase(client: MCPClient) -> dict:
    """Collect real IDs from NWN for use in test cases."""
    discovery: dict[str, Any] = {"companies": [], "nwn": {}}

    r = client.call_tool("list_companies", {})
    if not r.is_error and isinstance(r.raw, dict):
        discovery["companies"] = [c for c in r.raw.get("companies", []) if c]

    r = client.call_tool("list_meetings", {"company_id": "NWN", "limit": 20})
    if not r.is_error and isinstance(r.raw, dict):
        meetings = r.raw.get("meetings", [])
        discovery["nwn"]["meeting_uuids"] = [m.get("meeting_uuid") for m in meetings if m]
        discovery["nwn"]["transcript_uuids"] = [
            m.get("meeting_uuid") for m in meetings if m and m.get("has_transcript")
        ][:5]
        discovery["nwn"]["synthesis_uuids"] = [
            m.get("meeting_uuid") for m in meetings if m and m.get("has_synthesis")
        ][:5]

    r = client.call_tool("list_tasks", {"company_id": "NWN", "limit": 5})
    if not r.is_error and isinstance(r.raw, dict):
        tasks = r.raw.get("tasks", [])
        discovery["nwn"]["ticket_ids"] = [t.get("ticket_id") or t.get("id") for t in tasks if t]

    r = client.call_tool("get_wrike_users", {"company_id": "NWN"})
    if not r.is_error and isinstance(r.raw, dict):
        users = r.raw.get("users", [])
        discovery["nwn"]["user_names"] = [u for u in users[:5] if isinstance(u, str)]

    others = [c for c in discovery["companies"] if c != "NWN"]
    if others:
        other = others[0]
        discovery["other_company"] = other
        r = client.call_tool("list_meetings", {"company_id": other, "limit": 5})
        if not r.is_error and isinstance(r.raw, dict):
            other_meetings = r.raw.get("meetings", [])
            discovery.setdefault("other", {})["meeting_uuids"] = [
                m.get("meeting_uuid") for m in other_meetings if m
            ]

    r = client.call_tool("get_rag_health", {})
    if not r.is_error and isinstance(r.raw, dict):
        rag_ok = r.raw.get("rag_service", {}).get("available", False)
        if not rag_ok:
            console.print("  [yellow]⚠ RAG service DOWN — search tools will fail[/]")

    return discovery


def test_tool(client: MCPClient, tool: Tool, discovery: dict, report: Report) -> None:
    """Run all test cases for one tool, printing progress live."""
    companies = discovery.get("companies", ["NWN"])

    _live(tool.name, "[dim]Generating test cases…[/]")
    try:
        cases = generate_test_cases(
            tool_name=tool.name,
            tool_description=tool.description,
            input_schema=tool.input_schema,
            companies=companies,
            discovery=discovery,
        )
    except Exception as e:
        _live(tool.name, f"[red]LLM error generating test cases: {e}[/]")
        return

    _live(tool.name, f"Generated [bold]{len(cases)}[/] test cases")

    followup_budget = config.MAX_FOLLOWUPS
    i = 0
    while i < len(cases):
        case = cases[i]
        i += 1
        total = len(cases)

        _live(tool.name, f"([bold]{i}/{total}[/]) {case.name} [dim]— calling tool…[/]")
        call = client.call_tool(tool.name, case.arguments)

        _live(tool.name, f"([bold]{i}/{total}[/]) {case.name} [dim]— evaluating…[/]")
        try:
            verdict = evaluate(
                tool_name=tool.name,
                case=case,
                result=call.raw,
                duration_ms=call.duration_ms,
                is_error=call.is_error,
                error_message=call.error_message,
            )
        except Exception as e:
            _live(tool.name, f"([bold]{i}/{total}[/]) [red]Evaluation error: {e}[/]")
            # Route to infrastructure_events, not findings — an evaluator crash
            # is an infra problem, not a bug in the system under test. Mirrors
            # the contract that llm.evaluate() honors on its own failure paths.
            verdict = Verdict(
                verdict="unreachable", severity="info", category="infrastructure",
                summary=f"Evaluator raised: {str(e)[:80]}",
                detail=str(e), interesting=False,
            )

        _print_verdict(tool.name, case, verdict, call.duration_ms)
        report.record(tool.name, case, call.raw, call.duration_ms, verdict)

        is_infra = verdict.verdict == "unreachable"
        if verdict.interesting and followup_budget > 0 and not is_infra:
            try:
                followups = generate_followups(tool.name, case, call.raw, verdict)
                followups = followups[:followup_budget]
                cases.extend(followups)
                followup_budget -= len(followups)
                if followups:
                    _live(tool.name, f"[dim]-> {len(followups)} follow-up(s) queued[/]")
            except Exception as e:
                _live(tool.name, f"[dim yellow]follow-up generation failed: {e}[/]")


def run(tool_filter: str | None = None, dry_run: bool = False) -> None:
    console.rule("[bold magenta]assay[/] — autonomous QA agent")
    console.print(f"  Evaluator: [bold]{config.MODEL}[/] [dim](Alibaba Cloud, single-evaluator: intent + contract in one pass)[/]")
    console.print(f"  [dim]Binary pass/fail; category explains failures.[/]")

    with MCPClient() as client:
        with console.status("Initializing MCP session..."):
            info = client.initialize()
        console.print(f"  Connected to [bold]{info.get('serverInfo', {}).get('name', '?')}[/] "
                      f"v{info.get('serverInfo', {}).get('version', '?')}")

        with console.status("Listing tools..."):
            tools = client.list_tools()
        console.print(f"  {len(tools)} tools available")

        if dry_run:
            console.print("\n[bold yellow]Dry run — stopping here.[/]")
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
    def _handle_interrupt(sig, frame):
        console.print("\n\n[yellow]Interrupted — writing partial report...[/]")
        path = report.finalize()
        console.print(f"[bold yellow]Partial report:[/] {path}")
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_interrupt)

    for idx, tool in enumerate(tools_to_test, 1):
        console.rule(f"[bold cyan]{tool.name}[/] [dim]({idx}/{len(tools_to_test)})[/]")
        with MCPClient() as worker_client:
            worker_client.initialize()
            test_tool(worker_client, tool, discovery, report)
        report.flush()

    console.rule("[bold]Finalizing report[/]")
    with console.status("Writing report..."):
        path = report.finalize()
    console.print(f"\n[bold green]Report written to:[/] {path}")
    console.print(f"[dim]Also: {path.parent}/findings.json  |  {path.parent}/claude_context.json[/]")
