#!/usr/bin/env bash
# Launches RAG service + MCP server in named tmux sessions.
# Safe to re-run — kills existing sessions first.
# Usage: bash start.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Kill previous sessions if running ────────────────────────────────────────
tmux kill-session -t rag 2>/dev/null && echo "[~] Killed previous 'rag' session"
tmux kill-session -t mcp 2>/dev/null && echo "[~] Killed previous 'mcp' session"

# Pass the caller's terminal size to tmux so log output uses the real width
# instead of tmux's 80×24 default — otherwise output wraps awkwardly when the
# user attaches later from a wider terminal.
COLS=$(tput cols 2>/dev/null || echo 200)
LINES=$(tput lines 2>/dev/null || echo 50)

# ── RAG service ───────────────────────────────────────────────────────────────
echo "[1/2] Starting RAG service (port 8090)..."
tmux new-session -d -s rag -x "$COLS" -y "$LINES" \
    "cd $SCRIPT_DIR && rag_service/.venv/bin/python3 rag_service/app.py"

echo -n "      waiting (cold start may take ~10 min for NWN warmup)"
RAG_UP=0
for i in $(seq 1 150); do
    sleep 4
    if curl -s http://localhost:8090/health > /dev/null 2>&1; then
        echo " ✓ RAG service up ($((i * 4))s)"
        RAG_UP=1
        break
    fi
    echo -n "."
    if [ "$i" -eq 150 ]; then
        echo " ⚠ RAG service not responding after $((150 * 4))s — check: tmux attach -t rag"
    fi
done

# ── MCP server ────────────────────────────────────────────────────────────────
echo "[2/2] Starting MCP server (port 8080)..."
tmux new-session -d -s mcp -x "$COLS" -y "$LINES" \
    "cd $SCRIPT_DIR && .venv/bin/python3 server.py"

sleep 2
if curl -s http://localhost:8080/mcp > /dev/null 2>&1; then
    echo "      ✓ MCP server up"
else
    echo "      ⚠ MCP server not responding yet — check: tmux attach -t mcp"
fi

echo ""
echo "Sessions running:"
tmux ls
echo ""
echo "Logs:  tmux attach -t rag   |   tmux attach -t mcp"
echo "Stop:  tmux kill-session -t rag; tmux kill-session -t mcp"