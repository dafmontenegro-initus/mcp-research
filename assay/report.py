"""
Findings accumulator and report writer for assay.

Accumulates findings in memory, then writes two files at the end:
  findings/YYYY-MM-DD_HHMM/report.md   — human-readable
  findings/YYYY-MM-DD_HHMM/findings.json — machine-readable
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from llm import TestCase, Verdict


@dataclass
class Finding:
    tool: str
    test_name: str
    test_description: str
    arguments: dict
    result_repr: str
    duration_ms: int
    verdict: str
    severity: str
    category: str
    summary: str
    detail: str
    is_followup: bool


class Report:
    def __init__(self) -> None:
        self._findings: list[Finding] = []
        self._tool_stats: dict[str, dict] = {}
        self._started_at = time.time()
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
        ))

        stats = self._tool_stats.setdefault(tool, {
            "total": 0, "pass": 0, "fail": 0, "warning": 0, "security": 0,
            "total_ms": 0, "max_ms": 0,
        })
        stats["total"] += 1
        stats[verdict.verdict] = stats.get(verdict.verdict, 0) + 1
        stats["total_ms"] += duration_ms
        stats["max_ms"] = max(stats["max_ms"], duration_ms)

    def flush(self) -> None:
        """Write current state to disk — called after each tool so partial runs are preserved."""
        self._write_json()
        self._write_markdown()

    def finalize(self) -> Path:
        self._write_json()
        md_path = self._write_markdown()
        self._update_index()
        return md_path

    def _update_index(self) -> None:
        """Append this run's summary to findings/index.md."""
        index_path = self._dir.parent / "index.md"
        summary = self._build_summary_dict()
        elapsed = int(time.time() - self._started_at)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        run_dir = self._dir.name

        verdict_counts = (
            f"✓{summary['pass']} "
            f"✗{summary['fail']} "
            f"⚠{summary['warning']} "
            f"🔒{summary['security']}"
        )

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
            "tool_stats": self._tool_stats,
        }
        (self._dir / "findings.json").write_text(json.dumps(data, indent=2))

    def _build_summary_dict(self) -> dict:
        counts: dict[str, int] = {"pass": 0, "fail": 0, "warning": 0, "security": 0}
        for f in self._findings:
            counts[f.verdict] = counts.get(f.verdict, 0) + 1
        return {
            "tools_tested": len(self._tool_stats),
            "total_tests": len(self._findings),
            **counts,
        }

    def _severity_order(self, f: Finding) -> int:
        return {"critical": 0, "major": 1, "minor": 2, "info": 3}.get(f.severity, 4)

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
            "| Verdict | Count |",
            "|---------|-------|",
            f"| ✓ Pass | {summary['pass']} |",
            f"| ✗ Fail | {summary['fail']} |",
            f"| ⚠ Warning | {summary['warning']} |",
            f"| 🔒 Security | {summary['security']} |",
            "",
        ]

        # Critical and security findings first
        critical = [f for f in self._findings if f.severity in ("critical", "major") or f.verdict == "security"]
        critical.sort(key=self._severity_order)
        if critical:
            lines += ["## Critical & Security Findings", ""]
            for f in critical:
                icon = "🔒" if f.verdict == "security" else "🔴" if f.severity == "critical" else "🟠"
                lines += [
                    f"### {icon} `{f.tool}` — {f.summary}",
                    f"- **Test:** {f.test_name}",
                    f"- **Args:** `{json.dumps(f.arguments)}`",
                    f"- **Category:** {f.category}  |  **Duration:** {f.duration_ms}ms",
                    f"- {f.detail}",
                    "",
                ]

        # Per-tool summary table
        lines += ["## Tool-by-Tool Results", ""]
        lines += ["| Tool | Tests | ✓ Pass | ✗ Fail | ⚠ Warn | 🔒 Sec | Avg ms | Max ms |"]
        lines += ["|------|-------|--------|--------|--------|--------|--------|--------|"]
        for tool, s in sorted(self._tool_stats.items()):
            avg = s["total_ms"] // s["total"] if s["total"] else 0
            lines.append(
                f"| {tool} | {s['total']} | {s['pass']} | {s['fail']} | "
                f"{s['warning']} | {s.get('security', 0)} | {avg} | {s['max_ms']} |"
            )
        lines.append("")

        # All non-pass findings
        non_pass = [f for f in self._findings if f.verdict != "pass"]
        non_pass.sort(key=self._severity_order)
        if non_pass:
            lines += ["## All Findings (non-pass)", ""]
            current_tool = None
            for f in non_pass:
                if f.tool != current_tool:
                    lines += [f"### {f.tool}", ""]
                    current_tool = f.tool
                icon = {"fail": "✗", "warning": "⚠", "security": "🔒"}.get(f.verdict, "•")
                followup_tag = " *(follow-up)*" if f.is_followup else ""
                lines += [
                    f"**{icon} {f.test_name}**{followup_tag} `[{f.severity}]`",
                    f"{f.summary}",
                    f"> Args: `{json.dumps(f.arguments)}`  Duration: {f.duration_ms}ms",
                    f"> {f.detail}",
                    "",
                ]

        path = self._dir / "report.md"
        path.write_text("\n".join(lines))
        return path
