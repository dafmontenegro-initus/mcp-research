#!/usr/bin/env bash
# Launches assay QA agent in a named tmux session.
# Safe to re-run — kills existing session first.
#
# Usage:
#   bash start.sh                    # full run (all tools)
#   bash start.sh --tool list_meetings   # single tool
#   bash start.sh --dry-run          # connect and list tools only

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ARGS="${*}"

# ── Kill previous session if running ─────────────────────────────────────────
tmux kill-session -t assay 2>/dev/null && echo "[~] Killed previous 'assay' session"

# ── Check Ollama is reachable ────────────────────────────────────────────────
OLLAMA_URL=$(grep OLLAMA_URL .env 2>/dev/null | cut -d= -f2 || echo "http://localhost:11434")
if ! curl -s --max-time 3 "$OLLAMA_URL/api/tags" > /dev/null 2>&1; then
    echo "⚠  Ollama not responding on $OLLAMA_URL"
    echo "   sudo systemctl start ollama"
    exit 1
fi

# ── Check trajectory-mcp is reachable ────────────────────────────────────────
TOKEN=$(grep MCP_TOKEN .env 2>/dev/null | cut -d= -f2)
if [ -n "$TOKEN" ] && ! curl -s --max-time 3 "http://localhost:8080/mcp?token=$TOKEN" > /dev/null 2>&1; then
    echo "⚠  trajectory-mcp not responding on :8080 — start it first:"
    echo "   bash ~/workspace/mcp-research/trajectory-mcp/start.sh"
    exit 1
fi

# ── Launch assay in tmux ──────────────────────────────────────────────────────
echo "Starting assay agent…"
tmux new-session -d -s assay "cd $SCRIPT_DIR && .venv/bin/python3 runner.py $ARGS; echo ''; echo '[ assay finished — press any key to close ]'; read"

sleep 1
if tmux has-session -t assay 2>/dev/null; then
    echo "✓ assay running (attach: tmux attach -t assay)"
else
    echo "✗ assay failed to start — check venv: python3 -m venv .venv && pip install -r requirements.txt"
    exit 1
fi

echo ""
echo "Logs:  tmux attach -t assay"
echo "Stop:  tmux kill-session -t assay"
echo ""
FINDINGS_DIR="$SCRIPT_DIR/findings"
if [ -d "$FINDINGS_DIR" ]; then
    LATEST=$(ls -t "$FINDINGS_DIR" 2>/dev/null | head -1)
    [ -n "$LATEST" ] && echo "Last report: $FINDINGS_DIR/$LATEST/report.md"
fi
