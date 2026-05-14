"""
Main evaluation loop for assay.

Flow:
  1. Initialize MCP session (single client for discovery)
  2. Discovery phase — collect real IDs (UUIDs, ticket IDs, user emails) from NWN
  3. For each tool (parallel workers, each with its own MCP session):
       a. Generate test cases via generator LLM (config.MODEL, Alibaba Cloud)
       b. Execute each case, evaluate via dual-layer system:
            Layer 1: generator-as-judge (same model, requester perspective)
            Layer 2: independent jury (3 judges from DeepSeek AI, Google DeepMind, IBM Research)
          Final verdict = pass only if BOTH layers approve
       c. If verdict.interesting -> generate follow-ups (up to MAX_FOLLOWUPS)
  4. Write final report (report.md + findings.json + claude_context.json)

Parallelism: up to MAX_WORKERS tools run simultaneously. Each worker opens its own
MCP session. Output is buffered per tool and printed atomically on completion.
"""
from __future__ import annotations

import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from rich.console import Console
from rich.table import Table

import config
from llm import CombinedVerdict, JuryVerdict, TestCase, Verdict, evaluate_combined, generate_followups, generate_test_cases
from mcp_client import MCPClient, Tool
from report import Report

console = Console()
_print_lock = threading.Lock()

# Tools that write to external systems — skip to avoid side effects.
_SKIP_TOOLS = {"ingest_document"}

# Tools used only during discovery — not re-tested in the main loop.
_DISCOVERY_TOOLS = {"list_companies", "get_rag_health", "get_index_stats", "list_models"}

# layer_consensus display tags
_LAYER_TAG = {
    "both_pass": "",
    "both_fail": " [dim]both[/]",
    "generator_only_fail": " [dim]gen[/]",
    "jury_only_fail": " [dim]jury[/]",
}

# jury consensus display tags
_CONSENSUS_TAG = {
    "unanimous": "",
    "majority": " [dim]2/3[/]",
}


def _format_verdict(case: TestCase, verdict: "CombinedVerdict | JuryVerdict | Verdict", duration_ms: int, out: list[str]) -> None:
    if isinstance(verdict, CombinedVerdict):
        final = verdict.final_verdict
        layer_tag = _LAYER_TAG.get(verdict.layer_consensus, "")
    elif isinstance(verdict, JuryVerdict):
        final = verdict.final_verdict
        layer_tag = ""
    else:
        final = verdict.verdict
        layer_tag = ""

    icon = "[green]✓[/]" if final == "pass" else "[red]✗[/]"
    followup = " [dim](follow-up)[/]" if case.is_followup else ""
    slow = f" [yellow]{duration_ms}ms ⚡[/]" if duration_ms > config.SLOW_MS else f" [dim]{duration_ms}ms[/]"

    out.append(f"  {icon} {case.name}{followup}{slow}{layer_tag}")

    if final != "pass":
        if isinstance(verdict, CombinedVerdict):
            jury = verdict.jury
            gen = verdict.generator_judge
            jury_consensus_tag = _CONSENSUS_TAG.get(jury.consensus, "")
            category_tag = f" [dim][{verdict.final_category}][/]"

            out.append(f"     [dim]Jurado: {jury.final_summary}{jury_consensus_tag}{category_tag}[/]")
            gen_icon = "[green]✓[/]" if gen.verdict == "pass" else "[red]✗[/]"
            out.append(f"     [dim]Generador: {gen_icon} {gen.summary}[/]")

            # Show dissenting jury judges
            dissenters = [jv for jv in jury.verdicts if jv.verdict != jury.final_verdict]
            for jv in dissenters:
                short = jv.model.split(":")[0]
                out.append(f"     [dim]  ↳ {short}: {jv.verdict} — {jv.summary}[/]")
        elif isinstance(verdict, JuryVerdict):
            consensus_tag = _CONSENSUS_TAG.get(verdict.consensus, "")
            out.append(f"     [dim]{verdict.final_summary}{consensus_tag}[/]")
            dissenters = [jv for jv in verdict.verdicts if jv.verdict != verdict.final_verdict]
            for jv in dissenters:
                short = jv.model.split(":")[0]
                out.append(f"     [dim]  ↳ {short}: {jv.verdict} — {jv.summary}[/]")
        else:
            out.append(f"     [dim]{verdict.summary}[/]")


def _discovery_phase(client: MCPClient) -> dict:
    """
    Collect real IDs from NWN for use in test cases.
    Returns a dict the LLM can reference when generating test cases.
    """
    discovery: dict[str, Any] = {"companies": [], "nwn": {}}

    r = client.call_tool("list_companies", {})
    if not r.is_error and isinstance(r.raw, dict):
        discovery["companies"] = [c for c in r.raw.get("companies", []) if c]

    r = client.call_tool("list_meetings", {"company_id": "NWN", "limit": 20})
    if not r.is_error and isinstance(r.raw, dict):
        meetings = r.raw.get("meetings", [])
        discovery["nwn"]["meeting_uuids"] = [m.get("meeting_uuid") for m in meetings if m]
        discovery["nwn"]["transcript_uuids"] = [
            m.get("meeting_uuid") for m in meetings
            if m and m.get("has_transcript")
        ][:5]
        discovery["nwn"]["synthesis_uuids"] = [
            m.get("meeting_uuid") for m in meetings
            if m and m.get("has_synthesis")
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


def test_tool(client: MCPClient, tool: Tool, discovery: dict, report: Report) -> list[str]:
    """
    Run all test cases for one tool. Returns buffered output lines (Rich markup)
    for atomic printing by the caller — never writes to the shared console directly.
    """
    out: list[str] = []
    companies = discovery.get("companies", ["NWN"])

    try:
        cases = generate_test_cases(
            tool_name=tool.name,
            tool_description=tool.description,
            input_schema=tool.input_schema,
            companies=companies,
            discovery=discovery,
        )
    except Exception as e:
        out.append(f"  [red]LLM error generating test cases:[/]")
        for line in str(e).splitlines():
            out.append(f"  [dim]{line}[/]")
        return out

    out.append(f"  Generated [bold]{len(cases)}[/] test cases")

    followup_budget = config.MAX_FOLLOWUPS
    i = 0
    while i < len(cases):
        case = cases[i]
        i += 1

        call = client.call_tool(tool.name, case.arguments)

        try:
            combined = evaluate_combined(
                tool_name=tool.name,
                case=case,
                result=call.raw,
                duration_ms=call.duration_ms,
                is_error=call.is_error,
                error_message=call.error_message,
            )
        except Exception as e:
            out.append(f"  [red]Evaluation error: {e}[/]")
            combined = Verdict(
                verdict="fail", severity="minor", category="error_handling",
                summary="Evaluation failed", detail=str(e), interesting=False,
            )

        _format_verdict(case, combined, call.duration_ms, out)
        report.record(tool.name, case, call.raw, call.duration_ms, combined)

        interesting = combined.interesting if isinstance(combined, CombinedVerdict) else combined.interesting
        if interesting and followup_budget > 0:
            try:
                followups = generate_followups(tool.name, case, call.raw, combined)
                followups = followups[:followup_budget]
                cases.extend(followups)
                followup_budget -= len(followups)
                if followups:
                    out.append(f"  [dim]-> {len(followups)} follow-up(s) queued[/]")
            except Exception:
                pass

    return out


def run(tool_filter: str | None = None, dry_run: bool = False) -> None:
    console.rule("[bold magenta]assay[/] — autonomous QA agent")
    console.print(f"  Generator + Layer 1 judge: [bold]{config.MODEL}[/] [dim](Alibaba Cloud)[/]")
    judges_str = " · ".join(f"[bold]{m}[/]" for m in config.JUDGE_MODELS)
    console.print(f"  Layer 2 jury ({len(config.JUDGE_MODELS)}): {judges_str} [dim](quorum {config.JUDGE_QUORUM}/{len(config.JUDGE_MODELS)})[/]")
    console.print(f"  [dim]Verdict: pass only if BOTH layers approve[/]")

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
    console.print(
        f"\nTesting [bold]{len(tools_to_test)}[/] tools "
        f"([bold]{config.MAX_WORKERS}[/] parallel workers)...\n"
    )

    def _handle_interrupt(sig, frame):
        console.print("\n\n[yellow]Interrupted — writing partial report...[/]")
        path = report.finalize()
        console.print(f"[bold yellow]Partial report:[/] {path}")
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_interrupt)

    def _worker(tool: Tool) -> tuple[Tool, list[str]]:
        with MCPClient() as worker_client:
            worker_client.initialize()
            output = test_tool(worker_client, tool, discovery, report)
        return tool, output

    completed = 0
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as executor:
        futures = {executor.submit(_worker, tool): tool for tool in tools_to_test}
        for future in as_completed(futures):
            tool, output_lines = future.result()
            completed += 1
            with _print_lock:
                console.rule(f"[bold cyan]{tool.name}[/]")
                for line in output_lines:
                    console.print(line)
                console.print(f"  [dim]({completed}/{len(tools_to_test)} complete)[/]")
            report.flush()

    console.rule("[bold]Finalizing report[/]")
    with console.status("Writing report..."):
        path = report.finalize()
    console.print(f"\n[bold green]Report written to:[/] {path}")
    console.print(f"[dim]Also: {path.parent}/findings.json  |  {path.parent}/claude_context.json[/]")
