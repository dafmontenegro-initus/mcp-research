from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

MCP_URL: str = os.getenv("MCP_URL", "http://localhost:8080")
MCP_TOKEN: str = os.getenv("MCP_TOKEN", "")
OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL: str = os.getenv("MODEL", "deepseek-r1:32b")

# A tool call taking longer than this gets a "performance" warning.
SLOW_MS: int = int(os.getenv("SLOW_MS", "5000"))
# A tool call taking longer than this gets a "performance" critical finding.
CRITICAL_LATENCY_MS: int = int(os.getenv("CRITICAL_LATENCY_MS", "15000"))
# Max reflexion follow-ups per interesting finding.
MAX_FOLLOWUPS: int = int(os.getenv("MAX_FOLLOWUPS", "3"))
