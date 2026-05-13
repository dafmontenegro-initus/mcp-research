#!/usr/bin/env bash
# Launches crucible QA agent in a named tmux session.
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
tmux kill-session -t crucible 2>/dev/null && echo "[~] Killed previous 'crucible' session"

# ── Check trajectory-mcp is reachable ────────────────────────────────────────
TOKEN=$(grep MCP_TOKEN .env 2>/dev/null | cut -d= -f2)
if [ -n "$TOKEN" ] && ! curl -s --max-time 3 "http://localhost:8080/mcp?token=$TOKEN" > /dev/null 2>&1; then
    echo "⚠  trajectory-mcp not responding on :8080 — start it first:"
    echo "   bash ~/workspace/mcp-research/trajectory-mcp/start.sh"
    exit 1
fi

# ── Launch crucible in tmux ───────────────────────────────────────────────────
echo "[1/1] Starting crucible agent…"
tmux new-session -d -s crucible "cd $SCRIPT_DIR && .venv/bin/python3 runner.py $ARGS; echo ''; echo '[ crucible finished — press any key to close ]'; read"

sleep 1
if tmux has-session -t crucible 2>/dev/null; then
    echo "      ✓ crucible running"
else
    echo "      ✗ crucible failed to start — check venv: python3 -m venv .venv && pip install -r requirements.txt"
    exit 1
fi

echo ""
echo "Logs:  tmux attach -t crucible"
echo "Stop:  tmux kill-session -t crucible"
echo ""
FINDINGS_DIR="$SCRIPT_DIR/findings"
if [ -d "$FINDINGS_DIR" ]; then
    LATEST=$(ls -t "$FINDINGS_DIR" 2>/dev/null | head -1)
    [ -n "$LATEST" ] && echo "Last report: $FINDINGS_DIR/$LATEST/report.md"
fi
