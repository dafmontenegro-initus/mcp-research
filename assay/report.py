"""
Findings accumulator and report writer for assay.

Accumulates findings in memory, then writes three files at the end:
  findings/YYYY-MM-DD_HHMM/report.md         — human-readable, rich markdown
  findings/YYYY-MM-DD_HHMM/findings.json     — full machine-readable data
  findings/YYYY-MM-DD_HHMM/claude_context.json — AI-optimized, priority-ranked

Verdicts are binary: pass | fail. Failure reason is in `category`
(correctness | security | performance | data_quality | error_handling).

Infrastructure failures (Ollama runner crashes, unreachable evaluator) are kept
SEPARATE from real findings. They go into `infrastructure_events`, never into
`actionable_findings` or `findings.json::findings` — so a noisy Ollama run
doesn't pollute the bug report.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from llm import TestCase, Verdict

import config


@dataclass
class Finding:
    tool: str
    test_name: str
    test_description: str
    arguments: dict
    result_repr: str
    duration_ms: int
    verdict: str                    # "pass" | "fail"
    severity: str
    category: str
    summary: str
    detail: str
    is_followup: bool
    interesting: bool
    reasoning_chain: str            # evaluator's full thinking — used for root-cause analysis


@dataclass
class InfrastructureEvent:
    tool: str
    test_name: str
    arguments: dict
    duration_ms: int
    summary: str
    detail: str


class Report:
    def __init__(self) -> None:
        self._findings: list[Finding] = []
        self._infra_events: list[InfrastructureEvent] = []
        self._tool_stats: dict[str, dict] = {}
        self._started_at = time.time()
        self._lock = threading.Lock()
        ts = datetime.now().strftime("%Y-%m-%d_%H%M")
        self._dir = Path(__file__).parent / "findings" / ts
        self._dir.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        tool: str,
        case: TestCase,
        result: Any,
        duration_ms: int,
        verdict: Verdict,
    ) -> None:
        result_repr = json.dumps(result, default=str)[:500] if result is not None else "null"

        # Infrastructure failure path — do not pollute findings/tool_stats with these.
        if verdict.verdict == "unreachable":
            with self._lock:
                self._infra_events.append(InfrastructureEvent(
                    tool=tool,
                    test_name=case.name,
                    arguments=case.arguments,
                    duration_ms=duration_ms,
                    summary=verdict.summary,
                    detail=verdict.detail,
                ))
                stats = self._tool_stats.setdefault(tool, _empty_stats())
                stats["infrastructure"] = stats.get("infrastructure", 0) + 1
            return

        with self._lock:
            self._findings.append(Finding(
                tool=tool,
                test_name=case.name,
                test_description=case.description,
                arguments=case.arguments,
                result_repr=result_repr,
                duration_ms=duration_ms,
                verdict=verdict.verdict,
                severity=verdict.severity,
                category=verdict.category,
                summary=verdict.summary,
                detail=verdict.detail,
                is_followup=case.is_followup,
                interesting=verdict.interesting,
                reasoning_chain=verdict.reasoning_chain,
            ))

            stats = self._tool_stats.setdefault(tool, _empty_stats())
            stats["total"] += 1
            stats[verdict.verdict] = stats.get(verdict.verdict, 0) + 1
            stats["total_ms"] += duration_ms
            stats["max_ms"] = max(stats["max_ms"], duration_ms)

    def flush(self) -> None:
        with self._lock:
            self._write_json()
            self._write_markdown()
            self._write_claude_context()

    def finalize(self) -> Path:
        self._write_json()
        self._write_markdown()
        self._write_claude_context()
        self._update_index()
        return self._dir / "report.md"

    # ── Internal writers ───────────────────────────────────────────────────────

    def _build_summary_dict(self) -> dict:
        counts = {"pass": 0, "fail": 0}
        for f in self._findings:
            counts[f.verdict] = counts.get(f.verdict, 0) + 1
        return {
            "tools_tested": len(self._tool_stats),
            "total_tests": len(self._findings),
            **counts,
            "infrastructure_events": len(self._infra_events),
        }

    def _severity_order(self, f: Finding) -> int:
        return {"critical": 0, "major": 1, "minor": 2, "info": 3}.get(f.severity, 4)

    def _priority_rank(self, f: Finding) -> tuple:
        verdict_order = {"fail": 0, "pass": 1}.get(f.verdict, 2)
        severity_order = {"critical": 0, "major": 1, "minor": 2, "info": 3}.get(f.severity, 4)
        return (verdict_order, severity_order)

    def _update_index(self) -> None:
        index_path = self._dir.parent / "index.md"
        summary = self._build_summary_dict()
        elapsed = int(time.time() - self._started_at)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        run_dir = self._dir.name

        verdict_counts = f"✓{summary['pass']} ✗{summary['fail']}"
        if summary["infrastructure_events"]:
            verdict_counts += f" ⚙{summary['infrastructure_events']}"

        new_row = (
            f"| [{now}](/{run_dir}/report.md) "
            f"| {summary['tools_tested']} "
            f"| {summary['total_tests']} "
            f"| {verdict_counts} "
            f"| {elapsed // 60}m {elapsed % 60}s |"
        )

        if not index_path.exists():
            index_path.write_text(
                "# Assay — Run History\n\n"
                "| Run | Tools | Tests | Results | Duration |\n"
                "|-----|-------|-------|---------|----------|\n"
                + new_row + "\n"
            )
        else:
            index_path.write_text(index_path.read_text() + new_row + "\n")

    def _write_json(self) -> None:
        data = {
            "generated_at": datetime.now().isoformat(),
            "duration_s": int(time.time() - self._started_at),
            "summary": self._build_summary_dict(),
            "findings": [asdict(f) for f in self._findings],
            "infrastructure_events": [asdict(e) for e in self._infra_events],
            "tool_stats": self._tool_stats,
        }
        (self._dir / "findings.json").write_text(json.dumps(data, indent=2))

    def _write_markdown(self) -> Path:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        summary = self._build_summary_dict()
        elapsed = int(time.time() - self._started_at)
        lines: list[str] = []

        lines += [
            f"# Assay QA Report — {now}",
            "",
            f"**Duration:** {elapsed // 60}m {elapsed % 60}s  |  "
            f"**Tools tested:** {summary['tools_tested']}  |  "
            f"**Total tests:** {summary['total_tests']}",
            "",
            f"**Evaluator:** `{config.MODEL}` *(single-evaluator: intent + contract in one pass)*",
            "",
            "| Verdict | Count |",
            "|---------|-------|",
            f"| ✓ Pass | {summary['pass']} |",
            f"| ✗ Fail | {summary['fail']} |",
        ]
        if summary["infrastructure_events"]:
            lines.append(f"| ⚙ Infrastructure events (excluded from findings) | {summary['infrastructure_events']} |")
        lines.append("")

        # ── Critical & Security findings ─────────────────────────────────────────
        critical = [
            f for f in self._findings
            if f.verdict == "fail" and (f.severity in ("critical", "major") or f.category == "security")
        ]
        critical.sort(key=self._severity_order)
        if critical:
            lines += ["## Critical & Security Findings", ""]
            for f in critical:
                icon = "🔒" if f.category == "security" else "🔴" if f.severity == "critical" else "🟠"
                lines += [
                    f"### {icon} `{f.tool}` — {f.summary}",
                    f"- **Test:** {f.test_name}",
                    f"- **Args:** `{json.dumps(f.arguments)}`",
                    f"- **Category:** {f.category}  |  **Severity:** {f.severity}  |  **Duration:** {f.duration_ms}ms",
                    f"- {f.detail}",
                    "",
                ]
                lines += _reasoning_block(f.reasoning_chain)

        # ── Tool-by-tool ────────────────────────────────────────────────────────
        lines += ["## Tool-by-Tool Results", ""]
        lines += ["| Tool | Tests | ✓ | ✗ | ⚙Infra | Avg ms | Max ms |"]
        lines += ["|------|-------|---|---|--------|--------|--------|"]
        for tool, s in sorted(self._tool_stats.items()):
            avg = s["total_ms"] // s["total"] if s["total"] else 0
            lines.append(
                f"| {tool} | {s['total']} | {s['pass']} | {s['fail']} | "
                f"{s.get('infrastructure', 0)} | {avg} | {s['max_ms']} |"
            )
        lines.append("")

        # ── All findings ────────────────────────────────────────────────────────
        non_pass = [f for f in self._findings if f.verdict == "fail"]
        non_pass.sort(key=self._priority_rank)
        if non_pass:
            lines += ["## All Findings", ""]
            current_tool = None
            for f in non_pass:
                if f.tool != current_tool:
                    lines += [f"### {f.tool}", ""]
                    current_tool = f.tool
                followup_tag = " *(follow-up)*" if f.is_followup else ""
                lines += [
                    f"**✗ {f.test_name}**{followup_tag} `[{f.severity}]` `[{f.category}]`",
                    f"{f.summary}",
                    f"> Args: `{json.dumps(f.arguments)}`  Duration: {f.duration_ms}ms",
                    f"> {f.detail}",
                    "",
                ]
                lines += _reasoning_block(f.reasoning_chain)

        # ── Infrastructure events ──────────────────────────────────────────────
        if self._infra_events:
            lines += [
                "## ⚙️ Infrastructure Events",
                "",
                "> These tests could not be evaluated due to Ollama/evaluator unreachability.",
                "> They are NOT bugs in the system under test. Re-run when infrastructure is stable.",
                "",
            ]
            for e in self._infra_events:
                lines += [
                    f"- **{e.tool}** — {e.test_name}: {e.summary}",
                    f"  > Args: `{json.dumps(e.arguments)}`  Duration: {e.duration_ms}ms",
                ]
            lines.append("")

        path = self._dir / "report.md"
        path.write_text("\n".join(lines))
        return path

    def _write_claude_context(self) -> None:
        summary = self._build_summary_dict()
        elapsed = int(time.time() - self._started_at)

        non_pass = [f for f in self._findings if f.verdict == "fail"]
        non_pass.sort(key=self._priority_rank)

        actionable = []
        for rank, f in enumerate(non_pass, 1):
            actionable.append({
                "priority_rank": rank,
                "tool": f.tool,
                "test_name": f.test_name,
                "test_description": f.test_description,
                "arguments": f.arguments,
                "result_repr": f.result_repr,
                "duration_ms": f.duration_ms,
                "verdict": f.verdict,
                "severity": f.severity,
                "category": f.category,
                "summary": f.summary,
                "detail": f.detail,
                "is_followup": f.is_followup,
                "reasoning_chain": f.reasoning_chain,
            })

        tool_health = {}
        for tool, s in self._tool_stats.items():
            tool_health[tool] = {
                "tests": s["total"],
                "pass": s["pass"],
                "fail": s["fail"],
                "infrastructure_events": s.get("infrastructure", 0),
                "avg_ms": s["total_ms"] // s["total"] if s["total"] else 0,
                "max_ms": s["max_ms"],
            }

        data = {
            "schema_version": "5.0",
            "meta": {
                "generated_at": datetime.now().isoformat(),
                "duration_s": elapsed,
                "evaluator": config.MODEL,
                "totals": {
                    "tools": summary["tools_tested"],
                    "tests": summary["total_tests"],
                    "pass": summary["pass"],
                    "fail": summary["fail"],
                    "infrastructure_events": summary["infrastructure_events"],
                },
            },
            "how_to_use": (
                "Start with actionable_findings (sorted by priority: fails first, then by severity). "
                "Verdicts are binary (pass/fail); the failure type is in `category`. "
                "reasoning_chain has the evaluator's full thinking — use it for root-cause analysis. "
                "infrastructure_events are Ollama failures, NOT real bugs in the system under test."
            ),
            "actionable_findings": actionable,
            "infrastructure_events": [asdict(e) for e in self._infra_events],
            "tool_health": tool_health,
        }

        (self._dir / "claude_context.json").write_text(json.dumps(data, indent=2))


def _empty_stats() -> dict:
    return {
        "total": 0, "pass": 0, "fail": 0,
        "total_ms": 0, "max_ms": 0,
        "infrastructure": 0,
    }


def _reasoning_block(reasoning: str) -> list[str]:
    if not reasoning:
        return []
    excerpt = reasoning[:1000]
    if len(reasoning) > 1000:
        excerpt += "…"
    return [
        "<details><summary>evaluator reasoning</summary>",
        "",
        "```",
        excerpt,
        "```",
        "",
        "</details>",
        "",
    ]
