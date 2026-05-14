from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

MCP_URL: str = os.getenv("MCP_URL", "http://localhost:8080")
MCP_TOKEN: str = os.getenv("MCP_TOKEN", "")
OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL: str = os.getenv("MODEL", "qwen3.6:27b")

# Jury: comma-separated list of judge models — all evaluated independently, majority wins.
# All should be similar in size (~30B) so votes are comparable.
# Falls back to legacy JUDGE_MODEL for single-judge compat.
_judges_raw = os.getenv("JUDGE_MODELS", os.getenv("JUDGE_MODEL", "deepseek-r1:32b,gemma4:31b,granite4.1:30b"))
JUDGE_MODELS: list[str] = [m.strip() for m in _judges_raw.split(",") if m.strip()]

# Minimum votes needed for a non-pass verdict to stand. Default: majority of jury.
JUDGE_QUORUM: int = int(os.getenv("JUDGE_QUORUM", str((len(JUDGE_MODELS) // 2) + 1)))

# A tool call taking longer than this gets a "performance" warning.
SLOW_MS: int = int(os.getenv("SLOW_MS", "5000"))
# A tool call taking longer than this gets a "performance" critical finding.
CRITICAL_LATENCY_MS: int = int(os.getenv("CRITICAL_LATENCY_MS", "15000"))
# Max reflexion follow-ups per interesting finding.
MAX_FOLLOWUPS: int = int(os.getenv("MAX_FOLLOWUPS", "3"))
# Number of tools tested in parallel. Each worker opens its own MCP session.
# Ollama serializes LLM calls, but MCP I/O overlaps across workers — each worker
# calls the MCP server while others wait for the LLM. Sweet spot: ~8 for 14 tools.
# Can safely go up to the number of tools (one-per-tool) without CPU issues.
MAX_WORKERS: int = int(os.getenv("MAX_WORKERS", "8"))
