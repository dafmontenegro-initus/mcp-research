"""
Findings accumulator and report writer for assay.

Accumulates findings in memory, then writes three files at the end:
  findings/YYYY-MM-DD_HHMM/report.md         — human-readable, rich markdown
  findings/YYYY-MM-DD_HHMM/findings.json     — full machine-readable data
  findings/YYYY-MM-DD_HHMM/claude_context.json — AI-optimized, priority-ranked,
                                                  includes judge reasoning chains

Human report sections:
  - Jury configuration (models + quorum)
  - Summary table with consensus breakdown
  - Critical & Security findings
  - Judge Disagreements (split/majority — likely false positives)
  - Tool-by-tool results table
  - All non-pass findings with per-judge breakdown and reasoning

claude_context.json design:
  Optimized for Claude Code to consume, prioritize, and act on findings.
  actionable_findings: sorted by priority (unanimous first, then severity).
  split_verdicts: lowest confidence — review before acting.
  reasoning_chain: full model thinking per judge — use for root-cause analysis.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from llm import JudgeVerdict, JuryVerdict, TestCase, Verdict

import config


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
    consensus: str                  # "unanimous" | "majority" | "split"
    judge_verdicts: list[dict]      # [{model, verdict, severity, category, summary, detail, interesting, reasoning_chain}, ...]
    interesting: bool


class Report:
    def __init__(self) -> None:
        self._findings: list[Finding] = []
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
        jury: "JuryVerdict | Verdict",
    ) -> None:
        result_repr = json.dumps(result, default=str)[:500] if result is not None else "null"

        if isinstance(jury, JuryVerdict):
            verdict = jury.final_verdict
            severity = jury.final_severity
            category = jury.final_category
            summary = jury.final_summary
            detail = jury.final_detail
            consensus = jury.consensus
            interesting = jury.interesting
            judge_verdicts = [asdict(jv) for jv in jury.verdicts]
        else:
            verdict = jury.verdict
            severity = jury.severity
            category = jury.category
            summary = jury.summary
            detail = jury.detail
            consensus = "unanimous"
            interesting = jury.interesting
            judge_verdicts = []

        with self._lock:
            self._findings.append(Finding(
                tool=tool,
                test_name=case.name,
                test_description=case.description,
                arguments=case.arguments,
                result_repr=result_repr,
                duration_ms=duration_ms,
                verdict=verdict,
                severity=severity,
                category=category,
                summary=summary,
                detail=detail,
                is_followup=case.is_followup,
                consensus=consensus,
                judge_verdicts=judge_verdicts,
                interesting=interesting,
            ))

            stats = self._tool_stats.setdefault(tool, {
                "total": 0, "pass": 0, "fail": 0, "warning": 0, "security": 0,
                "total_ms": 0, "max_ms": 0,
                "unanimous_failures": 0, "majority_failures": 0, "split_verdicts": 0,
            })
            stats["total"] += 1
            stats[verdict] = stats.get(verdict, 0) + 1
            stats["total_ms"] += duration_ms
            stats["max_ms"] = max(stats["max_ms"], duration_ms)
            if verdict != "pass":
                if consensus == "unanimous":
                    stats["unanimous_failures"] += 1
                elif consensus == "majority":
                    stats["majority_failures"] += 1
                else:
                    stats["split_verdicts"] += 1

    def flush(self) -> None:
        """Write current state to disk — called after each tool so partial runs are preserved."""
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
        counts: dict[str, int] = {"pass": 0, "fail": 0, "warning": 0, "security": 0}
        consensus_counts = {"unanimous": 0, "majority": 0, "split": 0}
        for f in self._findings:
            counts[f.verdict] = counts.get(f.verdict, 0) + 1
            consensus_counts[f.consensus] = consensus_counts.get(f.consensus, 0) + 1
        unanimous_non_pass = sum(
            1 for f in self._findings if f.verdict != "pass" and f.consensus == "unanimous"
        )
        return {
            "tools_tested": len(self._tool_stats),
            "total_tests": len(self._findings),
            **counts,
            "consensus": consensus_counts,
            "unanimous_non_pass": unanimous_non_pass,
        }

    def _severity_order(self, f: Finding) -> int:
        return {"critical": 0, "major": 1, "minor": 2, "info": 3}.get(f.severity, 4)

    def _priority_rank(self, f: Finding) -> tuple:
        """Sort key: unanimous > majority > split, then by severity."""
        consensus_order = {"unanimous": 0, "majority": 1, "split": 2}.get(f.consensus, 3)
        verdict_order = {"security": 0, "fail": 1, "warning": 2, "pass": 3}.get(f.verdict, 4)
        severity_order = {"critical": 0, "major": 1, "minor": 2, "info": 3}.get(f.severity, 4)
        return (consensus_order, verdict_order, severity_order)

    def _update_index(self) -> None:
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

    def _write_markdown(self) -> Path:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        summary = self._build_summary_dict()
        elapsed = int(time.time() - self._started_at)
        judges = getattr(config, "JUDGE_MODELS", [])
        quorum = getattr(config, "JUDGE_QUORUM", 2)
        lines: list[str] = []

        # ── Header ──────────────────────────────────────────────────────────────
        lines += [
            f"# Assay QA Report — {now}",
            "",
            f"**Duration:** {elapsed // 60}m {elapsed % 60}s  |  "
            f"**Tools tested:** {summary['tools_tested']}  |  "
            f"**Total tests:** {summary['total_tests']}",
            "",
            f"**Generator:** `{config.MODEL}`  |  "
            f"**Jury:** {' · '.join(f'`{j}`' for j in judges)}  (quorum {quorum}/{len(judges)})",
            "",
        ]

        # ── Verdict summary ──────────────────────────────────────────────────────
        lines += [
            "| Verdict | Count |",
            "|---------|-------|",
            f"| ✓ Pass | {summary['pass']} |",
            f"| ✗ Fail | {summary['fail']} |",
            f"| ⚠ Warning | {summary['warning']} |",
            f"| 🔒 Security | {summary['security']} |",
            "",
            "| Consensus | Non-pass count |",
            "|-----------|----------------|",
            f"| 🔴 Unanimous | {summary['consensus'].get('unanimous', 0)} |",
            f"| 🟡 Majority | {summary['consensus'].get('majority', 0)} |",
            f"| ⚪ Split (disputed) | {summary['consensus'].get('split', 0)} |",
            "",
        ]

        # ── Critical & Security findings ─────────────────────────────────────────
        critical = [f for f in self._findings if f.severity in ("critical", "major") or f.verdict == "security"]
        critical.sort(key=self._severity_order)
        if critical:
            lines += ["## Critical & Security Findings", ""]
            for f in critical:
                icon = "🔒" if f.verdict == "security" else "🔴" if f.severity == "critical" else "🟠"
                consensus_tag = {"unanimous": "", "majority": " *(2/3)*", "split": " *(disputed)*"}.get(f.consensus, "")
                lines += [
                    f"### {icon} `{f.tool}` — {f.summary}{consensus_tag}",
                    f"- **Test:** {f.test_name}",
                    f"- **Args:** `{json.dumps(f.arguments)}`",
                    f"- **Category:** {f.category}  |  **Duration:** {f.duration_ms}ms  |  **Consensus:** {f.consensus}",
                    f"- {f.detail}",
                    "",
                ]
                if f.judge_verdicts:
                    lines += [
                        "| Judge | Verdict | Severity | Summary |",
                        "|-------|---------|----------|---------|",
                    ]
                    for jv in f.judge_verdicts:
                        v_icon = {"pass": "✓", "fail": "✗", "warning": "⚠", "security": "🔒"}.get(jv["verdict"], "•")
                        lines.append(f"| `{jv['model']}` | {v_icon} {jv['verdict']} | {jv['severity']} | {jv['summary']} |")
                    lines.append("")
                    # Reasoning chains for thinking-mode judges
                    for jv in f.judge_verdicts:
                        if jv.get("reasoning_chain"):
                            short_model = jv["model"].split(":")[0]
                            reasoning_excerpt = jv["reasoning_chain"][:1000]
                            if len(jv["reasoning_chain"]) > 1000:
                                reasoning_excerpt += "…"
                            lines += [
                                f"<details><summary>{short_model} reasoning</summary>",
                                "",
                                f"```",
                                reasoning_excerpt,
                                f"```",
                                "",
                                "</details>",
                                "",
                            ]

        # ── Judge Disagreements ──────────────────────────────────────────────────
        disagreements = [f for f in self._findings if f.verdict != "pass" and f.consensus != "unanimous"]
        disagreements.sort(key=self._priority_rank)
        if disagreements:
            lines += [
                "## ⚪ Judge Disagreements",
                "",
                "> These findings had split or majority verdicts — at least one judge disagreed.",
                "> Review carefully: they may be false positives or genuinely ambiguous edge cases.",
                "",
            ]
            current_tool = None
            for f in disagreements:
                if f.tool != current_tool:
                    lines += [f"### {f.tool}", ""]
                    current_tool = f.tool
                icon = {"fail": "✗", "warning": "⚠", "security": "🔒"}.get(f.verdict, "•")
                followup_tag = " *(follow-up)*" if f.is_followup else ""
                lines += [
                    f"**{icon} {f.test_name}**{followup_tag} `[{f.severity}]` `[{f.consensus}]`",
                    f"{f.summary}",
                    f"> Args: `{json.dumps(f.arguments)}`  Duration: {f.duration_ms}ms",
                    "",
                ]
                if f.judge_verdicts:
                    lines += [
                        "| Judge | Verdict | Summary |",
                        "|-------|---------|---------|",
                    ]
                    for jv in f.judge_verdicts:
                        v_icon = {"pass": "✓", "fail": "✗", "warning": "⚠", "security": "🔒"}.get(jv["verdict"], "•")
                        lines.append(f"| `{jv['model']}` | {v_icon} {jv['verdict']} | {jv['summary']} |")
                    lines.append("")

        # ── Tool-by-tool results ─────────────────────────────────────────────────
        lines += ["## Tool-by-Tool Results", ""]
        lines += ["| Tool | Tests | ✓ | ✗ | ⚠ | 🔒 | 🔴Unani | 🟡Maj | ⚪Split | Avg ms | Max ms |"]
        lines += ["|------|-------|---|---|---|----|---------|----|---------|--------|--------|"]
        for tool, s in sorted(self._tool_stats.items()):
            avg = s["total_ms"] // s["total"] if s["total"] else 0
            lines.append(
                f"| {tool} | {s['total']} | {s['pass']} | {s['fail']} | "
                f"{s['warning']} | {s.get('security', 0)} | "
                f"{s.get('unanimous_failures', 0)} | {s.get('majority_failures', 0)} | "
                f"{s.get('split_verdicts', 0)} | {avg} | {s['max_ms']} |"
            )
        lines.append("")

        # ── All non-pass findings ────────────────────────────────────────────────
        non_pass = [f for f in self._findings if f.verdict != "pass"]
        non_pass.sort(key=self._priority_rank)
        if non_pass:
            lines += ["## All Findings (non-pass)", ""]
            current_tool = None
            for f in non_pass:
                if f.tool != current_tool:
                    lines += [f"### {f.tool}", ""]
                    current_tool = f.tool
                icon = {"fail": "✗", "warning": "⚠", "security": "🔒"}.get(f.verdict, "•")
                followup_tag = " *(follow-up)*" if f.is_followup else ""
                consensus_badge = {"unanimous": "🔴", "majority": "🟡", "split": "⚪"}.get(f.consensus, "")
                lines += [
                    f"**{icon} {f.test_name}**{followup_tag} `[{f.severity}]` {consensus_badge}",
                    f"{f.summary}",
                    f"> Args: `{json.dumps(f.arguments)}`  Duration: {f.duration_ms}ms",
                    f"> {f.detail}",
                    "",
                ]
                if f.judge_verdicts:
                    lines += [
                        "| Judge | Verdict | Severity | Summary |",
                        "|-------|---------|----------|---------|",
                    ]
                    for jv in f.judge_verdicts:
                        v_icon = {"pass": "✓", "fail": "✗", "warning": "⚠", "security": "🔒"}.get(jv["verdict"], "•")
                        lines.append(f"| `{jv['model']}` | {v_icon} {jv['verdict']} | {jv['severity']} | {jv['summary']} |")
                    lines.append("")

        path = self._dir / "report.md"
        path.write_text("\n".join(lines))
        return path

    def _write_claude_context(self) -> None:
        """
        Write claude_context.json — optimized for Claude Code to consume.

        Design principles:
        - actionable_findings sorted by priority: unanimous failures first, then severity.
          These are the highest-confidence real bugs. Act on these first.
        - split_verdicts separated: at least 2 judges disagreed. These are the most likely
          false positives. Review reasoning_chain before acting.
        - reasoning_chain fields contain the full model thinking. Use them to understand
          WHY a judge flagged something — this is the most valuable data for root-cause analysis.
        - tool_health provides a quick per-tool signal: unanimous_failures is the count of
          things definitely broken; split_verdicts is the count of uncertain findings.
        - jury_stats.judge_agreement_rate: close to 1.0 = clean run; low = many false positives
          or genuinely ambiguous system behavior.
        """
        summary = self._build_summary_dict()
        elapsed = int(time.time() - self._started_at)
        judges = getattr(config, "JUDGE_MODELS", [])
        quorum = getattr(config, "JUDGE_QUORUM", 2)

        # Jury agreement stats
        total = len(self._findings)
        unanimous_count = sum(1 for f in self._findings if f.consensus == "unanimous")
        split_count = sum(1 for f in self._findings if f.consensus == "split")
        majority_count = sum(1 for f in self._findings if f.consensus == "majority")
        agreement_rate = round(unanimous_count / total, 3) if total else 1.0

        # Separate findings by actionability
        non_pass = [f for f in self._findings if f.verdict != "pass"]
        non_pass.sort(key=self._priority_rank)

        splits = [f for f in self._findings if f.consensus == "split"]

        # Build actionable_findings with priority_rank
        actionable = []
        for rank, f in enumerate(non_pass, 1):
            entry = {
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
                "consensus": f.consensus,
                "summary": f.summary,
                "detail": f.detail,
                "is_followup": f.is_followup,
                "judges": f.judge_verdicts,
            }
            actionable.append(entry)

        # Build split_verdicts list
        split_entries = []
        for f in splits:
            if f.verdict != "pass":
                continue  # already in actionable
            vote_counts: dict[str, int] = {}
            for jv in f.judge_verdicts:
                vote_counts[jv["verdict"]] = vote_counts.get(jv["verdict"], 0) + 1
            split_entries.append({
                "tool": f.tool,
                "test_name": f.test_name,
                "arguments": f.arguments,
                "result_repr": f.result_repr,
                "final_verdict": f.verdict,
                "votes": vote_counts,
                "judges": f.judge_verdicts,
            })

        # Tool health
        tool_health = {}
        for tool, s in self._tool_stats.items():
            tool_health[tool] = {
                "tests": s["total"],
                "pass": s["pass"],
                "fail": s["fail"],
                "warning": s["warning"],
                "security": s.get("security", 0),
                "unanimous_failures": s.get("unanimous_failures", 0),
                "majority_failures": s.get("majority_failures", 0),
                "split_verdicts": s.get("split_verdicts", 0),
                "avg_ms": s["total_ms"] // s["total"] if s["total"] else 0,
                "max_ms": s["max_ms"],
            }

        data = {
            "schema_version": "2.0",
            "meta": {
                "generated_at": datetime.now().isoformat(),
                "duration_s": elapsed,
                "generator_model": config.MODEL,
                "judge_models": judges,
                "quorum": quorum,
                "totals": {
                    "tools": summary["tools_tested"],
                    "tests": summary["total_tests"],
                    "pass": summary["pass"],
                    "fail": summary["fail"],
                    "warning": summary["warning"],
                    "security": summary["security"],
                },
            },
            "how_to_use": (
                "Start with actionable_findings (sorted by priority: unanimous non-pass first, "
                "then by severity). Use consensus field to gauge confidence: unanimous=high, "
                "majority=medium, split=low/likely false positive. "
                "reasoning_chain fields contain full model thinking — use them to understand "
                "root cause before acting. split_verdicts section lists findings where judges "
                "disagreed on a passing test — review if suspicious."
            ),
            "jury_stats": {
                "unanimous": unanimous_count,
                "majority": majority_count,
                "split": split_count,
                "unanimous_non_pass": summary["unanimous_non_pass"],
                "judge_agreement_rate": agreement_rate,
                "interpretation": (
                    "agreement_rate close to 1.0 = consistent run, high confidence findings. "
                    "Low agreement_rate = many ambiguous cases, review split_verdicts carefully."
                ),
            },
            "actionable_findings": actionable,
            "split_verdicts_passing": split_entries,
            "tool_health": tool_health,
        }

        (self._dir / "claude_context.json").write_text(json.dumps(data, indent=2))
